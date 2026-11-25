"""The ONE Athena runner for the VM: SQL cells, read_athena (and the catalog
discovery helpers built on it), unload_athena_to_s3 and the SQL-cell UNLOAD.

Behaviour shared by every caller:
- the run is bounded by the active run guard's budget (SQL cell or Python cell;
  EXECUTION_TIMEOUT_SECONDS for a bare call outside any cell) and registers
  stop_query_execution as the guard's native cancel, so a timeout or POST
  /interrupt cancels the query instead of leaving it scanning and billing;
- stop_query_execution is called at most once, on timeout, guard fire, any
  exception (including the KeyboardInterrupt a Python-cell timeout injects) and
  never once the query reached a terminal state;
- results are paginated and typed from Athena's declared column types, NULL is None.

Underscore-prefixed module: helpers/__init__ star-imports readers, so readers
imports these names under underscore aliases.
"""
import threading
import time as _time

import pandas as pd

from .. import aws_clients as _aws
from ..run_limits import TIMEOUT_ERROR, execution_timeout_seconds
from ._common import logger

_ATHENA_INT_TYPES = {"tinyint", "smallint", "int", "integer", "bigint"}
_ATHENA_FLOAT_TYPES = {"float", "double", "real", "decimal"}
_ATHENA_BOOL_TYPES = {"boolean"}
_ATHENA_DATETIME_TYPES = {"date", "timestamp"}


def coerce_athena_dtypes(df: pd.DataFrame, col_info) -> pd.DataFrame:
    """Convert an all-string Athena result frame to real dtypes using the query's
    column metadata. Best-effort: any column that fails to convert is left as-is
    (never raises), so a surprising value can't break the read."""
    if df.empty or not col_info:
        return df
    for col in col_info:
        name = col.get("Name")
        # Athena base type, e.g. "bigint", "double", "varchar", "decimal(10,2)".
        atype = (col.get("Type") or "").split("(", 1)[0].strip().lower()
        if name not in df.columns:
            continue
        try:
            if atype in _ATHENA_INT_TYPES:
                # Nullable integer so NULLs (None) survive as <NA>, not a cast error.
                df[name] = pd.to_numeric(df[name], errors="coerce").astype("Int64")
            elif atype in _ATHENA_FLOAT_TYPES:
                df[name] = pd.to_numeric(df[name], errors="coerce")
            elif atype in _ATHENA_BOOL_TYPES:
                df[name] = df[name].map(
                    {"true": True, "True": True, "false": False, "False": False}
                ).astype("boolean")
            elif atype in _ATHENA_DATETIME_TYPES:
                df[name] = pd.to_datetime(df[name], errors="coerce")
            # varchar/char/string/array/map/row/etc. → leave as string
        except Exception:
            # Never let a coercion surprise break the read — keep the raw strings.
            pass
    return df


def execute_athena(sql, *, workgroup, region=None, database=None, catalog=None,
                   output_location=None, timeout_s=None,
                   fail_prefix="Athena query {state}", enrich_reason=None):
    """Start `sql`, wait for SUCCEEDED under the guard/deadline, and return
    (query_execution_id, athena_client). Raises RuntimeError on FAILED/CANCELLED
    (`fail_prefix` may use {state}; `enrich_reason(reason)` may extend the reason),
    on timeout and on guard fire."""
    from ..run_guard import current_guard

    athena = _aws.aws_client("athena", region)
    start_kwargs = {"QueryString": sql, "WorkGroup": workgroup}
    ctx = {}
    if catalog:
        ctx["Catalog"] = catalog
    if database:
        ctx["Database"] = database
    if ctx:
        start_kwargs["QueryExecutionContext"] = ctx
    if output_location:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": output_location}
    execution_id = athena.start_query_execution(**start_kwargs)["QueryExecutionId"]

    stop_lock = threading.Lock()
    stop_state = {"stopped": False, "finished": False}

    def _stop():
        with stop_lock:
            if stop_state["stopped"] or stop_state["finished"]:
                return
            stop_state["stopped"] = True
        try:
            athena.stop_query_execution(QueryExecutionId=execution_id)
            logger.info(f"Athena query {execution_id} stopped")
        except Exception as e:
            logger.warning(f"Athena stop_query_execution failed for {execution_id}: {e}")

    guard = current_guard()
    if guard is not None:
        guard.add_cancel(_stop)
    if timeout_s is None:
        timeout_s = guard.timeout_s if guard is not None else execution_timeout_seconds()
    # Inside a cell the guard's timer owns the budget and calls _stop; this deadline
    # only bounds a bare call outside any cell.
    deadline = _time.monotonic() + timeout_s
    try:
        while True:
            status_resp = athena.get_query_execution(QueryExecutionId=execution_id)
            status = status_resp["QueryExecution"]["Status"]
            state = status["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                with stop_lock:
                    stop_state["finished"] = True
            if state == "SUCCEEDED":
                break
            if guard is not None and guard.fired:
                raise RuntimeError(guard.error_message())
            if state in ("FAILED", "CANCELLED"):
                reason = status.get("StateChangeReason", "Unknown error")
                if enrich_reason is not None:
                    reason = enrich_reason(reason)
                raise RuntimeError(f"{fail_prefix.format(state=state)}: {reason}")
            if _time.monotonic() >= deadline:
                raise RuntimeError(TIMEOUT_ERROR.format(seconds=timeout_s))
            _time.sleep(0.5)
    except BaseException:
        _stop()
        raise
    finally:
        if guard is not None:
            guard.remove_cancel(_stop)
    return execution_id, athena


def fetch_athena_results(athena, execution_id: str) -> pd.DataFrame:
    """Paginate get_query_results into a typed DataFrame (NULL is None)."""
    col_info = None
    columns = None
    rows = []
    next_token = None
    first_page = True
    while True:
        kwargs = {"QueryExecutionId": execution_id}
        if next_token:
            kwargs["NextToken"] = next_token
        results = athena.get_query_results(**kwargs)
        if col_info is None:
            col_info = results["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
            columns = [col["Name"] for col in col_info]
        page_rows = results["ResultSet"]["Rows"]
        # Athena repeats the header row as the first row of the FIRST page only.
        if first_page:
            page_rows = page_rows[1:]
            first_page = False
        for row in page_rows:
            # VarCharValue is absent for SQL NULLs — map those to None (not "").
            rows.append([field.get("VarCharValue", None) for field in row["Data"]])
        next_token = results.get("NextToken")
        if not next_token:
            break
    df = pd.DataFrame(rows, columns=columns)
    # Athena returns every value as a string; coerce by the DECLARED column type.
    return coerce_athena_dtypes(df, col_info)


def run_athena(sql, *, workgroup, region=None, database=None, catalog=None,
               output_location=None, fetch="rows", timeout_s=None,
               fail_prefix="Athena query {state}", enrich_reason=None):
    """Run `sql` and return a typed DataFrame (fetch="rows") or None (fetch="none",
    for UNLOAD/DDL: rows never reach the VM)."""
    qid, athena = execute_athena(
        sql, workgroup=workgroup, region=region, database=database, catalog=catalog,
        output_location=output_location, timeout_s=timeout_s,
        fail_prefix=fail_prefix, enrich_reason=enrich_reason)
    if fetch == "none":
        return None
    return fetch_athena_results(athena, qid)
