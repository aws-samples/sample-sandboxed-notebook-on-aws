"""
SageMaker real-time endpoint data source provider.

Surfaces this project's LIVE SageMaker inference endpoints (the ones
sm_deploy_serverless / sm_deploy_instance create, named microvm-ep-*) as first-class
entries in the Data Sources panel,
alongside S3/Athena/DynamoDB/Aurora/Bedrock-KB. Unlike those, an endpoint is not a
tabular source you read or SQL against — it's a deployed model you INVOKE — so:

  • supports_sql = False        (excluded from SQL-cell routing + sql_syntax_docs)
  • get_schema() returns None   (an endpoint has no rows/columns to describe)
  • get_preview() returns []    (nothing to preview; you invoke it with rows)
  • is_deletable() = True       (the panel offers a delete button; delete() tears
                                 down the endpoint + its config + model)

Discovery + deletion are proxy-side control-plane calls (sagemaker:ListEndpoints /
DeleteEndpoint / DeleteEndpointConfig / DeleteModel) made with the proxy's ambient
boto3 credentials — NOT the in-VM helpers (which can't be imported here). The VM
invokes the endpoint via sm_predict(endpoint, df); this provider only lists/deletes.

Scoped to microvm-ep-* (DATASOURCE_SM_ENDPOINT_PREFIX) so the panel shows only
this project's endpoints, matching the exec role's IAM scoping (endpoint/microvm-ep-*)
and the sm_endpoints() / sm_deploy_* naming convention.
"""

import os
import asyncio
import logging
from typing import Optional


from proxy.platform import aws_clients
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef

logger = logging.getLogger(__name__)

# Sentinel payload the OUR-container serving image answers with its I/O schema
# (feat_cols/classes/task) instead of running inference. See serving/serve_app.py.
# A foreign container won't set `microvm_schema` in its reply, so we treat any
# non-conforming response as "schema unavailable".
_SCHEMA_PROBE = b'{"__microvm_schema__": true}'
_SCHEMA_CACHE_TTL_SEC = 300  # 5 min — a deployed model's schema is immutable


class SageMakerEndpointProvider(DataSourceProvider):
    """Data source provider for live SageMaker real-time endpoints (microvm-ep-*)."""

    display_name = "SageMaker Endpoints"
    icon = "sagemaker_endpoint"
    supports_sql = False           # an endpoint is invoked, not SQL-queried
    requires_vm_execution = False  # list/delete are proxy-side control-plane calls

    def __init__(self):
        self._region = os.environ.get("AWS_REGION", "us-west-2")
        # Only surface THIS project's endpoints. The 'microvm-ep-' prefix is what
        # deploy stamps (_sanitize_endpoint_name) and what the exec role's IAM
        # scoping filters on, so it's the right discovery + deletion boundary.
        self._name_contains = os.environ.get(
            "DATASOURCE_SM_ENDPOINT_PREFIX", "microvm-ep-"
        ).strip()
        # endpoint_name -> (ts, SourceSchema|None). A schema probe is a real (small)
        # InvokeEndpoint that can COLD-START a scaled-to-zero endpoint, so we cache the
        # result — including a None "no schema" — to avoid re-invoking on every panel
        # schema-expand. get_schema is called ON DEMAND (panel expander), never during
        # discover(), so background refreshes don't wake idle endpoints.
        self._schema_cache: dict[str, tuple[float, "SourceSchema | None"]] = {}
        # config_name -> {"mode","instance_type","instance_count","memory_mb",
        # "max_concurrency"}. An endpoint's CONFIG is immutable (our naming stamps a
        # fresh timestamped name per deploy, so a config name never changes shape), so
        # cache classification FOREVER — no TTL. This keeps discover() at ~1 extra
        # describe per NEW endpoint/config and zero for ones we've already classified.
        self._config_cache: dict[str, dict] = {}

    @property
    def source_type(self) -> str:
        return "sagemaker_endpoint"

    def _client(self):
        return aws_clients.get_client("sagemaker", region_name=self._region)

    # --- Auto-discovery ---------------------------------------------------
    async def discover(self, session_id: str = None) -> list[SourceRef]:
        """List this account's microvm-ep-* endpoints (most-recent first) as a flat
        set of sources. Status + region ride along in detail/extra so the panel can
        render 'InService' / 'Creating' / 'Failed' and gate the delete button.

        list_endpoints is a blocking boto3 call, so the whole enumeration runs via
        asyncio.to_thread — otherwise it stalls the proxy's single event loop for the
        duration of the control-plane call (mirrors S3/Aurora.discover)."""

        def _enumerate() -> list[SourceRef]:
            sm = self._client()
            refs: list[SourceRef] = []
            live_configs: set[str] = set()  # config names that back a LIVE endpoint
            try:
                resp = sm.list_endpoints(
                    SortBy="CreationTime",
                    SortOrder="Descending",
                    NameContains=self._name_contains,
                    MaxResults=50,
                )
                for e in resp.get("Endpoints", []):
                    name = e.get("EndpointName", "")
                    if not name:
                        continue
                    status = e.get("EndpointStatus", "") or ""
                    created = e.get("CreationTime")
                    # Classify serverless vs instance (cached by config name).
                    shape = self._classify_endpoint(sm, name)
                    if shape.get("config_name"):
                        live_configs.add(shape["config_name"])
                    refs.append(self._ref_for_endpoint(name, status, created, shape))
            except Exception as e:  # pragma: no cover — control-plane failure hides the group
                logger.warning(f"SageMaker endpoint discovery failed: {e}")

            # STOPPED endpoints: a retained microvm-ep-* CONFIG whose live endpoint is
            # gone (sm_stop_endpoint deletes the endpoint but keeps config+model). Emit
            # them so the panel can show a Stopped row with a Start action. Instance
            # endpoints are the ones that get stopped; a serverless config left dangling
            # (rare) is surfaced too but shows no Start (supports_lifecycle gates it).
            try:
                cfg_resp = sm.list_endpoint_configs(
                    SortBy="CreationTime", SortOrder="Descending",
                    NameContains=self._name_contains, MaxResults=50,
                )
                for c in cfg_resp.get("EndpointConfigs", []):
                    cfg_name = c.get("EndpointConfigName", "")
                    if not cfg_name or cfg_name in live_configs:
                        continue  # backs a live endpoint → already listed above
                    # Our deploy uses the SAME name for endpoint + config, so the
                    # stopped endpoint's name IS the config name.
                    shape = self._classify_config(sm, cfg_name)
                    refs.append(self._ref_for_endpoint(
                        cfg_name, "Stopped", c.get("CreationTime"), shape))
            except Exception as e:  # pragma: no cover
                logger.info(f"SageMaker stopped-endpoint scan skipped: {e}")

            return refs

        return await asyncio.to_thread(_enumerate)

    # --- Classification helpers (mode: serverless vs instance) -----------
    def _classify_config(self, sm, config_name: str) -> dict:
        """Read an endpoint-config's production variant and return a shape dict
        {config_name, mode, instance_type?, instance_count?, memory_mb?,
        max_concurrency?}. Cached FOREVER by config name (configs are immutable in
        our naming scheme). Best-effort: a missing config → {mode: 'unknown'}."""
        cached = self._config_cache.get(config_name)
        if cached is not None:
            return cached
        shape: dict = {"config_name": config_name, "mode": "unknown"}
        try:
            cfg = sm.describe_endpoint_config(EndpointConfigName=config_name)
            variants = cfg.get("ProductionVariants", [])
            if variants:
                v = variants[0]
                sc = v.get("ServerlessConfig") or {}
                if sc:
                    shape["mode"] = "serverless"
                    if sc.get("MemorySizeInMB") is not None:
                        shape["memory_mb"] = int(sc["MemorySizeInMB"])
                    if sc.get("MaxConcurrency") is not None:
                        shape["max_concurrency"] = int(sc["MaxConcurrency"])
                elif v.get("InstanceType"):
                    shape["mode"] = "instance"
                    shape["instance_type"] = v["InstanceType"]
                    if v.get("InitialInstanceCount") is not None:
                        shape["instance_count"] = int(v["InitialInstanceCount"])
        except Exception as e:  # pragma: no cover
            logger.info(f"classify endpoint-config {config_name} failed: {e}")
            return shape  # don't cache an errored read — retry next discover
        self._config_cache[config_name] = shape
        return shape

    def _classify_endpoint(self, sm, endpoint_name: str) -> dict:
        """Resolve an endpoint's config name, then classify that config. One
        describe_endpoint (cheap, uncached — status changes) + a cached config read."""
        try:
            d = sm.describe_endpoint(EndpointName=endpoint_name)
            cfg_name = d.get("EndpointConfigName", "")
        except Exception:  # pragma: no cover
            return {"mode": "unknown"}
        if not cfg_name:
            return {"mode": "unknown"}
        return self._classify_config(sm, cfg_name)

    def _ref_for_endpoint(self, name, status, created, shape: dict) -> SourceRef:
        """Build the panel SourceRef for an endpoint (live or stopped), carrying its
        status, hosting mode, and shape so the UI can render a mode badge + status dot
        and gate Stop/Start."""
        mode = shape.get("mode", "unknown")
        # Secondary line: status · <shape> · region. Carry the full shape here (not
        # just via the badge, which can truncate in the narrow panel): instance →
        # instance TYPE + count; serverless → memory + max-concurrency.
        bits = [status] if status else []
        if mode == "instance":
            it = shape.get("instance_type") or "instance"
            cnt = shape.get("instance_count")
            bits.append(f"{it} ×{cnt}" if cnt and cnt != 1 else it)
        elif mode == "serverless":
            mem = shape.get("memory_mb")
            mc = shape.get("max_concurrency")
            if mem:
                shp = f"{int(mem) // 1024} GB" if int(mem) % 1024 == 0 else f"{mem} MB"
                if mc:
                    shp += f" · ≤{mc} concurrent"
                bits.append(shp)
            else:
                bits.append("serverless")
        if self._region:
            bits.append(self._region)
        return SourceRef(
            source_type="sagemaker_endpoint",
            source_id=name,
            display_name=name,
            detail=" · ".join(b for b in bits if b),
            extra={
                "status": status,
                "mode": mode,
                "instance_type": shape.get("instance_type"),
                "instance_count": shape.get("instance_count"),
                "memory_mb": shape.get("memory_mb"),
                "max_concurrency": shape.get("max_concurrency"),
                "region": self._region,
                "created": str(created) if created is not None else "",
                # Deletable always (config/model/endpoint teardown). The delete route
                # re-checks is_deletable(); the lifecycle route re-checks
                # supports_lifecycle() (instance-only) before stop/start.
                "deletable": True,
            },
        )

    # --- Schema / preview -------------------------------------------------
    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """Return the endpoint's INPUT schema — the raw feature columns to send and
        the classes it predicts — by asking the model itself.

        SageMaker records no schema for an endpoint (the control plane only knows the
        container image + config), so the ONLY way to learn the columns is to ask the
        running model. Our serving container answers a sentinel payload
        ({"__microvm_schema__": true}) with {feat_cols, classes, task} instead of
        running inference; we send that via InvokeEndpoint and turn feat_cols into
        columns. A FOREIGN endpoint (not our container) won't recognize the sentinel —
        its reply lacks the `microvm_schema` marker (or it errors), and we return None
        so the panel just shows no schema (graceful, never misleading).

        Cost note: a probe is a real (tiny) InvokeEndpoint that can COLD-START a
        scaled-to-zero endpoint, so the result is CACHED (5 min, including a None). This
        runs ON DEMAND (the panel schema expander), never during discover(), so a
        background refresh never wakes an idle endpoint."""
        import time

        cached = self._schema_cache.get(source_id)
        if cached and (time.time() - cached[0] < _SCHEMA_CACHE_TTL_SEC):
            return cached[1]

        schema = await asyncio.to_thread(self._probe_schema, source_id)
        self._schema_cache[source_id] = (time.time(), schema)
        return schema

    def _probe_schema(self, source_id: str) -> Optional[SourceSchema]:
        """Blocking: InvokeEndpoint with the schema sentinel, parse the reply into a
        SourceSchema. Returns None for a foreign/non-conforming/errored endpoint."""
        import json
        try:
            smr = aws_clients.get_client("sagemaker-runtime", region_name=self._region)
            resp = smr.invoke_endpoint(
                EndpointName=source_id,
                ContentType="application/json",
                Body=_SCHEMA_PROBE,
            )
            body = json.loads(resp["Body"].read())
        except Exception as e:  # pragma: no cover — foreign container, cold-fail, no perms, ...
            logger.info(f"SageMaker schema probe for {source_id} failed (no schema surfaced): {e}")
            return None

        # Only OUR container sets this marker. Anything else → treat as no schema.
        if not isinstance(body, dict) or not body.get("microvm_schema"):
            return None

        feat_cols = body.get("feat_cols") or []
        task = body.get("task") or ""
        classes = body.get("classes")
        columns = [ColumnInfo(name=str(c), dtype="", sample="") for c in feat_cols]
        # Secondary line: task + class labels, e.g. "classification · classes: 0, 1".
        detail_bits = []
        if task:
            detail_bits.append(task)
        if classes:
            shown = ", ".join(str(c) for c in classes[:6])
            if len(classes) > 6:
                shown += f", … (+{len(classes) - 6})"
            detail_bits.append(f"classes: {shown}")
        return SourceSchema(
            source_type="sagemaker_endpoint",
            source_id=source_id,
            display_name=source_id,
            columns=columns,
            row_count=None,
            size=" · ".join(detail_bits),
        )

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """No inline preview for an endpoint — there are no rows to show. You preview
        it by invoking it with sm_predict(endpoint, df) in a cell."""
        return []

    # --- Snippet generation ----------------------------------------------
    def get_python_snippet(self, source_id: str) -> str:
        return (
            "# Invoke a live SageMaker endpoint on RAW feature rows (a remote call —\n"
            "# the encoding/preprocessing is baked into the deployed model).\n"
            f"preds = sm_predict('{source_id}', df)              # label per row\n"
            f"# proba = sm_predict('{source_id}', df, proba=True)  # class probabilities\n"
            f"# scored = sm_predict_batch('{source_id}', df, output_col='pred')  # append a column\n"
            f"# print(sm_endpoint_status('{source_id}'))          # InService / Creating / Failed"
        )

    def get_sql_snippet(self, source_id: str) -> str:
        """An endpoint is invoked, not SQL-queried (supports_sql=False), so the panel
        hides the SQL button. If a SQL snippet is requested anyway, point at the
        Python helper rather than the generic 'no snippet' default."""
        return (
            "-- A SageMaker endpoint isn't SQL-queryable. Invoke it from a Python cell:\n"
            f"--   sm_predict('{source_id}', df)              # predictions for RAW rows\n"
            f"--   sm_predict_batch('{source_id}', df, output_col='pred')"
        )

    # --- AI assistant metadata -------------------------------------------
    def reader_docs(self) -> list[str]:
        return [
            "sm_predict(endpoint_name, df, proba=False) -> preds   # Invoke a live SageMaker endpoint (microvm-ep-*) on RAW rows; proba=True returns class probabilities",
            "sm_predict_batch(endpoint_name, df, output_col='prediction', proba=False) -> df   # Same, appending predictions as a column",
            "sm_endpoint_status(endpoint_name) -> str   # 'InService' / 'Creating' / 'Stopped' / 'Failed: <reason>' — cheap to poll",
            "sm_stop_endpoint(endpoint_name) -> str   # INSTANCE endpoints only: tear down the running endpoint to pause billing, keeping config+model to resume (no-op on serverless)",
            "sm_start_endpoint(endpoint_name) -> ep   # Resume a stopped INSTANCE endpoint by recreating it from its retained config",
        ]

    # supports_sql is False, so sql_syntax_docs() stays the inherited empty list.

    # --- Deletion (panel "delete endpoint") -------------------------------
    def is_deletable(self, source_id: str) -> bool:
        """True only for this project's microvm-ep-* endpoints. Guards the delete
        route so it can't be used to tear down an arbitrary account endpoint."""
        if not self._name_contains:
            return False
        return self._name_contains in source_id

    def delete(self, source_id: str) -> None:
        """Tear down a deployed endpoint. Caller MUST validate is_deletable() first.

        deploy creates three same-named resources — the endpoint, its
        endpoint-config, and the model — so a full delete removes all three.

        A STOPPED instance endpoint (sm_stop_endpoint deletes the live Endpoint but
        KEEPS its config + model so it can be resumed) has NO Endpoint object. So a
        'delete' on a stopped endpoint MUST NOT fail on the missing Endpoint — it
        should sweep the leftover config + model instead. We therefore treat an
        endpoint 'not found' as already-gone and continue to the config/model cleanup
        (mirroring the in-VM sm_delete_endpoint helper, which describes-first). Only a
        genuinely unexpected endpoint-delete error propagates to a 500."""
        sm = self._client()
        # Endpoint first — the billed resource. A 'not found' is the stopped-endpoint
        # (or already-deleted) case: swallow it and proceed to config/model cleanup.
        # Any OTHER error on the endpoint delete propagates (the route surfaces 500).
        try:
            sm.delete_endpoint(EndpointName=source_id)
            logger.info(f"Deleted SageMaker endpoint: {source_id}")
        except Exception as e:  # noqa: BLE001
            # Match on the MESSAGE (the authoritative "endpoint isn't there" signal),
            # not the exception class — DeleteEndpoint raises ValidationException for a
            # missing endpoint, but matching the class alone would also swallow an
            # unrelated validation error. These phrases are specific to "no such endpoint".
            msg = str(e).lower()
            if ("could not find endpoint" in msg or "not found" in msg
                    or "does not exist" in msg):
                logger.info(f"Endpoint '{source_id}' not running (stopped/already gone) "
                            f"— cleaning up its config + model.")
            else:
                raise
        # Config + model share the endpoint name (see _sanitize_endpoint_name). Clean
        # them up best-effort so we don't leak orphaned configs/models — this is what
        # reclaims a STOPPED endpoint's retained config + model.
        for label, fn, kwargs in (
            ("endpoint-config", sm.delete_endpoint_config, {"EndpointConfigName": source_id}),
            ("model", sm.delete_model, {"ModelName": source_id}),
        ):
            try:
                fn(**kwargs)
            except Exception as e:  # pragma: no cover — best-effort cleanup
                logger.warning(f"Best-effort delete of {label} '{source_id}' failed: {e}")

    # --- Lifecycle (stop / start) ----------------------------------------
    def supports_lifecycle(self, source_id: str) -> bool:
        """Stop/start is offered ONLY for this project's INSTANCE-backed endpoints.
        Serverless already scales to zero ($0 idle) so there's nothing to stop, and a
        foreign endpoint is out of scope. Reads the (cached) config to decide."""
        if not self.is_deletable(source_id):
            return False
        # Our deploy names endpoint + config identically, so source_id is the config.
        shape = self._classify_config(self._client(), source_id)
        return shape.get("mode") == "instance"

    def lifecycle_action(self, source_id: str, action: str) -> str:
        """Stop (delete endpoint only, keep config+model to resume) or Start (recreate
        the endpoint from its retained config). Caller MUST validate
        supports_lifecycle() first. Returns a short status string.

        This mirrors the in-VM sm_stop_endpoint / sm_start_endpoint, but runs here as
        a proxy-side control-plane call so the panel button doesn't need a live VM."""
        sm = self._client()
        if action == "stop":
            # Delete ONLY the endpoint; config + model (and the S3 artifact) survive so
            # a later Start can recreate it. Idempotent: already-stopped → no-op.
            try:
                sm.delete_endpoint(EndpointName=source_id)
            except Exception as e:  # pragma: no cover — already gone
                logger.info(f"stop {source_id}: endpoint not running ({e})")
                return f"{source_id} already stopped"
            logger.info(f"Stopped SageMaker endpoint (kept config+model): {source_id}")
            return f"Stopped {source_id}"
        if action == "start":
            # Recreate from the retained config (same name for endpoint + config).
            try:
                sm.describe_endpoint(EndpointName=source_id)
                return f"{source_id} already running"  # idempotent
            except Exception:  # noqa: BLE001 — not running; recreate
                pass
            try:
                sm.describe_endpoint_config(EndpointConfigName=source_id)
            except Exception:
                raise RuntimeError(
                    f"Cannot start '{source_id}': its endpoint-config no longer exists "
                    f"(it was deleted, not stopped). Redeploy with sm_deploy_instance.")
            sm.create_endpoint(EndpointName=source_id, EndpointConfigName=source_id)
            logger.info(f"Started SageMaker endpoint from retained config: {source_id}")
            return f"Starting {source_id}"
        raise ValueError(f"Unknown lifecycle action: {action!r} (expected 'stop' or 'start')")
