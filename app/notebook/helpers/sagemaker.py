"""MACHINE LEARNING / SAGEMAKER — managed training, batch inference & real-time endpoints (boto3 only)."""

import io
import os
import re
import json
import time
import tarfile

import boto3
import numpy as np
import pandas as pd

from ._common import _REGION, logger
from . import _s3
from .. import aws_clients as _aws


# =============================================================================
# MACHINE LEARNING / SAGEMAKER — managed training & batch inference
# =============================================================================
# (Categorized under "Machine Learning / SageMaker" so the sm_* helpers form their
# OWN Snippets-panel / autocomplete group — separate from the in-VM "Machine
# Learning / Local" helpers (train_model / predict / ONNX) — since they run on remote
# SageMaker compute, not in the VM. The category is derived from this header by
# completion_catalog._category_map_from_source. Private _sm_* helpers are
# underscore-prefixed and excluded from the catalog.)
#
# WHY THIS EXISTS
#   The in-VM train_model() runs inside the MicroVM — bounded by the VM's memory /
#   CPU / cell-execution window. When the data is too big for the VM, you need a
#   GPU, or training simply takes too long, SUBMIT the work to SageMaker instead:
#   the notebook uploads the data + a preprocessing recipe to S3 and asks SageMaker
#   to run a managed Training Job on its OWN compute (as big as you ask). The VM
#   stays small; SageMaker does the heavy lifting. Every call here is an HTTPS AWS
#   API call (sagemaker / s3), which the VM's managed egress (80/443/53) allows.
#
# SCOPE: managed TRAINING (sklearn FRAMEWORK script mode) + BATCH TRANSFORM (score a
#   whole table with a transient job). NO always-on real-time endpoints — that is
#   the standing-cost surface (sm_deploy_*). So nothing here bills after a job
#   finishes; you pay only for the minutes a training / transform job actually runs.
#
# ASYNC BY DESIGN: a training job takes minutes (instance spin-up alone is ~3-4m).
#   sm_train() therefore does NOT block — it uploads, calls CreateTrainingJob, and
#   RETURNS A HANDLE immediately (the cell finishes in seconds). Check progress with
#   job.status() / sm_training_job_status(job). The terminal helpers sm_load_model(job)
#   / predict_batch(job, df) wait-with-poll (bounded) only when you call them.
#
# ONE TRAINING PATH — sklearn FRAMEWORK SCRIPT MODE: sm_train runs OUR entry-point
#   (sm_scripts/sklearn_train.py) in AWS's managed scikit-learn container, which fits
#   a COMPLETE Pipeline(ColumnTransformer(one-hot) -> estimator) and writes it back as
#   a self-contained model.joblib. The estimator is chosen by name (estimator=, e.g.
#   random_forest [default], xgboost, lightgbm). This keeps the SAME "feed raw rows"
#   ergonomics as train_model: the Pipeline carries its own preprocessing, so
#   predict(model, raw_df) behaves identically whether the model trained in the VM or
#   on SageMaker — and sm_load_model is a plain joblib.load (no rewrap). Taking data
#   straight from an S3 pointer (unload_athena_to_s3) uses this same path.

# SageMaker framework container registry account, by region. SageMaker publishes the
# managed framework images (sagemaker-scikit-learn, and historically sagemaker-xgboost)
# from a per-region AWS-owned account. We build the image URI without the sagemaker
# SDK (not baked into the VM — boto3 only), so this map must be correct per region or
# CreateTrainingJob fails with a "manifest not found" ECR error.
#
# SOURCE OF TRUTH — do NOT hand-edit from memory. This is the FULL registry map
# extracted verbatim from the SageMaker Python SDK's own config. The per-region
# accounts for sagemaker-scikit-learn and sagemaker-xgboost are IDENTICAL across all
# 37 regions (verified against both image_uri_config/sklearn.json and xgboost.json
# training registries — zero mismatches), so one map serves the sklearn framework
# image. To refresh/verify: `pip install sagemaker` in a throwaway venv and read that
# JSON's training["versions"]["<tag>"]["registries"].
_SM_FRAMEWORK_ACCOUNTS = {
    "af-south-1": "510948584623", "ap-east-1": "651117190479",
    "ap-northeast-1": "354813040037", "ap-northeast-2": "366743142698",
    "ap-northeast-3": "867004704886", "ap-south-1": "720646828776",
    "ap-south-2": "628508329040", "ap-southeast-1": "121021644041",
    "ap-southeast-2": "783357654285", "ap-southeast-3": "951798379941",
    "ap-southeast-4": "106583098589", "ca-central-1": "341280168497",
    "ca-west-1": "190319476487", "cn-north-1": "450853457545",
    "cn-northwest-1": "451049120500", "eu-central-1": "492215442770",
    "eu-central-2": "680994064768", "eu-north-1": "662702820516",
    "eu-south-1": "978288397137", "eu-south-2": "104374241257",
    "eu-west-1": "141502667606", "eu-west-2": "764974769150", "eu-west-3": "659782779980",
    "il-central-1": "898809789911", "me-central-1": "272398656194",
    "me-south-1": "801668240914", "sa-east-1": "737474898029", "us-east-1": "683313688378",
    "us-east-2": "257758044811", "us-gov-east-1": "237065988967",
    "us-gov-west-1": "414596584902", "us-iso-east-1": "833128469047",
    "us-isob-east-1": "281123927165", "us-isof-east-1": "108575199400",
    "us-isof-south-1": "124985052026", "us-west-1": "746614075791",
    "us-west-2": "246618743249",
}
def _sm_region() -> str:
    return os.environ.get("AWS_REGION", _REGION)


def _sm(region: str = None):
    """SageMaker control-plane client from the shared factory."""
    return _aws.aws_client("sagemaker", region or _sm_region())


def _smr(region: str = None):
    """SageMaker runtime (invoke endpoint) client from the shared factory."""
    return _aws.aws_client("sagemaker-runtime", region or _sm_region())


def _sm_role_arn() -> str:
    """The SageMaker EXECUTION role ARN (jobs run AS this role). Injected at VM
    launch (SAGEMAKER_EXEC_ROLE_ARN) when setup_sagemaker.sh has run. Blank ⇒
    SageMaker not configured → raise a clear, actionable error."""
    arn = os.environ.get("SAGEMAKER_EXEC_ROLE_ARN", "")
    if not arn:
        raise RuntimeError(
            "SageMaker is not configured for this notebook: SAGEMAKER_EXEC_ROLE_ARN "
            "is unset. Provision the execution role with scripts/setup_sagemaker.sh "
            "and relaunch the proxy so it propagates the role into new VMs. Until "
            "then, use the in-VM train_model() helper instead."
        )
    return arn


def _sm_bucket() -> str:
    """Artifacts bucket for training IO (same bucket the other helpers use)."""
    b = os.environ.get("ARTIFACT_BUCKET", "")
    if not b:
        raise RuntimeError(
            "ARTIFACT_BUCKET is unset — cannot stage SageMaker training data. "
            "This should be injected at VM launch; relaunch the proxy."
        )
    return b


def _sm_prefix() -> str:
    p = os.environ.get("SAGEMAKER_ARTIFACT_PREFIX", "sagemaker/")
    return p if p.endswith("/") else p + "/"


# scikit-learn FRAMEWORK container (script mode), by framework version tag. The
# sklearn container runs OUR OWN training script (sm_scripts/sklearn_train.py) and
# writes back a COMPLETE sklearn Pipeline as a .joblib. That artifact loads with a
# plain joblib.load (no rewrap) and the existing serving container's .joblib path
# serves it unchanged. The chosen estimator (random_forest default, xgboost,
# lightgbm, ...) lives INSIDE that Pipeline — see _SKLEARN_ESTIMATORS.
#
# TAG FORMAT: "<fwver>-cpu-py3" (processors=['cpu'],
# py=['py3']), e.g. "1.9-0-cpu-py3". Confirmed by resolving via the SDK:
#   image_uris.retrieve('sklearn', region='us-west-2', version='1.9-0',
#                        image_scope='training') ->
#   246618743249.dkr.ecr.us-west-2.amazonaws.com/sagemaker-scikit-learn:1.9-0-cpu-py3
# To VERIFY/UPDATE the fwver, read it from the SDK's sklearn.json training versions
# (source of truth): available tags include 0.23-1, 1.0-1, 1.2-1, 1.4-2, 1.9-0.
#
# VERSION MUST MATCH THE READERS (this is NOT free cross-version tolerance). The
# artifact is a pickled Pipeline, and while LINEAR estimators + the encoders unpickle
# across minor versions with only an InconsistentVersionWarning, TREE ensembles do
# NOT: scikit-learn's Cython Tree struct changes between minors (e.g. 1.9 added a
# 'missing_go_to_left' field), so a RandomForest/GradientBoosting pickled by one
# minor FAILS to load on another with "node array ... incompatible dtype". So the
# training container's sklearn MUST equal the readers' sklearn. The VM reader and the
# serving container both pin scikit-learn==1.9.1 (see requirements.txt), so we pin the
# training image to the 1.9 line too — same reader==writer discipline the built-in
# XGBoost path uses. (An AWS e2e caught this: a 1.2-1 image trained fine but its
# RandomForest wouldn't load on the 1.9.1 reader.)
_SKLEARN_FRAMEWORK_VERSION = "1.9-0"

# The FIXED estimator menu for sklearn script mode. Script mode can't receive a live
# sklearn object (train_model's ergonomics), so sm_train takes a NAMED key and the
# entry-point script maps it to a concrete estimator (task-aware for the linear
# ones). Keys MUST match _build_estimator() in sm_scripts/sklearn_train.py. Mirrors
# train_model's RandomForest default for vocabulary coherence.
_SKLEARN_ESTIMATORS = ("random_forest", "gradient_boosting", "logistic", "linear",
                       "ridge", "xgboost", "lightgbm")


def _sklearn_image_uri(region: str) -> str:
    """Build the scikit-learn FRAMEWORK (script-mode) container image URI for the
    region, from the shared framework account map + the sagemaker-scikit-learn repo
    + '<fwver>-cpu-py3' tag. Raises a clear error for an unmapped region."""
    acct = _SM_FRAMEWORK_ACCOUNTS.get(region)
    if not acct:
        raise RuntimeError(
            f"No SageMaker framework image mapping for region {region!r}. Add it to "
            "_SM_FRAMEWORK_ACCOUNTS (see the SageMaker framework container docs) or "
            "run in a supported region."
        )
    return (
        f"{acct}.dkr.ecr.{region}.amazonaws.com/"
        f"sagemaker-scikit-learn:{_SKLEARN_FRAMEWORK_VERSION}-cpu-py3"
    )


def _sanitize_job_name(prefix: str) -> str:
    """A unique, IAM-scoping-compatible job/model name:
    'microvm-<prefix>-<ts>-<rand>'. Must match the exec role's
    arn:...:training-job/microvm-* scoping, so the 'microvm-' prefix is MANDATORY.
    SageMaker names: [a-zA-Z0-9-], <=63 chars.

    The timestamp is second-resolution, so a 4-char random suffix is appended to keep
    names unique when several jobs are submitted within the same second (e.g. a loop
    over estimators, or submitting a batch before polling) — without it SageMaker
    rejects the second job with ResourceInUse."""
    import uuid
    base = re.sub(r"[^a-zA-Z0-9-]", "-", str(prefix or "job")).strip("-").lower() or "job"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rand = uuid.uuid4().hex[:4]
    # Reserve room for the "-<rand>" suffix so truncation never drops it.
    name = f"microvm-{base}-{stamp}"[: 63 - 1 - len(rand)].rstrip("-")
    return f"{name}-{rand}"


class SageMakerJob:
    """Handle for a submitted SageMaker training job. Returned by sm_train() — the
    cell gets this back in ~seconds while training runs on managed compute. It
    carries what the terminal helpers need: the job name, task, and the S3 model
    output. The trained artifact is a SELF-CONTAINED sklearn Pipeline (the
    preprocessing is baked in), so there's no sibling preprocessor to carry. Pass it
    to sm_training_job_status(), sm_load_model(), predict()/predict_batch(), or
    sm_predict_batch()."""

    def __init__(self, job_name, task, model_s3_uri, preprocessor, feat_cols, target,
                 framework="sklearn"):
        self.job_name = job_name
        self.task = task
        self.framework = framework
        self.model_s3_uri = model_s3_uri
        # Vestigial: always None now (the artifact is a complete Pipeline). Kept as a
        # positional for call-site stability — see the pre_bundle=None returns in the
        # submit paths. Do NOT rely on it for a rewrap; there is nothing to rewrap.
        self._pre = preprocessor
        self._feat_cols = feat_cols
        self._target = target

    def status(self) -> str:
        """Cheap status check: 'InProgress (3m)' / 'Completed' / 'Failed: <reason>'."""
        return sm_training_job_status(self)

    def __repr__(self):
        return (f"<SageMakerJob {self.job_name} framework={self.framework} "
                f"task={self.task}>")


def _sklearn_feature_split(data, target, features, categorical, numeric):
    """Resolve (feat_cols, categorical, numeric) by dtype WITHOUT fitting anything —
    the fit happens remotely in the sklearn container. Used to render the
    categorical/numeric hyperparameter lists so the entry-point script builds a
    ColumnTransformer with the same split (object/bool/category one-hot, rest numeric)
    train_model would."""
    from pandas.api.types import is_numeric_dtype

    feat_cols = list(features) if features is not None else [c for c in data.columns if c != target]
    feat_cols = [c for c in feat_cols if c != target]
    if not feat_cols:
        raise ValueError("No feature columns — pass features=[...] or add columns.")
    X = data[feat_cols]
    if categorical is None:
        categorical = [c for c in feat_cols if X[c].dtype == bool or not is_numeric_dtype(X[c])]
    else:
        categorical = [c for c in categorical if c in feat_cols]
    if numeric is None:
        numeric = [c for c in feat_cols if c not in categorical]
    else:
        numeric = [c for c in numeric if c in feat_cols]
    return feat_cols, categorical, numeric


# ─── S3-pointer training input (sm_train from an S3 prefix / UnloadResult) ─────
# These let sm_train take a POINTER to data already in S3 (e.g. unload_athena_to_s3
# output) instead of an in-VM DataFrame — so the training set never materializes in
# the VM. The VM only reads a tiny SAMPLE of ONE file (to infer the categorical/
# numeric split) and hands SageMaker the S3 prefix; the training instance reads the
# full data. Both inputs run the SAME sklearn script-mode path, so every estimator
# (random_forest, xgboost, lightgbm, ...) works identically from a DataFrame or an
# S3 pointer — the only asymmetry is that the pointer path REQUIRES task= (the VM
# can't infer it without reading the data).

_DATA_FORMAT_EXT = {".parquet": "parquet", ".pq": "parquet", ".csv": "csv"}


def _s3_uri_of(src) -> str:
    """Normalize a training-input pointer to an s3:// URI string. Accepts an
    UnloadResult (has .s3_uri), a plain 's3://...' string, or anything whose str() is
    an s3 URI (os.PathLike via __fspath__)."""
    uri = getattr(src, "s3_uri", None) or str(src)
    if not uri.startswith("s3://"):
        raise ValueError(
            f"Expected an S3 URI (or UnloadResult) for the training data pointer, got "
            f"{uri!r}. Use unload_athena_to_s3(...) or pass 's3://bucket/prefix/'.")
    return uri


def _s3_split(uri: str):
    m = re.match(r"s3://([^/]+)/(.*)$", uri)
    if not m:
        raise ValueError(f"Bad S3 URI: {uri!r}")
    return m.group(1), m.group(2)


def _is_prefix_noise(key: str) -> bool:
    """True for S3 keys under a training prefix that are NOT data: directory markers,
    the Athena UNLOAD manifest/metadata, and our own sourcedir upload. These must be
    skipped when listing data files so an extensionless-but-real data object isn't
    crowded out (or a manifest mistaken for data)."""
    base = os.path.basename(key.rstrip("/"))
    if key.endswith("/") or base == "":
        return True
    low = base.lower()
    # Athena UNLOAD drops a manifest/metadata sidecar; our packager writes source/.
    if low.endswith("-manifest") or low.endswith("manifest.csv") \
            or low.endswith(".metadata") or low == "sourcedir.tar.gz":
        return True
    if "/source/" in key or key.endswith("/source"):
        return True
    return False


def _list_prefix_data_files(uri: str, region: str, limit: int = 1000,
                            data_format: str = None):
    """List the data files under an S3 prefix (or a single-file URI). Returns
    (files, detected_format) where files is a list of 's3://...' keys and
    detected_format is 'parquet'|'csv'|None.

    Extension is used when present, but Athena UNLOAD writes EXTENSIONLESS objects
    (a bare query-id hash), so when the extension doesn't resolve the format we fall
    back to EVERY non-noise object under the prefix and lean on `data_format` (an
    explicit override, or the UNLOAD manifest's known format passed by the caller).
    Only if the format is still unknown AND nothing has a recognized extension do we
    report no files — the sampler then raises an actionable error."""
    bucket, key = _s3_split(uri)
    # A single-file URI with a known extension — honor it directly.
    if key and not key.endswith("/") and os.path.splitext(key)[1].lower() in _DATA_FORMAT_EXT:
        ext = os.path.splitext(key)[1].lower()
        return [uri], _DATA_FORMAT_EXT[ext]

    ext_files, ext_fmt = [], None   # objects whose extension names a known format
    all_files = []                  # every non-noise object (extension or not)
    for o in _s3.list_objects(bucket, key, region=region)[0]:
        k = o["Key"]
        if _is_prefix_noise(k):
            continue
        all_files.append(f"s3://{bucket}/{k}")
        ext = os.path.splitext(k)[1].lower()
        if ext in _DATA_FORMAT_EXT:
            ext_files.append(f"s3://{bucket}/{k}")
            if ext_fmt is None:
                ext_fmt = _DATA_FORMAT_EXT[ext]

    # Prefer extension-typed files when we found any (and the caller didn't force a
    # conflicting format). Otherwise use ALL objects, with the format from the
    # explicit override — this is the Athena-UNLOAD (extensionless) path.
    if ext_files and (data_format is None or data_format == ext_fmt):
        return ext_files[:limit], ext_fmt
    if data_format:
        return all_files[:limit], data_format.lower()
    # No extension matched and no override: hand back whatever is there (format
    # unknown) so the sampler can sniff or raise a clear, actionable error.
    return all_files[:limit], ext_fmt


def _sample_s3_schema(uri: str, region: str, data_format: str = None, n: int = 100):
    """Read a SMALL random sample from ONE file under the S3 prefix to learn the
    columns + dtypes — WITHOUT materializing the dataset in the VM. Returns
    (columns, sample_df). Reads one file, then df.sample(n) (random WITHIN the file,
    Option A) so dtype/cat-num inference isn't biased by the head of the file.

    data_format forces 'parquet'|'csv'; otherwise it's detected from the file
    extension, and — for extensionless Athena UNLOAD output — SNIFFED from the first
    file's magic bytes. Raises a clear error if no data files are found under the
    prefix."""
    files, detected = _list_prefix_data_files(uri, region, data_format=data_format)
    if not files:
        raise FileNotFoundError(
            f"No data files found under {uri!r}. If the data is there with a "
            "different layout, pass data_format='csv'|'parquet'.")
    bucket, key = _s3_split(files[0])
    blob = _s3.get_bytes(bucket, key, region=region)
    # Resolve format: explicit override > extension-detected > byte-sniff. A parquet
    # file begins (and ends) with the 4-byte magic 'PAR1'; anything else we treat as
    # CSV text. This is what lets an EXTENSIONLESS Athena UNLOAD parquet object load.
    fmt = (data_format or detected or _sniff_data_format(blob)).lower()
    if fmt == "parquet":
        df = pd.read_parquet(io.BytesIO(blob))
    else:
        # Cap the CSV parse so a huge single file doesn't balloon the VM — we only
        # need enough rows to infer types. nrows is generous but bounded.
        df = pd.read_csv(io.BytesIO(blob), nrows=max(n * 50, 5000))
    if len(df) > n:
        df = df.sample(n=n, random_state=0)
    return list(df.columns), df, fmt


def _sniff_data_format(blob: bytes) -> str:
    """Best-effort format sniff from a file's bytes, for extensionless Athena UNLOAD
    output. Parquet files start with the 4-byte magic 'PAR1'; otherwise assume CSV."""
    return "parquet" if blob[:4] == b"PAR1" else "csv"


def _sklearn_feature_split_from_sample(sample_df, target, features, categorical, numeric):
    """Resolve (feat_cols, categorical, numeric) from a SAMPLE DataFrame — the same
    logic as _sklearn_feature_split but driven by the sampled schema instead of the
    full data. Explicit features/categorical/numeric override the auto-split."""
    return _sklearn_feature_split(sample_df, target, features, categorical, numeric)


def _package_sourcedir(region: str, bucket: str, prefix: str) -> str:
    """Package the sklearn entry-point script as sourcedir.tar.gz and upload it to
    S3, returning the S3 URI. SCRIPT MODE: the generic sagemaker-scikit-learn
    container has no model logic — it downloads this tarball (sagemaker_submit_directory)
    and runs sklearn_train.py (sagemaker_program) as the training command. We package
    on-demand from the copy shipped INSIDE the VM (app/notebook/helpers/sm_scripts/),
    so the uploaded script is always the one running in this VM — no install-time
    pre-stage, no drift."""
    script_dir = os.path.join(os.path.dirname(__file__), "sm_scripts")
    entry = os.path.join(script_dir, "sklearn_train.py")
    if not os.path.exists(entry):
        raise RuntimeError(
            f"sklearn entry-point script not found at {entry!r}. The VM image may be "
            "stale (missing sm_scripts/); rebuild the images so app/ ships the script."
        )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Add at archive root as 'sklearn_train.py' (sagemaker_program is the basename).
        tar.add(entry, arcname="sklearn_train.py")
        # A requirements.txt in the source dir is pip-installed by the framework
        # container BEFORE the script runs — this is how estimator='xgboost' gets
        # xgboost into the sklearn container. Ship it when present (archive root, the
        # path SageMaker looks for) so training-only extra deps are available at fit.
        reqs = os.path.join(script_dir, "requirements.txt")
        if os.path.exists(reqs):
            tar.add(reqs, arcname="requirements.txt")
    buf.seek(0)
    key = f"{prefix}/source/sourcedir.tar.gz"
    _s3.put_bytes(bucket, key, buf.getvalue(), content_type="application/x-tar",
                  region=region)
    return f"s3://{bucket}/{key}"


def _submit_sklearn_job(data, target, features, categorical, numeric, task, estimator,
                        scale, job_name, prefix, output_path, bucket, region, role,
                        instance, n_estimators, max_depth, C, alpha):
    """Submit a scikit-learn FRAMEWORK (script-mode) training job. Unlike XGBoost we
    do NOT encode in the VM: we upload the RAW CSV (header + target column) and let
    OUR entry-point script build+fit the full Pipeline(preprocess -> estimator) in
    the sklearn container, writing a self-contained model.joblib. Returns
    (model_s3_uri, pre_bundle=None, feat_cols) — pre_bundle is None because the
    Pipeline already contains its preprocessing (nothing to rewrap at load time)."""
    estimator = (estimator or "random_forest").lower()
    if estimator not in _SKLEARN_ESTIMATORS:
        raise ValueError(
            f"estimator={estimator!r} is not in the sklearn menu. Choose one of "
            f"{', '.join(_SKLEARN_ESTIMATORS)} (random_forest is the default)."
        )

    feat_cols, categorical, numeric = _sklearn_feature_split(
        data, target, features, categorical, numeric)

    # Upload RAW training data: header row + the target column intact. The script
    # reads the train channel and splits off --target itself.
    cols = feat_cols + [target]
    csv_buf = io.StringIO()
    data[cols].to_csv(csv_buf, header=True, index=False)
    train_key = f"{prefix}/input/train.csv"
    _s3.put_bytes(bucket, train_key, csv_buf.getvalue().encode("utf-8"),
                  content_type="text/csv", region=region)

    model_s3_uri = _sklearn_create_job(
        job_name, prefix, output_path, bucket, region, role, instance,
        train_s3_uri=f"s3://{bucket}/{train_key}", content_type="text/csv",
        target=target, task=task, estimator=estimator, categorical=categorical,
        numeric=numeric, scale=scale, n_estimators=n_estimators, max_depth=max_depth,
        C=C, alpha=alpha)
    # No sibling preprocessor: the artifact is a COMPLETE Pipeline. pre_bundle=None
    # signals the sklearn load path (plain joblib.load, no rewrap).
    return model_s3_uri, None, feat_cols


def _sklearn_create_job(job_name, prefix, output_path, bucket, region, role, instance,
                        *, train_s3_uri, content_type, target, task, estimator,
                        categorical, numeric, scale, n_estimators, max_depth, C, alpha):
    """Shared sklearn script-mode job submitter: package the entry-point, render the
    script-mode HyperParameters, and create the training job pointing the `train`
    channel at `train_s3_uri` (a VM-uploaded CSV for the DataFrame path, OR the user's
    own S3 prefix for the pointer path — SageMaker reads it either way). Returns the
    model.tar.gz S3 URI."""
    sourcedir_uri = _package_sourcedir(region, bucket, prefix)

    # Script-mode HyperParameters. SageMaker's framework container reads the reserved
    # sagemaker_* keys to locate+run the script; the rest are passed to our script as
    # `--key value` CLI args (the SM toolkit converts hyperparameter name->flag). We
    # JSON-encode the reserved values (SM convention: they're quoted JSON strings) and
    # pass our own flags as plain strings. List columns go as comma-joined strings.
    hp = {
        "sagemaker_program": json.dumps("sklearn_train.py"),
        "sagemaker_submit_directory": json.dumps(sourcedir_uri),
        "target": target,
        "task": task,
        "estimator": estimator,
        "categorical": ",".join(categorical),
        "numeric": ",".join(numeric),
        "scale": "true" if scale else "false",
        "n-estimators": str(int(n_estimators)),
        "max-depth": str(int(max_depth)),
        "C": str(float(C)),
        "alpha": str(float(alpha)),
    }

    sm = _sm(region)
    sm.create_training_job(
        TrainingJobName=job_name,
        AlgorithmSpecification={"TrainingImage": _sklearn_image_uri(region),
                                "TrainingInputMode": "File"},
        RoleArn=role,
        InputDataConfig=[{
            "ChannelName": "train",
            "DataSource": {"S3DataSource": {
                "S3DataType": "S3Prefix",
                "S3Uri": train_s3_uri,
                "S3DataDistributionType": "FullyReplicated"}},
            "ContentType": content_type,
        }],
        OutputDataConfig={"S3OutputPath": output_path},
        ResourceConfig={"InstanceType": instance, "InstanceCount": 1, "VolumeSizeInGB": 10},
        StoppingCondition={"MaxRuntimeInSeconds": 3600},
        HyperParameters=hp,
    )
    return f"{output_path}/{job_name}/output/model.tar.gz"


def _submit_sklearn_job_from_s3(train_uri, target, features, categorical, numeric, task,
                                estimator, scale, data_format, job_name, prefix,
                                output_path, bucket, region, role, instance,
                                n_estimators, max_depth, C, alpha):
    """Submit a sklearn script-mode job whose training data is ALREADY in S3 (an
    unload_athena_to_s3 result or any s3:// prefix) — the data NEVER enters the VM.
    The VM reads only a tiny sample of ONE file to infer the categorical/numeric split
    (explicit lists override), then points the training channel's S3Prefix straight at
    the user's URI. Returns (model_s3_uri, None, feat_cols)."""
    # Infer the schema from a small random sample of one file (Option A) — explicit
    # features/categorical/numeric override the auto-split.
    _cols, sample_df, fmt = _sample_s3_schema(train_uri, region, data_format=data_format)
    if target not in sample_df.columns:
        raise KeyError(
            f"target column {target!r} is not in the data at {train_uri} "
            f"(sampled columns: {list(sample_df.columns)}).")
    feat_cols, categorical, numeric = _sklearn_feature_split_from_sample(
        sample_df, target, features, categorical, numeric)

    content_type = "application/x-parquet" if fmt == "parquet" else "text/csv"
    model_s3_uri = _sklearn_create_job(
        job_name, prefix, output_path, bucket, region, role, instance,
        train_s3_uri=train_uri, content_type=content_type,
        target=target, task=task, estimator=estimator, categorical=categorical,
        numeric=numeric, scale=scale, n_estimators=n_estimators, max_depth=max_depth,
        C=C, alpha=alpha)
    return model_s3_uri, None, feat_cols


# Training FRAMEWORKS sm_train can dispatch to. Today only "sklearn" (framework
# SCRIPT MODE — our entry-point builds a complete Pipeline(preprocess -> estimator)
# and writes a self-contained .joblib that plain joblib.load reads and the serving
# container serves unchanged; the model is chosen via estimator=). This is a SEPARATE
# axis from estimator=: framework= is the training paradigm/container (future values
# like "pytorch"/"autopilot" would be new backends), estimator= is which model within
# the sklearn framework.
_SUPPORTED_FRAMEWORKS = ("sklearn",)


def sm_train(df, target: str, *, features=None, task: str = None,
             framework: str = "sklearn", estimator: str = "random_forest",
             categorical=None, numeric=None, scale: bool = False,
             data_format: str = None,
             instance: str = None, max_depth: int = 6,
             n_estimators: int = 100, C: float = 1.0, alpha: float = 1.0,
             wait: bool = False) -> "SageMakerJob":
    """
    Train a model on SageMaker-managed compute — the scale-out counterpart of the
    in-VM train_model(). NON-BLOCKING: uploads the data, submits the job, and
    RETURNS A HANDLE in seconds (training then runs on its own instance). Use this
    when the data is too big for the VM, you want a bigger/GPU instance, or training
    would exceed a cell's time; for small/fast tabular work, prefer train_model()
    (instant, in-VM).

        job = sm_train(df, "churn")                          # RandomForest (default)
        job = sm_train(df, "churn", estimator="xgboost")     # XGBoost Pipeline
        job = sm_train(df, "churn", estimator="lightgbm")    # LightGBM Pipeline
        job = sm_train(df, "price", task="regression",
                       estimator="ridge", scale=True)        # scaled Ridge regression
        job.status()                                          # check progress
        model = sm_load_model(job); preds = predict(model, new_rows)   # then predict

    TRAIN FROM DATA ALREADY IN S3 (no VM memory used) — pass a POINTER instead of a
    DataFrame: an UnloadResult (from unload_athena_to_s3) or an 's3://...' prefix. The
    training set is read by the SageMaker instance, NEVER pulled into the VM; the VM
    only samples ONE file to infer the feature split. This is how you train on data
    too big to fit the VM at all:

        prep = unload_athena_to_s3('''SELECT ... FROM big_db.t JOIN ...''')  # → S3
        job  = sm_train(prep, "churn", task="classification")            # RandomForest
        job  = sm_train("s3://bucket/prefix/", "churn",                  # bare prefix
                        task="classification", estimator="xgboost")
        job  = sm_train(prep, "price", task="regression", estimator="lightgbm")

    Every estimator works identically from a DataFrame OR an S3 pointer (same
    script-mode path). The ONLY asymmetry: the pointer path REQUIRES task= (the VM
    can't infer it without reading the data).

    Args:
      df          the TRAINING DATA — polymorphic: a pandas DataFrame (train from
                  in-VM data), OR a pointer to data already in S3 (an UnloadResult
                  from unload_athena_to_s3, or an 's3://bucket/prefix/' string). The
                  pointer path keeps the data OUT of the VM entirely.
      target      column to predict. For a DataFrame, task is inferred from it or
                  forced via task=. For an S3 pointer, task= is REQUIRED.
      framework   the training paradigm / managed container. Today only "sklearn"
                  (default): framework SCRIPT MODE — runs a shipped entry-point in
                  AWS's scikit-learn container and returns a COMPLETE sklearn
                  Pipeline(preprocess -> estimator) as a .joblib (plain joblib.load,
                  served unchanged). This is a separate axis from estimator= and is
                  where future backends (e.g. deep-learning) would plug in.
      estimator   which model the Pipeline uses. One of random_forest (default),
                  gradient_boosting, logistic, linear, ridge, xgboost, lightgbm. The
                  gradient-boosting trio (gradient_boosting / xgboost / lightgbm) are
                  the strong tabular defaults; xgboost and lightgbm are their own
                  libraries, installed into the training container on demand and
                  lazy-installed in the VM at load. Task-aware for the linear ones:
                  logistic/linear -> LogisticRegression (classification) or
                  LinearRegression (regression); ridge -> RidgeClassifier or Ridge.
      features    columns to train on (default: all except target).
      categorical/numeric  feature split (default: auto — object/bool/category are
                  one-hot encoded, the rest numeric). SAME encoding as train_model,
                  applied INSIDE the Pipeline on the training instance. You feed RAW
                  rows to predict() later.
      scale       sklearn-only: if True, StandardScaler the numeric columns inside
                  the Pipeline (default False — numerics pass through). Helps the
                  linear/ridge/logistic estimators; harmless-to-unhelpful for trees.
      data_format S3-pointer path only: 'csv' or 'parquet'. Default auto-detects from
                  the file extensions under the prefix (UNLOAD writes parquet by
                  default). Set it if the files have an unusual/absent extension.
      instance    training instance type (default: env SAGEMAKER_TRAIN_INSTANCE or
                  ml.m5.xlarge). Use a g-family type for GPU.
      n_estimators / max_depth  trees + tree depth for the tree estimators
                  (random_forest / gradient_boosting / xgboost / lightgbm; defaults
                  100 / 6).
      C / alpha   linear knobs: C is inverse-regularization for logistic (default
                  1.0); alpha is the ridge penalty (default 1.0).
      wait        if True, block-poll until the job finishes (default False —
                  returns immediately; poll via job.status()).

    Returns a SageMakerJob handle. Pass it to sm_load_model() or
    predict()/predict_batch() (predict_batch also accepts it directly).
    """
    from .ml import _infer_task

    framework = (framework or "sklearn").lower()
    if framework not in _SUPPORTED_FRAMEWORKS:
        raise ValueError(
            f"framework={framework!r} is not supported. The only framework today is "
            "'sklearn' (framework script mode — a full scikit-learn Pipeline; pick "
            f"the model via estimator=, one of {', '.join(_SKLEARN_ESTIMATORS)})."
        )
    estimator = (estimator or "random_forest").lower()
    if estimator not in _SKLEARN_ESTIMATORS:
        raise ValueError(
            f"estimator={estimator!r} is not in the menu. Choose one of "
            f"{', '.join(_SKLEARN_ESTIMATORS)} (random_forest is the default)."
        )

    region = _sm_region()
    role = _sm_role_arn()
    bucket = _sm_bucket()
    instance = instance or os.environ.get("SAGEMAKER_TRAIN_INSTANCE", "ml.m5.xlarge")

    # The first argument is POLYMORPHIC: a DataFrame (train from in-VM data) OR a
    # pointer to data already in S3 — an UnloadResult (unload_athena_to_s3 output) or
    # an 's3://...' prefix string. The S3-pointer path keeps the training set OUT of
    # the VM entirely (the VM only samples one file to infer the feature split).
    # Duck-typed so we don't import UnloadResult from readers (sibling module —
    # avoids a cross-helper import cycle): a pointer is an s3:// string, something
    # with an .s3_uri attribute (UnloadResult), or an os.PathLike whose str() is s3.
    is_pointer = (
        (isinstance(df, str) and df.startswith("s3://"))
        or isinstance(getattr(df, "s3_uri", None), str)
        or (hasattr(df, "__fspath__") and str(df).startswith("s3://"))
    )
    if not is_pointer and not isinstance(df, pd.DataFrame):
        raise TypeError(
            "sm_train's first argument must be a pandas DataFrame, an s3:// URI "
            "string, or an UnloadResult (from unload_athena_to_s3).")

    job_name = _sanitize_job_name(target)
    prefix = f"{_sm_prefix()}{job_name}"
    output_path = f"s3://{bucket}/{prefix}/output"

    if is_pointer:
        # --- S3-POINTER PATH (data stays in S3; VM never materializes it) ----------
        if not task:
            raise ValueError(
                "task= is REQUIRED when training from an S3 pointer — the VM can't "
                "infer it without reading the whole dataset. Pass "
                "task='classification' or task='regression'.")
        task = task.lower()
        if task not in ("classification", "regression"):
            raise ValueError("task must be 'classification' or 'regression'.")
        train_uri = _s3_uri_of(df)
        model_s3_uri, pre_bundle, feat_cols = _submit_sklearn_job_from_s3(
            train_uri, target, features, categorical, numeric, task, estimator, scale,
            data_format, job_name, prefix, output_path, bucket, region, role, instance,
            n_estimators, max_depth, C, alpha)
        n_rows_note = "streamed from S3"
    else:
        # --- IN-VM DATAFRAME PATH --------------------------------------------------
        if target not in df.columns:
            raise KeyError(f"target column {target!r} is not in the DataFrame.")
        data = df.dropna(subset=[target])
        if len(data) == 0:
            raise ValueError(f"No rows left after dropping missing {target!r} values.")
        task = (task or _infer_task(data[target])).lower()
        if task not in ("classification", "regression"):
            raise ValueError("task must be 'classification' or 'regression'.")
        model_s3_uri, pre_bundle, feat_cols = _submit_sklearn_job(
            data, target, features, categorical, numeric, task, estimator, scale,
            job_name, prefix, output_path, bucket, region, role, instance,
            n_estimators, max_depth, C, alpha)
        n_rows_note = f"{len(data)} rows"

    job = SageMakerJob(job_name, task, model_s3_uri, pre_bundle, feat_cols, target,
                       framework=framework)
    logger.info(f"[sagemaker] submitted training job {job_name} (framework={framework}, "
                f"estimator={estimator}, task={task}, instance={instance}, "
                f"{n_rows_note}) → {model_s3_uri}")

    print(f"🚀 SageMaker training job submitted: {job_name}\n"
          f"   estimator={estimator}  task={task}  instance={instance}  data={n_rows_note}\n"
          f"   Runs on managed compute (~a few minutes). This did NOT block your VM.\n"
          f"   Check progress:   job.status()\n"
          f"   When Completed:   model = sm_load_model(job); predict(model, new_rows)\n"
          f"                     or  scored = predict_batch(job, big_df)")

    if wait:
        _sm_wait(job.job_name, region)
    return job


def _sm_describe(job_name: str, region: str = None) -> dict:
    region = region or _sm_region()
    sm = _sm(region)
    return sm.describe_training_job(TrainingJobName=job_name)


def sm_training_job_status(job) -> str:
    """Report a training job's status in one human line:
    'InProgress (3m elapsed)' / 'Completed (2m)' / 'Failed: <reason>' / 'Stopped'.
    Cheap — safe to call repeatedly in a cell while the job runs."""
    name = job.job_name if isinstance(job, SageMakerJob) else str(job)
    d = _sm_describe(name)
    status = d.get("TrainingJobStatus", "Unknown")
    secondary = d.get("SecondaryStatus", "")
    start = d.get("TrainingStartTime")
    end = d.get("TrainingEndTime")
    try:
        if start and end:
            mins = int((end - start).total_seconds() // 60)
        elif start:
            import datetime as _dt
            mins = int((_dt.datetime.now(start.tzinfo) - start).total_seconds() // 60)
        else:
            mins = 0
    except Exception:
        mins = 0
    if status == "Failed":
        return f"Failed: {d.get('FailureReason', 'unknown reason')}"
    if status == "Completed":
        return f"Completed ({mins}m)"
    if status == "Stopped":
        return "Stopped"
    return f"{status} — {secondary} ({mins}m elapsed)"


def _sm_wait(job_name: str, region: str = None, timeout_s: int = 1800,
             poll_s: int = 20) -> dict:
    """Block-poll a training job until terminal or timeout. Returns the final
    describe dict. Raises on Failed/Stopped or if it doesn't finish in time (the
    job keeps running server-side — call sm_load_model again later)."""
    region = region or _sm_region()
    deadline = time.time() + timeout_s
    while True:
        d = _sm_describe(job_name, region)
        status = d.get("TrainingJobStatus", "Unknown")
        if status == "Completed":
            return d
        if status in ("Failed", "Stopped"):
            raise RuntimeError(
                f"SageMaker training job {job_name} {status}: "
                f"{d.get('FailureReason', 'no reason given')}")
        if time.time() >= deadline:
            raise TimeoutError(
                f"Training job {job_name} still {status} after {timeout_s}s. It is "
                f"NOT cancelled — it keeps running on SageMaker. Check job.status() "
                f"and call sm_load_model(job) again once it shows Completed.")
        time.sleep(poll_s)


# The gradient-boosting estimators (estimator='xgboost'/'lightgbm') are their own
# libraries. They are installed INTO the training container on demand (sm_scripts/
# requirements.txt) and are NOT baked into the VM image, so loading such a model back
# into the VM (joblib.load unpickles the embedded estimator) needs the library
# importable HERE too. These lazy-install it on first load, PINNED to the serving
# container's major.minor (serving/requirements.txt: xgboost-cpu / lightgbm 3.2.* /
# 4.*) so the VM reader is the same format family as the training-instance writer.
# A fresh VM has neither, so this is a clean first-install, never a downgrade.
_XGBOOST_PIN = "xgboost==3.2.*"
_LIGHTGBM_PIN = "lightgbm==4.*"


def _ensure_xgboost():
    """Make xgboost importable in the VM, installing the pinned version on first use.
    Returns the imported module."""
    from .packages import install_package
    try:
        import xgboost  # noqa: F401
        return xgboost
    except ImportError:
        install_package(_XGBOOST_PIN)
        import xgboost  # noqa: F811
        return xgboost


def _ensure_lightgbm():
    """Make lightgbm importable in the VM, installing the pinned version on first use.
    Returns the imported module."""
    from .packages import install_package
    try:
        import lightgbm  # noqa: F401
        return lightgbm
    except ImportError:
        install_package(_LIGHTGBM_PIN)
        import lightgbm  # noqa: F811
        return lightgbm


def _fetch_model_tar_members(model_s3_uri: str, region: str):
    """Download model.tar.gz and return (tar_bytes, member_basenames). Lets callers
    confirm the sklearn Pipeline member ('model.joblib') is present without a second
    download."""
    m = re.match(r"s3://([^/]+)/(.+)", model_s3_uri)
    if not m:
        raise ValueError(f"Bad model S3 URI: {model_s3_uri!r}")
    bucket, key = m.group(1), m.group(2)
    blob = _s3.get_bytes(bucket, key, region=region)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = [os.path.basename(mm.name) for mm in tar.getmembers() if mm.isfile()]
    return blob, names


def _load_sklearn_pipeline_from_tar(tar_bytes: bytes):
    """Extract model.joblib from an in-memory model.tar.gz and joblib.load it. The
    sklearn script-mode artifact IS a complete, self-contained Pipeline(preprocess ->
    estimator) — so this returns a ready predictor with NO rewrap and NO sibling
    preprocessor. Lazily ensures joblib is importable (it's a sklearn dep, present
    once sklearn is installed in the VM)."""
    import warnings
    import joblib
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        members = [mm for mm in tar.getmembers() if mm.isfile()]
        chosen = next(
            (mm for mm in members if os.path.basename(mm.name) == "model.joblib"),
            None,
        )
        if chosen is None:
            raise RuntimeError(
                "Expected 'model.joblib' inside the sklearn model artifact but found "
                f"{[os.path.basename(mm.name) for mm in members]}. The job may not be "
                "a sklearn script-mode job."
            )
        data = tar.extractfile(chosen).read()
    # Suppress sklearn's InconsistentVersionWarning on load. The AWS framework
    # container's newest sklearn tag is 1.9-0 (ships 1.9.0) while the VM/serving pin
    # is 1.9.1 — an unavoidable PATCH-level drift (no 1.9-1 framework tag exists). A
    # patch gap is safe for stock estimators (sklearn's own guidance; our AWS e2e
    # confirmed identical predictions), so the per-estimator warning is pure noise
    # here — one line per pipeline step floods the cell output. We scope the filter
    # tightly to THIS warning class around THIS load, so any genuine incompatibility
    # of a different kind still surfaces.
    with warnings.catch_warnings():
        try:
            from sklearn.exceptions import InconsistentVersionWarning
            warnings.simplefilter("ignore", InconsistentVersionWarning)
        except Exception:  # noqa: BLE001 — older sklearn without the class: fall back
            warnings.filterwarnings("ignore", message="Trying to unpickle estimator")
        try:
            return joblib.load(io.BytesIO(data))
        except ModuleNotFoundError as e:
            # The Pipeline may embed an estimator whose library isn't in the VM —
            # estimator='xgboost'/'lightgbm' pickles an estimator that needs its
            # library importable to UNPICKLE (the VM is the reader; the training
            # instance was the writer). Neither is baked into the image, so
            # lazy-install the pinned version and retry ONCE.
            ensure = {"xgboost": _ensure_xgboost, "lightgbm": _ensure_lightgbm}.get(
                e.name or "")
            if ensure is not None:
                ensure()
                return joblib.load(io.BytesIO(data))
            raise


def sm_load_model(job, *, timeout_s: int = 1800):
    """
    Pull a finished SageMaker model back into the VM as a ready-to-use predictor.
    WAITS (bounded) if the job is still training. Returns a COMPLETE scikit-learn
    Pipeline (preprocess -> estimator) — so you feed it RAW rows exactly like a
    train_model() model:

        model = sm_load_model(job)
        preds = predict(model, new_rows)          # your existing helper
        scored = predict_batch(model, new_rows, output_col="pred")

    The artifact is a self-contained Pipeline .joblib (plain joblib.load — no rewrap).
    If the Pipeline's estimator is xgboost/lightgbm, the matching library is
    lazy-installed in the VM on load (not baked in). To score a whole table in one
    call, pass the job straight to predict_batch(job, df) (or sm_predict_batch(job,
    df)) — it loads the model once and scores locally. Raises if the job
    Failed/Stopped or hasn't finished within timeout_s (it keeps running — retry).

    Also accepts an S3 URI string (the model.tar.gz key, e.g. the one shown in the
    Data Sources panel) instead of a job handle:

        model = sm_load_model('s3://bucket/sagemaker/<job>/output/<job>/output/model.tar.gz')

    so a model found in the panel (with no `job` in scope) is directly usable."""
    region = _sm_region()

    # From-URI path: load straight from the S3 artifact (panel use-case).
    if isinstance(job, str):
        model_s3_uri = job
        tar_bytes, _names = _fetch_model_tar_members(model_s3_uri, region)
        model = _load_sklearn_pipeline_from_tar(tar_bytes)
        logger.info(f"[sagemaker] loaded sklearn Pipeline from artifact {model_s3_uri}")
        return model

    if not isinstance(job, SageMakerJob):
        raise TypeError(
            "sm_load_model expects a SageMakerJob (from sm_train) or an S3 URI string "
            "to a model.tar.gz."
        )
    _sm_wait(job.job_name, region, timeout_s=timeout_s)
    tar_bytes, _names = _fetch_model_tar_members(job.model_s3_uri, region)
    model = _load_sklearn_pipeline_from_tar(tar_bytes)
    logger.info(f"[sagemaker] loaded sklearn Pipeline for {job.job_name} ({job.task})")
    return model


# NOTE: the former sm_batch_predict(job, df) has been FOLDED INTO sm_predict_batch,
# which now accepts a job OR an endpoint (and predict_batch routes to it). One batch
# surface, no near-duplicate name (sm_batch_predict vs sm_predict_batch) to confuse.


def _sm_list_pages(call, result_key: str, max_results: int, **kwargs) -> list:
    """Follow NextToken until max_results items were collected or the listing ends.
    SageMaker caps one page at 100, so a larger max_results needs more than one call."""
    want = max(1, int(max_results))
    items, token = [], None
    while len(items) < want:
        page_kwargs = dict(kwargs, MaxResults=min(100, want - len(items)))
        if token:
            page_kwargs["NextToken"] = token
        resp = call(**page_kwargs)
        items.extend(resp.get(result_key, []))
        token = resp.get("NextToken")
        if not token:
            break
    return items[:want]


def sm_jobs(max_results: int = 10, *, since=None) -> "pd.DataFrame":
    """
    List this account's recent SageMaker training jobs (name, status, created) as a
    DataFrame — a quick way to see what you've submitted and whether it's done.
    Only jobs this notebook can see (microvm-* under the exec role) are meaningful.

        sm_jobs()                          # most recent jobs
        sm_jobs(since="2026-09-01")        # only jobs submitted on/after Sep 1
        sm_jobs(since=datetime(2026, 9, 1))

    since (optional): keep only jobs whose CreationTime is on/after this moment.
    Accepts a "YYYY-MM-DD" (or ISO datetime) string, a datetime, or a pandas
    Timestamp. Filtered SERVER-SIDE via CreationTimeAfter, so max_results applies to
    the already-filtered set (raise it if you expect many jobs in the window).
    """
    region = _sm_region()
    sm = _sm(region)
    kwargs = dict(SortBy="CreationTime", SortOrder="Descending")
    after = _coerce_datetime(since)
    if after is not None:
        kwargs["CreationTimeAfter"] = after
    jobs = _sm_list_pages(sm.list_training_jobs, "TrainingJobSummaries", max_results,
                          **kwargs)
    rows = [{
        "job_name": j.get("TrainingJobName"),
        "status": j.get("TrainingJobStatus"),
        "created": j.get("CreationTime"),
        "ended": j.get("TrainingEndTime"),
    } for j in jobs]
    return pd.DataFrame(rows)


def _coerce_datetime(value):
    """Normalize a user-supplied date/datetime/string into a datetime for boto3's
    CreationTimeAfter filter (boto3 accepts a datetime or an ISO/epoch value). Returns
    None for None. Accepts a datetime, a pandas Timestamp, or a string like
    '2026-09-01' or '2026-09-01T12:00:00'. Raises a clear error on an unparseable
    value so the user gets actionable feedback instead of a cryptic boto3 error."""
    if value is None:
        return None
    import datetime as _dt
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    # pandas Timestamp (has to_pydatetime) or anything datetime-like.
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if isinstance(value, str):
        try:
            return pd.to_datetime(value).to_pydatetime()
        except Exception as e:
            raise ValueError(
                f"Could not parse since={value!r} as a date. Use 'YYYY-MM-DD', an ISO "
                f"datetime string, or a datetime object."
            ) from e
    raise TypeError(
        f"since must be a date/datetime/Timestamp or a date string, got "
        f"{type(value).__name__}."
    )


# =============================================================================
# MACHINE LEARNING / SAGEMAKER — real-time ENDPOINTS (Phase 2, serverless)
# =============================================================================
#   (endpoints) Promote a trained model to a STANDING HTTPS inference endpoint that
# outlives the VM and is callable from any notebook by name. Unlike Phase 1 (train →
# score), an endpoint is a persistent account resource — but on SERVERLESS it costs
# $0 while idle (auto scales to zero) and needs no start/stop: just deploy + delete.
#
# HOW IT SERVES (verified end-to-end): one custom container (SAGEMAKER_SERVING_IMAGE)
# handles BOTH a scikit-learn model (.joblib → .predict) and an ONNX graph (.onnx →
# onnxruntime). We deliberately do NOT use the stock SageMaker SKLearn/Triton
# containers: on Serverless the SKLearn container fails writing /etc (read-only root
# fs) and the ONNX-capable Triton image is >10GB / missing the onnxruntime backend.
# Our container binds gunicorn directly on :8080 (no nginx) and writes only to /tmp,
# so it survives serverless. sm_deploy_serverless / sm_deploy_instance just package
# the artifact + point this image at it — the container dispatches on artifact type.
#
# COST SHAPE: serverless bills per-request (+ data), $0 when idle — no "stop" needed,
# lifecycle is just sm_deploy_serverless (create) + sm_delete_endpoint (cleanup).
# INSTANCE endpoints bill per instance-hour while running, so they add stop/start:
# sm_deploy_instance (create) → sm_stop_endpoint (pause, keep config+model) →
# sm_start_endpoint (resume) → sm_delete_endpoint (permanent teardown).


def _serving_image() -> str:
    """The custom serving container URI (SAGEMAKER_SERVING_IMAGE), injected at VM
    launch alongside the exec role. Handles both .joblib and .onnx artifacts. Blank
    ⇒ the serving image wasn't built/pushed (setup step) → raise a clear error."""
    img = os.environ.get("SAGEMAKER_SERVING_IMAGE", "")
    if not img:
        raise RuntimeError(
            "SageMaker serving image is not configured: SAGEMAKER_SERVING_IMAGE is "
            "unset. Build + push the custom serving container (serving/) to ECR and "
            "relaunch the proxy so it propagates into new VMs. Until then, endpoints "
            "can't be created; use predict()/predict_batch() for in-VM scoring."
        )
    return img


def _sanitize_endpoint_name(prefix: str) -> str:
    """A unique endpoint/model/config name 'microvm-ep-<prefix>-<ts>'. The
    'microvm-ep-' prefix is MANDATORY — it's what the exec role's IAM scoping
    (endpoint/microvm-ep-*) and the Data Sources panel filter on."""
    base = re.sub(r"[^a-zA-Z0-9-]", "-", str(prefix or "model")).strip("-").lower() or "model"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"microvm-ep-{base}-{stamp}"
    return name[:63].rstrip("-")


def _stamp_onnx_classes(onnx_bytes: bytes) -> bytes:
    """Return the ONNX graph bytes with the classifier's class labels recorded in
    metadata_props under 'microvm_classes' (JSON list). skl2onnx stores them on the
    classifier node as classlabels_int64s / classlabels_strings, but onnxruntime
    doesn't surface node attributes — only metadata_props (via
    get_modelmeta().custom_metadata_map). So we lift the labels into metadata here,
    at deploy time (the VM has onnx via skl2onnx), letting the container read them
    with no onnx dependency. No-op (returns input unchanged) for a regressor, a graph
    with no class labels, or if onnx isn't importable."""
    try:
        import json as _json
        import onnx
        m = onnx.load_from_string(onnx_bytes)
        labels = None
        for node in m.graph.node:
            for attr in node.attribute:
                if attr.name == "classlabels_int64s" and attr.ints:
                    labels = [int(v) for v in attr.ints]
                elif attr.name == "classlabels_strings" and attr.strings:
                    labels = [s.decode() if isinstance(s, bytes) else s for s in attr.strings]
            if labels is not None:
                break
        if not labels:
            return onnx_bytes  # not a classifier / no labels → leave as-is
        # Replace any existing microvm_classes entry, then append ours.
        keep = [p for p in m.metadata_props if p.key != "microvm_classes"]
        del m.metadata_props[:]
        m.metadata_props.extend(keep)
        entry = m.metadata_props.add()
        entry.key = "microvm_classes"
        entry.value = _json.dumps(labels)
        return m.SerializeToString()
    except Exception as e:  # noqa: BLE001 — never fail deploy over a label-stamp shim.
        logger.info(f"[sagemaker] _stamp_onnx_classes skipped: {e}")
        return onnx_bytes


def _endpoint_artifact(model_or_job):
    """Serialize the thing being deployed into (member_name, bytes) for the serving
    container's model.tar.gz. Dispatch:
      • ONNX ModelProto/bytes→ ('model.onnx', graph bytes)
      • fitted sklearn model → ('model.joblib', joblib bytes)
    A SageMaker-trained model (sm_load_model result) is itself a complete sklearn
    Pipeline, so it takes the generic .joblib path below — the serving container's
    sklearn path serves it unchanged (xgboost/lightgbm estimators included, since the
    container ships those libraries). Reuses ml._serialize_model so the format logic
    lives in one place."""
    from .ml import _serialize_model

    # ONNX graph (onnx.ModelProto exposes SerializeToString) or raw bytes.
    if hasattr(model_or_job, "SerializeToString") or isinstance(model_or_job, (bytes, bytearray)):
        blob = _serialize_model(model_or_job, ".onnx")
        # Stamp the classifier's class labels into the graph's metadata_props so the
        # serving container can label proba columns with the REAL classes (matching
        # the sklearn/xgboost paths) — onnxruntime exposes metadata_props via
        # get_modelmeta().custom_metadata_map, so the container needs no onnx package.
        blob = _stamp_onnx_classes(blob)
        return "model.onnx", blob
    # A scikit-learn-style estimator (has .predict) → joblib.
    if hasattr(model_or_job, "predict"):
        return "model.joblib", _serialize_model(model_or_job, ".joblib")
    raise TypeError(
        "sm_deploy_serverless/sm_deploy_instance expect a fitted scikit-learn model "
        "(train_model output / any "
        "estimator with .predict), a SageMaker-trained model (sm_load_model result), "
        "an ONNX model (onnx.ModelProto from to_onnx) or its bytes. Got "
        f"{type(model_or_job).__name__}."
    )


class SageMakerEndpoint:
    """Handle for a deployed SageMaker endpoint. Returned by sm_deploy_serverless() /
    sm_deploy_instance(). Carries the endpoint name (the stable identity — invoke from
    any VM by name) plus what it was deployed from and its hosting mode. Pass it to
    sm_predict()/predict()/sm_delete_endpoint(), or just use its .name string.

    mode is 'serverless' (scales to zero, $0 idle) or 'instance' (dedicated instances
    that bill continuously while running — so instance endpoints can be STOPPED with
    sm_stop_endpoint() and resumed with sm_start_endpoint()). mode may be None when the
    handle was built from a bare name without a control-plane lookup."""

    def __init__(self, endpoint_name, model_name=None, config_name=None,
                 kind=None, model_s3_uri=None, mode=None):
        self.endpoint_name = endpoint_name
        self.name = endpoint_name          # ergonomic alias
        self.model_name = model_name or endpoint_name
        self.config_name = config_name or endpoint_name
        self.kind = kind                   # "sklearn" | "onnx"
        self.model_s3_uri = model_s3_uri
        self.mode = mode                   # "serverless" | "instance" | None

    def status(self) -> str:
        return sm_endpoint_status(self.endpoint_name)

    def predict(self, data, **kw):
        return sm_predict(self, data, **kw)

    def stop(self, **kw):
        return sm_stop_endpoint(self, **kw)

    def start(self, **kw):
        return sm_start_endpoint(self, **kw)

    def delete(self):
        return sm_delete_endpoint(self.endpoint_name)

    def __repr__(self):
        m = f" mode={self.mode}" if self.mode else ""
        return f"<SageMakerEndpoint {self.endpoint_name} kind={self.kind}{m}>"


def _package_endpoint_model(model, name: str, region: str) -> tuple[str, str, str]:
    """Shared deploy step: classify the model, package it as model.tar.gz and upload
    it to S3. Returns (res_name, kind, model_s3_uri).

    Used by BOTH sm_deploy_serverless and sm_deploy_instance, which then register
    the SageMaker Model (create_model) and their own endpoint-config production
    variant (serverless vs instance)."""
    # Fail fast (before any upload) when the execution role or the serving image
    # is not configured; the callers read both again for create_model.
    _sm_role_arn()
    bucket = _sm_bucket()
    _serving_image()

    # _endpoint_artifact yields exactly two formats: .onnx (ONNX graph) or .joblib
    # (any sklearn Pipeline, including a SageMaker-trained one with an xgboost/lightgbm
    # estimator). There is no separate booster format.
    member, blob = _endpoint_artifact(model)
    kind = "onnx" if member.endswith(".onnx") else "sklearn"

    res_name = _sanitize_endpoint_name(name or kind)
    key = f"{_sm_prefix()}endpoints/{res_name}/model.tar.gz"

    # Package model.tar.gz (single member at the archive root; the serving container
    # scans /opt/ml/model for *.joblib / *.onnx and dispatches on it).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        ti = tarfile.TarInfo(name=member)
        ti.size = len(blob)
        tar.addfile(ti, io.BytesIO(blob))
    tarball = buf.getvalue()

    _s3.put_bytes(bucket, key, tarball, content_type="application/gzip", region=region)
    model_s3_uri = f"s3://{bucket}/{key}"
    return res_name, kind, model_s3_uri


def _tag_kwargs(tags: dict) -> dict:
    """Normalize an optional {k: v} dict into the boto3 Tags kwarg (or {} if none).
    Applied to the model, endpoint config, AND endpoint for cost allocation."""
    tag_list = [{"Key": str(k), "Value": str(v)} for k, v in (tags or {}).items()]
    return {"Tags": tag_list} if tag_list else {}


def sm_deploy_serverless(model, *, name: str = None, memory_mb: int = 2048,
                         max_concurrency: int = 5, provisioned_concurrency: int = None,
                         tags: dict = None, wait: bool = True,
                         timeout_s: int = 1200) -> "SageMakerEndpoint":
    """
    Deploy a trained model as a SERVERLESS SageMaker endpoint — a standing HTTPS model
    callable from ANY notebook by name, that costs $0 while idle (scales to zero) and
    needs no start/stop. This is the DEFAULT way to deploy; reach for
    sm_deploy_instance only when you specifically need dedicated always-warm instances.

        model = train_model(orders, target="returned")   # or any sklearn estimator
        ep = sm_deploy_serverless(model)                  # → serverless endpoint
        preds = predict(ep, new_rows)                     # invoke by handle
        sm_delete_endpoint(ep)                            # remove when done

        onx = to_onnx(model, orders.head(1))              # ONNX also supported
        ep  = sm_deploy_serverless(onx)

    Accepts THREE model sources, all served by the SAME custom container
    (SAGEMAKER_SERVING_IMAGE), which dispatches on the packaged artifact:
      • a fitted **scikit-learn** model (train_model output or any estimator with
        .predict) → packaged as .joblib;
      • an **ONNX** model (onnx.ModelProto from to_onnx, or its bytes) → .onnx;
      • a **SageMaker-trained** model from `sm_load_model(job)` → it IS a complete
        scikit-learn Pipeline, so it takes the .joblib path (xgboost/lightgbm
        estimators included — the serving container ships those libraries), letting
        you promote a managed-training job to a real-time endpoint directly:
        `ep = sm_deploy_serverless(sm_load_model(job))`.
    All three take RAW rows at inference (the preprocessing is baked into the artifact).

    Args:
      name             base name for the endpoint (becomes microvm-ep-<name>-<ts>).
      memory_mb        serverless memory (1024–6144, must be a multiple of 1024).
      max_concurrency  serverless max concurrent invocations (1–200).
      provisioned_concurrency  optional pre-warmed concurrency (1..max_concurrency)
                       that keeps that many workers HOT to eliminate cold-start
                       latency. Still serverless, but you pay for the warm pool even
                       when idle — leave None for pure scale-to-zero ($0 idle).
      tags             optional {key: value} dict applied to the model, endpoint
                       config, AND endpoint (cost allocation / ownership).
      wait             block until InService (default True; a serverless cold start
                       is typically a few minutes). False returns immediately —
                       poll ep.status().
      timeout_s        max seconds to wait for InService.

    Returns a SageMakerEndpoint handle (mode='serverless'). NON-idle cost is $0
    (unless you set provisioned_concurrency); you pay per request while serving. Call
    sm_delete_endpoint(ep) to remove it entirely. Serverless scales to zero on its
    own, so there is nothing to stop/start.
    """
    region = _sm_region()
    res_name, kind, model_s3_uri = _package_endpoint_model(model, name, region)

    if memory_mb % 1024 != 0:
        memory_mb = max(1024, round(memory_mb / 1024) * 1024)
    serverless = {"MemorySizeInMB": int(memory_mb), "MaxConcurrency": int(max_concurrency)}
    # Optional pre-warmed pool (still serverless). Clamp to [1, max_concurrency] so
    # SageMaker doesn't reject provisioned > max, and skip 0/None (pure scale-to-zero).
    if provisioned_concurrency:
        pc = max(1, min(int(provisioned_concurrency), int(max_concurrency)))
        serverless["ProvisionedConcurrency"] = pc

    tag_kw = _tag_kwargs(tags)
    image = _serving_image()
    sm = _sm(region)
    sm.create_model(
        ModelName=res_name, ExecutionRoleArn=_sm_role_arn(),
        PrimaryContainer={"Image": image, "ModelDataUrl": model_s3_uri}, **tag_kw)
    sm.create_endpoint_config(
        EndpointConfigName=res_name,
        ProductionVariants=[{
            "VariantName": "AllTraffic",
            "ModelName": res_name,
            "ServerlessConfig": serverless,
        }],
        **tag_kw)
    sm.create_endpoint(EndpointName=res_name, EndpointConfigName=res_name, **tag_kw)

    ep = SageMakerEndpoint(res_name, model_name=res_name, config_name=res_name,
                           kind=kind, model_s3_uri=model_s3_uri, mode="serverless")
    pc_note = f", provisioned_concurrency={serverless['ProvisionedConcurrency']}" \
        if "ProvisionedConcurrency" in serverless else ""
    logger.info(f"[sagemaker] deploying endpoint {res_name} ({kind}, serverless "
                f"{memory_mb}MB/{max_concurrency}{pc_note}) ← {model_s3_uri}")
    print(f"🚀 Deploying SERVERLESS endpoint: {res_name}\n"
          f"   kind={kind}  memory={memory_mb}MB  max_concurrency={max_concurrency}{pc_note}\n"
          f"   Serverless: $0 while idle, pay per request. A cold start takes a few\n"
          f"   minutes to reach InService.\n"
          f"   Invoke:  predict(ep, rows)   or   sm_predict('{res_name}', rows)\n"
          f"   Remove:  sm_delete_endpoint('{res_name}')")

    if wait:
        _sm_wait_endpoint(res_name, region, timeout_s=timeout_s)
        print(f"✅ Endpoint {res_name} is InService.")
    return ep


# GPU instance families (SageMaker hosting). ml.<family>... — g/p are GPU, inf/trn
# are AWS accelerators. We flag these because the serving container is CPU-ONLY and
# the models it serves (sklearn / XGBoost / ONNX-of-sklearn) are classic TABULAR ML
# that does NOT benefit from a GPU — you'd pay the GPU premium for no speedup.
_GPU_INSTANCE_FAMILIES = ("g", "p", "inf", "trn")


def _is_gpu_instance_type(instance_type: str) -> bool:
    """True if an 'ml.<family><...>' hosting type is a GPU/accelerator family
    (ml.g*, ml.p*, ml.inf*, ml.trn*). Best-effort string parse — an unrecognized
    shape returns False (treated as CPU, no warning)."""
    s = str(instance_type or "").lower()
    if not s.startswith("ml."):
        return False
    fam = s[3:]  # drop 'ml.'
    return any(fam.startswith(f) for f in _GPU_INSTANCE_FAMILIES)


def sm_deploy_instance(model, *, name: str = None, instance_type: str = "ml.m5.large",
                       instance_count: int = 1, tags: dict = None, wait: bool = True,
                       timeout_s: int = 1200) -> "SageMakerEndpoint":
    """
    Deploy a trained model as an INSTANCE-BACKED (dedicated) SageMaker endpoint —
    always-warm compute with no cold start, callable from ANY notebook by name.

    ⚠️ COST: unlike serverless, an instance endpoint bills CONTINUOUSLY for the whole
    time it's running (per instance-hour), NOT per request — even when idle. So:
      • prefer sm_deploy_serverless (the default) unless you specifically need
        always-warm latency or an instance type serverless can't offer;
      • STOP it when you're not using it — sm_stop_endpoint(ep) tears down the running
        endpoint (→ $0) while KEEPING the config + model + S3 artifact, so
        sm_start_endpoint(ep) can bring it back without re-uploading;
      • sm_delete_endpoint(ep) removes everything permanently when you're truly done.

        model = train_model(orders, target="returned")
        ep = sm_deploy_instance(model, instance_type="ml.m5.large")
        preds = predict(ep, new_rows)
        sm_stop_endpoint(ep)        # pause billing; keeps the config+model to resume
        sm_start_endpoint(ep)       # bring it back InService
        sm_delete_endpoint(ep)      # permanent teardown

    Accepts the SAME three model sources as sm_deploy_serverless (sklearn / ONNX /
    SageMaker-trained), all served by the SAME custom container and all taking RAW
    rows at inference.

    Args:
      name            base name for the endpoint (becomes microvm-ep-<name>-<ts>).
      instance_type   SageMaker hosting instance type (e.g. 'ml.m5.large',
                      'ml.c5.xlarge', or a GPU type like 'ml.g5.xlarge').
      instance_count  number of instances behind the endpoint (default 1).
      tags            optional {key: value} dict applied to model, config, endpoint.
      wait            block until InService (default True; provisioning instances
                      typically takes a few minutes). False returns immediately.
      timeout_s       max seconds to wait for InService.

    Returns a SageMakerEndpoint handle (mode='instance').

    ⚠️ GPU types: the serving container is CPU-only and serves TABULAR models
    (sklearn / XGBoost / ONNX-of-sklearn), which do NOT benefit from a GPU. Deploying
    on a GPU family (ml.g*, ml.p*, ...) just adds cost with no speedup — this function
    warns but still proceeds (you may have a reason). GPU-accelerated serving would
    need a separate GPU container + a GPU-bound model type, which this platform
    doesn't have yet.
    """
    region = _sm_region()

    # Guardrail: warn (don't block) on a GPU/accelerator instance family — the CPU-only
    # serving container + tabular models gain nothing from a GPU, so this is almost
    # always an accidental overspend.
    if _is_gpu_instance_type(instance_type):
        logger.warning(f"[sagemaker] sm_deploy_instance on GPU type {instance_type}: "
                       "serving container is CPU-only; tabular models gain no speedup.")
        print(f"⚠️  '{instance_type}' is a GPU instance family, but the serving container is\n"
              f"   CPU-only and serves tabular models (sklearn / XGBoost / ONNX) that do NOT\n"
              f"   use a GPU — you'd pay the GPU premium for no speedup. Use a CPU type like\n"
              f"   ml.m5.large / ml.c5.xlarge unless you have a specific reason. Proceeding anyway.")

    res_name, kind, model_s3_uri = _package_endpoint_model(model, name, region)

    tag_kw = _tag_kwargs(tags)
    image = _serving_image()
    sm = _sm(region)
    sm.create_model(
        ModelName=res_name, ExecutionRoleArn=_sm_role_arn(),
        PrimaryContainer={"Image": image, "ModelDataUrl": model_s3_uri}, **tag_kw)
    sm.create_endpoint_config(
        EndpointConfigName=res_name,
        ProductionVariants=[{
            "VariantName": "AllTraffic",
            "ModelName": res_name,
            "InstanceType": str(instance_type),
            "InitialInstanceCount": int(instance_count),
        }],
        **tag_kw)
    sm.create_endpoint(EndpointName=res_name, EndpointConfigName=res_name, **tag_kw)

    ep = SageMakerEndpoint(res_name, model_name=res_name, config_name=res_name,
                           kind=kind, model_s3_uri=model_s3_uri, mode="instance")
    logger.info(f"[sagemaker] deploying endpoint {res_name} ({kind}, instance "
                f"{instance_count}×{instance_type}) ← {model_s3_uri}")
    print(f"🚀 Deploying INSTANCE endpoint: {res_name}\n"
          f"   kind={kind}  instance_type={instance_type}  count={instance_count}\n"
          f"   ⚠️ Instance endpoints bill continuously while running (per instance-hour),\n"
          f"      even when idle. Stop it to pause billing:\n"
          f"   Invoke:  predict(ep, rows)   or   sm_predict('{res_name}', rows)\n"
          f"   Stop:    sm_stop_endpoint('{res_name}')   (keeps config+model to resume)\n"
          f"   Start:   sm_start_endpoint('{res_name}')\n"
          f"   Remove:  sm_delete_endpoint('{res_name}')")

    if wait:
        _sm_wait_endpoint(res_name, region, timeout_s=timeout_s)
        print(f"✅ Endpoint {res_name} is InService.")
    return ep


def _sm_describe_endpoint(name: str, region: str = None) -> dict:
    region = region or _sm_region()
    sm = _sm(region)
    return sm.describe_endpoint(EndpointName=name)


def sm_endpoint_status(endpoint) -> str:
    """One-line status of an endpoint: 'InService' / 'Creating' / 'Failed: <reason>'
    / 'OutOfService' / 'Stopped' / 'Deleted'. Cheap — safe to poll while it provisions.

    'Stopped' is this project's synthetic state for an INSTANCE endpoint that was
    torn down by sm_stop_endpoint but whose config + model still exist (so it can be
    resumed with sm_start_endpoint). If neither the endpoint nor its config exist,
    the status is 'Deleted'."""
    name = endpoint.endpoint_name if isinstance(endpoint, SageMakerEndpoint) else str(endpoint)
    region = _sm_region()
    sm = _sm(region)
    try:
        d = sm.describe_endpoint(EndpointName=name)
    except Exception:  # noqa: BLE001 — no live endpoint object
        # No endpoint. Distinguish "stopped (resumable)" from "gone" by the config.
        cfg = endpoint.config_name if isinstance(endpoint, SageMakerEndpoint) else name
        try:
            sm.describe_endpoint_config(EndpointConfigName=cfg)
            return "Stopped"
        except Exception:  # noqa: BLE001
            return "Deleted"
    status = d.get("EndpointStatus", "Unknown")
    # Surface the reason for states that carry one (a failed create, or a failed
    # update that's reverting) so a poll shows WHY rather than a bare status.
    if status in ("Failed", "RollingBack"):
        return f"{status}: {d.get('FailureReason', 'unknown reason')}"
    return status


def _endpoint_info(endpoint) -> dict:
    """Control-plane summary of a deployed endpoint for model_info(). CHEAP — only
    describe_endpoint (+ describe_endpoint_config for the serverless memory/concurrency
    shape); it does NOT invoke the endpoint (no request cost, no sample rows needed).

    Accepts a SageMakerEndpoint handle OR an endpoint NAME string. Returns a dict:
      {type, name, status, kind?, endpoint_type, memory_mb?, max_concurrency?,
       region, created?, failure_reason?}. Best-effort — a missing config/field is
       simply omitted rather than raising, so a partially-provisioned endpoint still
       yields a useful summary."""
    region = _sm_region()
    name = endpoint.endpoint_name if isinstance(endpoint, SageMakerEndpoint) else str(endpoint)
    info = {"type": "SageMakerEndpoint", "name": name, "region": region,
            "endpoint_kind": "endpoint"}
    # kind (sklearn/onnx/xgboost) is only known from a handle; a bare name can't tell.
    if isinstance(endpoint, SageMakerEndpoint) and endpoint.kind:
        info["kind"] = endpoint.kind

    sm = _sm(region)
    try:
        d = sm.describe_endpoint(EndpointName=name)
    except Exception as e:
        info["status"] = "not found"
        info["error"] = str(e)
        return info

    info["status"] = d.get("EndpointStatus", "Unknown")
    if d.get("FailureReason"):
        info["failure_reason"] = d["FailureReason"]
    if d.get("CreationTime") is not None:
        info["created"] = str(d["CreationTime"])

    # Serverless shape lives on the endpoint CONFIG's production variant.
    cfg_name = d.get("EndpointConfigName")
    if cfg_name:
        try:
            cfg = sm.describe_endpoint_config(EndpointConfigName=cfg_name)
            variants = cfg.get("ProductionVariants", [])
            if variants:
                sc = variants[0].get("ServerlessConfig") or {}
                if sc:
                    info["endpoint_type"] = "serverless"
                    if sc.get("MemorySizeInMB") is not None:
                        info["memory_mb"] = int(sc["MemorySizeInMB"])
                    if sc.get("MaxConcurrency") is not None:
                        info["max_concurrency"] = int(sc["MaxConcurrency"])
                    if sc.get("ProvisionedConcurrency") is not None:
                        info["provisioned_concurrency"] = int(sc["ProvisionedConcurrency"])
                else:
                    # Instance-backed (provisioned) variant.
                    info["endpoint_type"] = "instance"
                    if variants[0].get("InstanceType"):
                        info["instance_type"] = variants[0]["InstanceType"]
                    # Prefer the LIVE instance count from the endpoint description's
                    # ProductionVariants (CurrentInstanceCount); fall back to the
                    # config's InitialInstanceCount.
                    live_variants = d.get("ProductionVariants") or []
                    cnt = None
                    if live_variants:
                        cnt = live_variants[0].get("CurrentInstanceCount")
                    if cnt is None:
                        cnt = variants[0].get("InitialInstanceCount")
                    if cnt is not None:
                        info["instance_count"] = int(cnt)
        except Exception:
            pass

    # Best-effort: ask the model itself for its I/O schema (the raw feature columns
    # + classes) so model_info shows "feed these columns → get this prediction". Only
    # OUR serving container answers the sentinel; a foreign endpoint (or a cold-fail)
    # just leaves these out. This IS a tiny InvokeEndpoint — acceptable here because
    # model_info is an explicit user call (not background discovery); skipped if the
    # endpoint isn't InService (a schema probe can't help while it's still coming up).
    if info.get("status") == "InService":
        sch = _endpoint_schema(name, region)
        if sch:
            if sch.get("feat_cols"):
                info["feature_names"] = [str(c) for c in sch["feat_cols"]]
                info["n_features"] = len(info["feature_names"])
            if sch.get("classes") is not None:
                info["classes"] = [str(c) for c in sch["classes"]]
            if sch.get("task"):
                info["model_task"] = sch["task"]
    return info


def _endpoint_schema(name: str, region: str = None) -> dict | None:
    """Ask the endpoint's model for its I/O schema via the schema sentinel (only OUR
    serving container answers it). Returns {feat_cols, classes, task, kind, ...} or
    None for a foreign/errored/cold-fail endpoint. This is a real (tiny) InvokeEndpoint
    — callers gate it (e.g. only when InService, only on explicit user actions)."""
    region = region or _sm_region()
    try:
        smr = _smr(region)
        # Ride out a cold start (shared policy) so model_info() right after a deploy
        # reliably gets the schema instead of silently dropping it on the first probe.
        resp = _invoke_with_cold_start_retry(
            smr, label="schema probe", EndpointName=name,
            ContentType="application/json", Body=b'{"__microvm_schema__": true}')
        body = json.loads(resp["Body"].read())
    except Exception:  # noqa: BLE001 — foreign container, cold-fail, no perms, ...
        return None
    if isinstance(body, dict) and body.get("microvm_schema"):
        return body
    return None


def _sm_wait_endpoint(name: str, region: str = None, timeout_s: int = 1200,
                      poll_s: int = 20) -> dict:
    """Block-poll an endpoint until InService or a terminal state/timeout. Raises on
    any state that will NOT progress to InService — Failed, OutOfService, RollingBack
    (an update failed and is reverting), or Deleting (it's being torn down) — so we
    don't spin until timeout on an endpoint that can never come up. The provisioning
    states (Creating/Updating/SystemUpdating) keep polling."""
    region = region or _sm_region()
    # States from which InService is unreachable → stop waiting immediately.
    TERMINAL_BAD = ("Failed", "OutOfService", "RollingBack", "Deleting")
    deadline = time.time() + timeout_s
    while True:
        d = _sm_describe_endpoint(name, region)
        status = d.get("EndpointStatus", "Unknown")
        if status == "InService":
            return d
        if status in TERMINAL_BAD:
            raise RuntimeError(
                f"Endpoint {name} {status}: {d.get('FailureReason', 'no reason given')}")
        if time.time() >= deadline:
            raise TimeoutError(
                f"Endpoint {name} still {status} after {timeout_s}s. It is NOT "
                f"cancelled — it keeps provisioning. Check sm_endpoint_status('{name}') "
                f"and invoke once it shows InService.")
        time.sleep(poll_s)


def _rows_from_frame(df: "pd.DataFrame"):
    """DataFrame → list of {column: value} dicts (JSON-safe) for the serving
    container's {"rows": [...]} contract. numpy scalars → native Python."""
    return [
        {k: (v.item() if hasattr(v, "item") else v) for k, v in rec.items()}
        for rec in df.to_dict(orient="records")
    ]


# Cold-start invoke retry policy. A fresh serverless/instance endpoint's FIRST
# invoke can cold-start past SageMaker's invoke window → ModelError (0); it's warm
# within seconds. 5 attempts with exponential backoff (2,4,8,16s ≈ 30s total) covers
# a container image pull + model load without hanging a cell indefinitely.
_INVOKE_MAX_ATTEMPTS = 5
_INVOKE_RETRY_BASE_S = 2


def _is_cold_start_error(exc) -> bool:
    """True if a sagemaker-runtime InvokeEndpoint error looks like a transient
    COLD START (retryable), not a genuine model/data failure. Matches the serverless
    cold-start signature: a ModelError whose message says the endpoint couldn't be
    reached / didn't respond, plus the throttling/5xx classes SageMaker marks
    retryable. A ValidationError, ModelError from a real exception in the container,
    etc. are NOT retried (re-raised immediately)."""
    try:
        from botocore.exceptions import ClientError
    except Exception:  # pragma: no cover
        return False
    if not isinstance(exc, ClientError):
        return False
    err = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
    code = str(err.get("Code", ""))
    msg = str(err.get("Message", "")).lower()
    # Explicitly retryable AWS error classes for InvokeEndpoint.
    if code in ("ThrottlingException", "ServiceUnavailable", "ServiceUnavailableException",
                "InternalFailure", "InternalServerError"):
        return True
    # Serverless cold start surfaces as ModelError (0) with a "could not get a
    # response from the model / endpoint" message. Scope the ModelError retry to that
    # phrasing so a REAL in-container model error (bad input, exception) still fails fast.
    if code == "ModelError" and (
        "could not get a response" in msg
        or "no response" in msg
        or "receive a response" in msg
        or "did not return a response" in msg
    ):
        return True
    return False


def _invoke_with_cold_start_retry(smr, *, label: str, **invoke_kwargs):
    """Call sagemaker-runtime.invoke_endpoint, retrying ONLY the transient cold-start
    error class (see _is_cold_start_error) with exponential backoff. The single place
    that encodes "InService ≠ warm": the FIRST invoke of a serverless/freshly-started
    endpoint cold-starts the container (image pull + model load), which can exceed
    SageMaker's invoke window and return ModelError (0) "could not get a response" —
    retryable, not a real failure. A genuine model/validation error is NOT retried
    (re-raised immediately). boto3 already retries throttling/5xx under the hood; this
    adds ONLY the data-plane cold-start class boto3 doesn't. Returns the raw response.

    label is used only for log context (e.g. 'invoke', 'schema probe')."""
    name = invoke_kwargs.get("EndpointName", "?")
    for attempt in range(_INVOKE_MAX_ATTEMPTS):
        try:
            return smr.invoke_endpoint(**invoke_kwargs)
        except Exception as e:  # noqa: BLE001
            if not _is_cold_start_error(e) or attempt == _INVOKE_MAX_ATTEMPTS - 1:
                raise
            delay = _INVOKE_RETRY_BASE_S * (2 ** attempt)
            logger.info(
                f"[sagemaker] endpoint {name} cold-start on {label} "
                f"(attempt {attempt + 1}/{_INVOKE_MAX_ATTEMPTS}); retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


def _invoke_endpoint(name: str, payload: dict, region: str) -> dict:
    smr = _smr(region)
    body = json.dumps(payload).encode("utf-8")
    resp = _invoke_with_cold_start_retry(
        smr, label="invoke", EndpointName=name,
        ContentType="application/json", Body=body)
    return json.loads(resp["Body"].read())


def sm_predict(endpoint, data, *, proba: bool = False):
    """
    Invoke a deployed endpoint on RAW rows and return predictions. This is a REMOTE
    HTTPS call (sagemaker-runtime InvokeEndpoint) to a standing, billable resource —
    distinct from the local, free predict() on an in-VM model.

        preds = sm_predict(ep, new_rows)              # labels (or values)
        proba = sm_predict(ep, new_rows, proba=True)  # class probabilities

    `endpoint` is a SageMakerEndpoint handle or an endpoint NAME string. `data` is a
    DataFrame (recommended — sent as feature dicts), Series, array, or list of rows.
    Returns a numpy array (or, for a DataFrame input, a Series aligned to its index).
    With proba=True returns the probability matrix (one column per class) when the
    endpoint provides it.
    """
    index = data.index if isinstance(data, pd.DataFrame) else None
    out = _sm_invoke(endpoint, data)
    if proba:
        probs = out.get("probabilities")
        if probs is None:
            name = endpoint.endpoint_name if isinstance(endpoint, SageMakerEndpoint) else str(endpoint)
            raise ValueError(
                f"Endpoint {name} did not return probabilities (the model may be a "
                "regressor, or was exported without them). Use sm_predict(...) for "
                "the point prediction.")
        return np.asarray(probs)
    preds = np.asarray(out.get("predictions"))
    if index is not None and preds.ndim == 1 and len(preds) == len(index):
        return pd.Series(preds, index=index, name="prediction")
    return preds


def _sm_payload(data):
    """Build the {"rows": [...]} or {"instances": [[...]]} invoke payload from a
    DataFrame/Series (preferred — sends named feature dicts) or array/list."""
    if isinstance(data, pd.DataFrame):
        return {"rows": _rows_from_frame(data)}
    if isinstance(data, pd.Series):
        return {"rows": _rows_from_frame(data.to_frame().T)}
    arr = np.asarray(data)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return {"instances": arr.tolist()}


def _sm_invoke(endpoint, data) -> dict:
    """Invoke an endpoint and return its FULL response dict ({predictions,
    probabilities?, classes?}). The single place that turns a handle/name + data into
    a remote call, so sm_predict and predict_proba stay consistent.

    A DataFrame/Series is scored in BOUNDED ROW CHUNKS (see _sm_invoke_chunked) so a
    large frame never blows the SageMaker ~6 MB invoke payload cap / invoke timeout;
    an array/list is sent as-is (callers using the array path pass small inputs)."""
    region = _sm_region()
    name = endpoint.endpoint_name if isinstance(endpoint, SageMakerEndpoint) else str(endpoint)
    if isinstance(data, pd.DataFrame):
        return _sm_invoke_chunked(name, data, region)
    if isinstance(data, pd.Series):
        return _sm_invoke_chunked(name, data.to_frame().T, region)
    return _invoke_endpoint(name, _sm_payload(data), region)


# SageMaker InvokeEndpoint caps the request payload at ~6 MB (and has an invoke
# timeout). A whole-DataFrame invoke silently hit that wall on a large batch, so we
# score in bounded ROW CHUNKS and merge. The chunk size is a conservative row count
# that keeps a JSON {"rows":[...]} payload well under 6 MB even for wide rows; it's
# a row cap, not a byte cap (rows vary in width), so it's deliberately conservative.
_SM_INVOKE_CHUNK_ROWS = 2000


def _sm_invoke_chunked(name: str, df: "pd.DataFrame", region: str) -> dict:
    """Invoke `name` over `df` in row chunks of _SM_INVOKE_CHUNK_ROWS and MERGE the
    per-chunk responses into one dict shaped exactly like a single invoke
    ({predictions, probabilities?, classes?}) — so callers (sm_predict / predict_proba
    / sm_predict_batch) are unchanged whether the frame was split or not.

    predictions/probabilities are CONCATENATED in input order; classes is taken from
    the first chunk that reports it (identical across chunks for one model). A frame
    at/under one chunk makes exactly one call (no overhead). Empty frame → empty
    predictions."""
    n = len(df)
    if n == 0:
        return {"predictions": []}
    if n <= _SM_INVOKE_CHUNK_ROWS:
        return _invoke_endpoint(name, {"rows": _rows_from_frame(df)}, region)

    predictions = []
    probabilities = []
    classes = None
    any_probs = False
    for start in range(0, n, _SM_INVOKE_CHUNK_ROWS):
        chunk = df.iloc[start:start + _SM_INVOKE_CHUNK_ROWS]
        resp = _invoke_endpoint(name, {"rows": _rows_from_frame(chunk)}, region)
        preds = resp.get("predictions")
        if preds is not None:
            predictions.extend(preds)
        probs = resp.get("probabilities")
        if probs is not None:
            any_probs = True
            probabilities.extend(probs)
        if classes is None and resp.get("classes") is not None:
            classes = resp.get("classes")
    merged = {"predictions": predictions}
    if any_probs:
        merged["probabilities"] = probabilities
    if classes is not None:
        merged["classes"] = classes
    return merged


def _is_sm_endpoint(target) -> bool:
    """True if `target` refers to a DEPLOYED endpoint — a SageMakerEndpoint handle or
    a 'microvm-ep-*' endpoint NAME string — as opposed to a training-job handle or a
    loaded model. Used by sm_predict_batch to route remote-invoke vs local-load. A
    SageMakerJob has a .job_name (not .endpoint_name), so it correctly returns False."""
    if isinstance(target, SageMakerEndpoint):
        return True
    return isinstance(target, str) and target.startswith("microvm-ep-")


def sm_predict_batch(target, df: "pd.DataFrame", *, output_col: str = "prediction",
                     proba: bool = False, timeout_s: int = 1800) -> "pd.DataFrame":
    """
    Score a whole DataFrame with a SageMaker model and return a COPY of df with
    predictions appended as a new column. Accepts EITHER of the two SageMaker model
    shapes (overloaded on `target`):

      • a deployed ENDPOINT (SageMakerEndpoint handle from sm_deploy_*, or an endpoint
        NAME string) → scored REMOTELY via InvokeEndpoint (billable per call; for very
        large frames consider chunking);
      • a training JOB (SageMakerJob handle from sm_train) → pulled into the VM once
        via sm_load_model (WAITS for the job) and scored LOCALLY — endpoint-free,
        ideal for a table that fits the VM.

            scored = sm_predict_batch(ep,  new_df, output_col="will_return")   # endpoint
            scored = sm_predict_batch(job, new_df, output_col="will_return")   # job

    (The generic predict_batch(model_or_job_or_ep, df) routes here too, so you can use
    either name — predict_batch is the one surface for local, job, and endpoint models.)

    proba=True (classification) appends class PROBABILITIES instead of the hard label —
    what you want to RANK by likelihood. For a BINARY classifier `output_col` holds
    P(positive class) as one column; MULTICLASS appends one column per class named
    "<output_col>_<class>".

        scored = sm_predict_batch(job, new_df, output_col="return_prob", proba=True)
        scored.sort_values("return_prob", ascending=False).head(20)   # riskiest first

    timeout_s bounds the job-load wait (ignored for an endpoint, which is already up).
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("sm_predict_batch expects a DataFrame.")

    # JOB (or a loaded SageMaker model) → load once into the VM and score locally.
    # An endpoint is handled remotely below; everything else is treated as a
    # job/model and resolved via sm_load_model (a no-op download for a loaded model).
    if not _is_sm_endpoint(target):
        # An ALREADY-loaded model (has .predict) is used as-is; a SageMakerJob handle
        # or an S3 URI string is resolved via sm_load_model (which waits + downloads).
        model = target if hasattr(target, "predict") \
            else sm_load_model(target, timeout_s=timeout_s)
        out = df.copy()
        # A SageMaker-trained model is a bare scikit-learn Pipeline (no .task attr),
        # which exposes predict_proba when its estimator is a classifier (and proxies
        # .classes_ to the final estimator). Duck-type on predict_proba so the proba
        # path isn't skipped — a .task-only gate would silently drop to hard labels.
        wants_proba = proba and hasattr(model, "predict_proba")
        if wants_proba:
            p = np.asarray(model.predict_proba(df), dtype=float)
            _append_proba_columns(out, p, getattr(model, "classes_", None), output_col)
        else:
            out[output_col] = np.asarray(model.predict(df))
        return out

    # ENDPOINT → score remotely (InvokeEndpoint).
    out = df.copy()
    if proba:
        resp = _sm_invoke(target, df)
        probs = resp.get("probabilities")
        if probs is None:
            name = target.endpoint_name if isinstance(target, SageMakerEndpoint) else str(target)
            raise ValueError(
                f"Endpoint {name} did not return probabilities (likely a regressor). "
                "Use sm_predict_batch(..., proba=False).")
        _append_proba_columns(out, np.asarray(probs, dtype=float),
                              resp.get("classes"), output_col)
    else:
        out[output_col] = np.asarray(sm_predict(target, df))
    return out


def _append_proba_columns(out: "pd.DataFrame", p, classes, output_col: str) -> None:
    """Append class-probability column(s) to `out` in place, shared by the endpoint
    and job paths of sm_predict_batch so their proba output is identical. Binary →
    one P(positive class) column; multiclass → one column per class, labelled with the
    real class label when known (matches local predict_batch), else the index."""
    if p.ndim == 2 and p.shape[1] == 2:
        out[output_col] = p[:, 1]          # binary: P(positive class)
    elif p.ndim == 2:
        labels = classes if (classes is not None and len(classes) == p.shape[1]) \
            else list(range(p.shape[1]))
        for i, cls in enumerate(labels):
            out[f"{output_col}_{cls}"] = p[:, i]
    else:
        out[output_col] = p


def sm_endpoints(max_results: int = 25) -> "pd.DataFrame":
    """
    List this account's SageMaker endpoints (filtered to this project's
    microvm-ep-*) as a DataFrame: endpoint, type, status, region, created. The 'type'
    column tells serverless from instance at a glance — serverless shows its memory +
    max-concurrency (e.g. 'serverless · 2GB ≤5'), instance shows its instance type +
    count (e.g. 'instance · ml.m5.large'). Handy to see what's standing and to grab a
    name for sm_predict / sm_delete_endpoint.

        sm_endpoints()
    """
    region = _sm_region()
    sm = _sm(region)
    endpoints = _sm_list_pages(sm.list_endpoints, "Endpoints", max_results,
                               SortBy="CreationTime", SortOrder="Descending",
                               NameContains="microvm-ep-")
    rows = []
    for e in endpoints:
        name = e.get("EndpointName")
        # Our deploy names the endpoint and its config identically, so the endpoint
        # name is also the config name — read the config to classify the hosting mode
        # (one extra describe per endpoint; best-effort so a missing config degrades
        # to 'unknown' rather than failing the whole listing).
        rows.append({
            "endpoint": name,
            "type": _endpoint_type_label(name, region, sm),
            "status": e.get("EndpointStatus"),
            "region": region,
            "created": e.get("CreationTime"),
        })
    return pd.DataFrame(rows, columns=["endpoint", "type", "status", "region", "created"])


def _endpoint_config_shape(config_name: str, region: str = None, sm=None) -> dict:
    """Read an endpoint CONFIG's production variant → a shape dict
    {mode, instance_type?, instance_count?, memory_mb?, max_concurrency?}. Serverless
    variants carry a ServerlessConfig (memory + max-concurrency); instance variants
    carry InstanceType (+ InitialInstanceCount). Reads the config even when the live
    endpoint is gone (works for a Stopped endpoint). Best-effort: a missing config /
    unexpected shape → {mode: 'unknown'}. Mirrors the discovery side's _classify_config
    so the helper and the Data Sources panel agree."""
    region = region or _sm_region()
    sm = sm or _sm(region)
    shape = {"mode": "unknown"}
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
    except Exception:  # noqa: BLE001
        pass
    return shape


def _endpoint_type_label(config_name: str, region: str = None, sm=None) -> str:
    """Compact human label for the sm_endpoints() 'type' column, e.g.
    'instance · ml.m5.large', 'instance · ml.m5.large ×2', 'serverless · 2GB ≤5',
    or 'serverless'. Falls back to 'unknown' when the config can't be read."""
    s = _endpoint_config_shape(config_name, region, sm)
    mode = s.get("mode", "unknown")
    if mode == "instance":
        it = s.get("instance_type") or "instance"
        cnt = s.get("instance_count")
        return f"instance · {it} ×{cnt}" if cnt and cnt != 1 else f"instance · {it}"
    if mode == "serverless":
        bits = []
        if s.get("memory_mb") is not None:
            mb = s["memory_mb"]
            bits.append(f"{mb // 1024}GB" if mb >= 1024 and mb % 1024 == 0 else f"{mb}MB")
        if s.get("max_concurrency") is not None:
            bits.append(f"≤{s['max_concurrency']}")
        return "serverless · " + " ".join(bits) if bits else "serverless"
    return "unknown"


def _endpoint_config_is_instance(config_name: str, region: str = None) -> bool:
    """True if an endpoint CONFIG's production variant is instance-backed (has
    InstanceType) rather than serverless (has ServerlessConfig). Reads the config even
    when the live endpoint is gone, so it works for a Stopped endpoint. Best-effort:
    a missing config / unexpected shape → False (treated as not-instance)."""
    return _endpoint_config_shape(config_name, region).get("mode") == "instance"


def sm_stop_endpoint(endpoint, *, wait: bool = False, timeout_s: int = 600) -> str:
    """
    STOP an INSTANCE-backed endpoint to pause its per-instance-hour billing, while
    KEEPING everything needed to bring it back. Deletes only the running endpoint
    (the billed resource); its endpoint-config, SageMaker model, and the S3
    model.tar.gz all stay put, so sm_start_endpoint(ep) resumes it without any
    re-upload or re-training.

        ep = sm_deploy_instance(model)
        sm_stop_endpoint(ep)      # → $0 while stopped; config + model retained
        sm_start_endpoint(ep)     # ← back InService, same name

    SERVERLESS endpoints already scale to zero ($0 idle), so there's nothing to stop —
    this is a no-op on them (use sm_delete_endpoint to remove a serverless endpoint).

    wait=False returns immediately after issuing the delete (the endpoint spends a
    short while in 'Deleting'); wait=True blocks until it's fully gone.

    Returns a short STATUS STRING describing what happened (e.g. "Stopped <name>", or
    a no-op message for a serverless/already-stopped endpoint) — this is an action
    verb that reports an outcome. (Its counterpart sm_start_endpoint instead returns a
    SageMakerEndpoint handle, because starting yields a usable endpoint to invoke.)
    """
    region = _sm_region()
    name = endpoint.endpoint_name if isinstance(endpoint, SageMakerEndpoint) else str(endpoint)
    cfg = endpoint.config_name if isinstance(endpoint, SageMakerEndpoint) else name
    sm = _sm(region)

    # Guard: only instance endpoints have anything to stop. Prefer the handle's known
    # mode; otherwise inspect the config. Serverless → no-op with a clear message.
    mode = getattr(endpoint, "mode", None) if isinstance(endpoint, SageMakerEndpoint) else None
    is_instance = (mode == "instance") or (mode is None and _endpoint_config_is_instance(cfg, region))
    if not is_instance:
        msg = (f"{name} is serverless (scales to zero automatically) — nothing to stop. "
               f"Use sm_delete_endpoint('{name}') to remove it.")
        logger.info(f"[sagemaker] sm_stop_endpoint no-op: {msg}")
        print(f"ℹ️  {msg}")
        return msg

    try:
        sm.delete_endpoint(EndpointName=name)
    except Exception as e:  # noqa: BLE001 — already stopped / never existed
        msg = f"{name} is not running (nothing to stop): {e}"
        logger.info(f"[sagemaker] sm_stop_endpoint: {msg}")
        print(f"ℹ️  {msg}")
        return msg

    logger.info(f"[sagemaker] stopped endpoint {name} (config+model retained for resume)")
    print(f"⏸️  Stopped instance endpoint: {name}\n"
          f"   Billing paused ($0 while stopped). Config + model kept.\n"
          f"   Resume:  sm_start_endpoint('{name}')\n"
          f"   Remove:  sm_delete_endpoint('{name}')  (permanent)")

    if wait:
        # Poll until describe_endpoint 404s (fully deleted).
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                sm.describe_endpoint(EndpointName=name)
                time.sleep(5)
            except Exception:  # noqa: BLE001
                break
        print(f"✅ Endpoint {name} is Stopped.")
    return f"Stopped {name}"


def sm_start_endpoint(endpoint, *, wait: bool = True, timeout_s: int = 1200) -> "SageMakerEndpoint":
    """
    START (resume) a STOPPED instance endpoint by recreating it from its retained
    endpoint-config. The config + model still exist (sm_stop_endpoint kept them), so
    this is a single create_endpoint call — no re-upload, no re-training.

        sm_start_endpoint(ep)                    # from a handle
        sm_start_endpoint("microvm-ep-...")      # or a name

    Returns a SageMakerEndpoint handle (mode='instance'). If the endpoint is already
    running, this is a no-op that returns a handle to it. wait=True (default) blocks
    until InService.
    """
    region = _sm_region()
    name = endpoint.endpoint_name if isinstance(endpoint, SageMakerEndpoint) else str(endpoint)
    cfg = endpoint.config_name if isinstance(endpoint, SageMakerEndpoint) else name
    kind = getattr(endpoint, "kind", None) if isinstance(endpoint, SageMakerEndpoint) else None
    model_s3 = getattr(endpoint, "model_s3_uri", None) if isinstance(endpoint, SageMakerEndpoint) else None
    sm = _sm(region)

    # Already running? Return a handle rather than erroring on create.
    try:
        sm.describe_endpoint(EndpointName=name)
        logger.info(f"[sagemaker] sm_start_endpoint: {name} already exists — returning handle")
        print(f"ℹ️  {name} is already running.")
        return SageMakerEndpoint(name, config_name=cfg, kind=kind,
                                 model_s3_uri=model_s3, mode="instance")
    except Exception:  # noqa: BLE001 — not running; proceed to (re)create
        pass

    # The config must still exist for a resume. If it's gone, the endpoint was fully
    # deleted (sm_delete_endpoint), not stopped — guide the user to redeploy.
    if not _sm_config_exists(cfg, region):
        raise RuntimeError(
            f"Cannot start '{name}': its endpoint-config '{cfg}' no longer exists. "
            f"It was deleted (not stopped) — redeploy with sm_deploy_instance(model).")

    sm.create_endpoint(EndpointName=name, EndpointConfigName=cfg)
    logger.info(f"[sagemaker] starting endpoint {name} from config {cfg}")
    print(f"▶️  Starting instance endpoint: {name}\n"
          f"   Provisioning instances from the retained config — a few minutes to InService.")

    ep = SageMakerEndpoint(name, config_name=cfg, kind=kind,
                           model_s3_uri=model_s3, mode="instance")
    if wait:
        _sm_wait_endpoint(name, region, timeout_s=timeout_s)
        print(f"✅ Endpoint {name} is InService.")
    return ep


def _sm_config_exists(config_name: str, region: str = None) -> bool:
    """True if an endpoint-config exists (used to tell 'stopped, resumable' from
    'fully deleted')."""
    region = region or _sm_region()
    sm = _sm(region)
    try:
        sm.describe_endpoint_config(EndpointConfigName=config_name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _delete_endpoint_object(sm, name: str, *, gone_timeout_s: int = 20) -> str:
    """Delete the ENDPOINT object and wait (BOUNDED) for it to actually disappear.
    Returns one of:
      "absent"   — it never existed / already gone,
      "gone"     — we deleted it and confirmed it's gone (safe to remove config/model),
      "deleting" — delete issued but still tearing down when the time budget ran out
                   (caller must NOT remove config/model yet — re-run to finish).

    WHY DELETE FIRST, EVEN WHILE 'Creating': per the SageMaker docs, DeleteEndpoint
    works on an endpoint in any state (incl. Creating/Updating) and transitions it to
    Deleting — it does NOT strand it. The thing that DOES strand an endpoint is
    deleting its EndpointConfig while the endpoint still exists, so the caller only
    removes config/model after we return "gone". The old code instead WAITED up to
    600s for a 'Creating' endpoint to finish before deleting — which blew the 60s
    notebook-cell budget and surfaced as 'Execution timed out'. We now issue the
    delete immediately and bound the wait so the call always fits a cell."""
    # Issue the delete immediately (idempotent: a 'not found' just means it's gone).
    try:
        sm.describe_endpoint(EndpointName=name)
    except Exception:  # noqa: BLE001 — not found → nothing to delete
        return "absent"
    try:
        sm.delete_endpoint(EndpointName=name)
    except Exception as e:  # noqa: BLE001
        if "find endpoint" in str(e).lower() or "does not exist" in str(e).lower():
            return "absent"
        raise

    # Bounded wait for the object to disappear (so the caller can safely remove the
    # config/model). If it's still Deleting when the budget runs out, say so rather
    # than hang — re-running sm_delete_endpoint finishes the cleanup idempotently.
    deadline = time.time() + gone_timeout_s
    while time.time() < deadline:
        try:
            sm.describe_endpoint(EndpointName=name)
            time.sleep(5)
        except Exception:  # noqa: BLE001 — 404 → fully deleted
            return "gone"
    return "deleting"


def sm_delete_endpoint(endpoint) -> str:
    """
    Delete a deployed endpoint AND its config + model (full teardown, so nothing keeps
    standing). Serverless endpoints cost $0 idle, but deleting removes the resource
    entirely for hygiene. For an INSTANCE endpoint this is the PERMANENT teardown —
    use sm_stop_endpoint to merely pause a resumable one. Callable from ANY notebook
    by name.

        sm_delete_endpoint(ep)            # a handle
        sm_delete_endpoint("microvm-ep-...")   # or a name

    Best-effort on each of endpoint/config/model — a missing piece isn't fatal (so it
    also cleans up the leftover config+model of a Stopped endpoint).
    """
    region = _sm_region()
    sm = _sm(region)
    if isinstance(endpoint, SageMakerEndpoint):
        name = endpoint.endpoint_name
        cfg = endpoint.config_name
        mdl = endpoint.model_name
    else:
        name = str(endpoint)
        cfg = mdl = name   # our deploy uses the same name for all three
    deleted = []

    # ORDER MATTERS: delete the ENDPOINT first and confirm it's GONE before removing
    # its config/model. Deleting the config out from under an endpoint that still
    # exists (e.g. right after a wait=False deploy, still 'Creating') strands it in a
    # 'Failed' state ("EndpointConfig ... does not exist"). _delete_endpoint_object
    # issues the delete immediately (safe even while Creating — DeleteEndpoint works
    # in any state) and waits a BOUNDED ~20s (well under the 60s cell timeout) for it
    # to vanish; if still tearing down it returns "deleting" so the cell never hangs.
    try:
        ep_state = _delete_endpoint_object(sm, name)   # "absent" | "gone" | "deleting"
        if ep_state in ("gone",):
            deleted.append("endpoint")
    except Exception as e:  # noqa: BLE001 — already gone / never existed
        logger.info(f"[sagemaker] delete endpoint {name} skipped: {e}")
        ep_state = "absent"

    # Still tearing down? Do NOT remove config/model yet (that's the stranding race) —
    # return now so the call fits a cell. Re-running sm_delete_endpoint once it's gone
    # finishes the cleanup idempotently.
    if ep_state == "deleting":
        msg = (f"{name}: endpoint deletion in progress (it was still provisioning). "
               f"Its config + model are kept for now — re-run sm_delete_endpoint('{name}') "
               f"in a minute to finish removing them.")
        logger.info(f"[sagemaker] {msg}")
        return msg

    # Endpoint is confirmed gone (or never existed) → safe to remove config + model.
    for what, fn in (("config", lambda: sm.delete_endpoint_config(EndpointConfigName=cfg)),
                     ("model", lambda: sm.delete_model(ModelName=mdl))):
        try:
            fn(); deleted.append(what)
        except Exception as e:  # noqa: BLE001
            logger.info(f"[sagemaker] delete {what} {name} skipped: {e}")
    # Also remove the S3 deploy artifact this endpoint was packaged from
    # (<sm_prefix>endpoints/<name>/...). deploy uses the endpoint name as res_name,
    # so the artifact lives under that prefix. Tying artifact lifecycle to the
    # endpoint keeps sagemaker/endpoints/ from accumulating orphans. Best-effort.
    try:
        if _delete_endpoint_artifact(name, region):
            deleted.append("s3-artifact")
    except Exception as e:  # noqa: BLE001
        logger.info(f"[sagemaker] delete s3 artifact for {name} skipped: {e}")
    logger.info(f"[sagemaker] deleted endpoint {name} ({', '.join(deleted) or 'nothing'})")
    return f"Deleted {name}: {', '.join(deleted) or 'nothing found'}"


def _delete_endpoint_artifact(endpoint_name: str, region: str) -> bool:
    """Delete the S3 deploy artifact(s) written for an endpoint, i.e. every
    object under <sm_prefix>endpoints/<endpoint_name>/ (written at deploy). Returns True if anything was
    deleted. Best-effort — the bucket may be unset or the prefix already gone."""
    bucket = os.environ.get("ARTIFACT_BUCKET") or os.environ.get("SAGEMAKER_ARTIFACT_BUCKET", "")
    if not bucket:
        return False
    prefix = f"{_sm_prefix()}endpoints/{endpoint_name}/"
    # Paginated delete (one delete_objects call per listing page), shared with the
    # UNLOAD prefix clear in readers.
    total = _s3.delete_prefix(bucket, prefix, region)
    if not total:
        return False
    logger.info(f"[sagemaker] deleted {total} S3 artifact object(s) under s3://{bucket}/{prefix}")
    return True
