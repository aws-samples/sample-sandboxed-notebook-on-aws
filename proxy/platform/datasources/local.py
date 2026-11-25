"""
Local file schema provider — reads file headers from the MicroVM's /tmp/ directory.
Requires forwarding requests to the running MicroVM via the proxy.
"""

import logging
from typing import Optional

from .interface import DataSourceProvider, SourceSchema, ColumnInfo

logger = logging.getLogger(__name__)


class LocalFileSchemaProvider(DataSourceProvider):
    """
    Schema provider for local files on the MicroVM (/tmp/).
    
    Unlike other providers, this one needs to execute code on the VM
    to read the file header. It delegates to the MicroVM via execute_code.

    Discovery is NOT done here — local /tmp files are enumerated on the VM
    (app/notebook/data_catalog.py) and surfaced via the VM data catalog, so
    discover() keeps the default (empty) behavior.
    """

    display_name = "Sandbox Files"
    icon = "local"
    supports_sql = True
    requires_vm_execution = True

    def __init__(self, execute_fn=None):
        """
        Args:
            execute_fn: Async callable(session_id, code) → result dict.
                        Used to run Python on the MicroVM to read file schema.
        """
        self._execute_fn = execute_fn

    @property
    def source_type(self) -> str:
        return "local"

    def reader_docs(self) -> list[str]:
        return [
            "read_local(path) -> df                          # Read /tmp/file (.csv, .parquet, .json, .xlsx)",
            "read_local_prefix('/tmp/parts/') -> df          # Read a FOLDER of part files (part-0000.parquet …) into one df",
        ]

    def sql_syntax_docs(self) -> list[str]:
        return [
            "Local CSV files: SELECT * FROM '/tmp/file.csv' LIMIT 10",
            "Local JSON files: SELECT * FROM '/tmp/file.json' LIMIT 10",
            "Local Parquet files: SELECT * FROM '/tmp/file.parquet' LIMIT 10",
            "Local partitioned folder: SELECT * FROM read_parquet('/tmp/parts/*.parquet') LIMIT 10",
        ]

    async def get_schema(self, source_id: str, session_id: str = None) -> Optional[SourceSchema]:
        """
        Get schema for a local file on the MicroVM.
        source_id: file path (e.g., '/tmp/sales_data.csv')
        session_id: required to forward the request to the correct VM.
        """
        if not self._execute_fn or not session_id:
            return None

        display_name = source_id.rsplit('/', 1)[-1] if '/' in source_id else source_id

        # A PREFIX source (partition folder, trailing '/') is a SET of part files,
        # not one object. Mirror S3's get_schema prefix branch: infer the schema
        # from the FIRST part under the dir (parts are parquet in the real case).
        if source_id.endswith("/"):
            display_name = source_id
            code = f"""
import os, json, glob
import pandas as pd

d = '{source_id}'.rstrip('/')
parts = sorted(p for p in glob.glob(os.path.join(d, '*')) if os.path.isfile(p))
if not parts:
    print(json.dumps({{"error": "No part files"}}))
else:
    first = parts[0]
    total = sum(os.path.getsize(p) for p in parts)
    fext = first.rsplit('.', 1)[-1].lower() if '.' in first.rsplit('/', 1)[-1] else 'parquet'
    if fext == 'csv':
        df = pd.read_csv(first, nrows=5)
    elif fext in ('json',):
        df = pd.read_json(first, lines=True, nrows=5)
    else:
        df = pd.read_parquet(first).head(5)
    cols = []
    for col in df.columns:
        dtype = str(df[col].dtype)
        if 'int' in dtype: dtype = 'int'
        elif 'float' in dtype: dtype = 'float'
        elif 'datetime' in dtype: dtype = 'datetime'
        elif 'bool' in dtype: dtype = 'boolean'
        else: dtype = 'string'
        sample = str(df[col].iloc[0])[:50] if len(df) > 0 else ''
        cols.append({{"name": str(col), "dtype": dtype, "sample": sample}})
    print(json.dumps({{"columns": cols, "row_count": None, "size_bytes": total}}))
"""
            try:
                result = await self._execute_fn(session_id, code)
                if not result or not result.get("success"):
                    return None
                import json
                data = json.loads(result.get("output", "").strip())
                if "error" in data:
                    return None
                size_bytes = data.get("size_bytes", 0)
                if size_bytes < 1024:
                    size = f"{size_bytes} B"
                elif size_bytes < 1024 * 1024:
                    size = f"{size_bytes / 1024:.1f} KB"
                else:
                    size = f"{size_bytes / (1024 * 1024):.1f} MB"
                columns = [
                    ColumnInfo(name=c["name"], dtype=c["dtype"], sample=c.get("sample", ""))
                    for c in data.get("columns", [])
                ]
                return SourceSchema(
                    source_type="local", source_id=source_id,
                    display_name=display_name, columns=columns,
                    row_count=None, size=size,
                )
            except Exception as e:
                logger.warning(f"Local prefix schema failed for {source_id}: {e}")
                return None

        code = f"""
import os, json
import pandas as pd

path = '{source_id}'
if not os.path.exists(path):
    print(json.dumps({{"error": "File not found"}}))
else:
    size = os.path.getsize(path)
    ext = path.rsplit('.', 1)[-1].lower()
    if ext == 'csv':
        df = pd.read_csv(path, nrows=5)
    elif ext == 'parquet':
        df = pd.read_parquet(path).head(5)
    elif ext in ('json',):
        df = pd.read_json(path, lines=True, nrows=5)
    elif ext in ('xlsx', 'xls'):
        df = pd.read_excel(path, nrows=5)
    else:
        df = pd.DataFrame()
    
    cols = []
    for col in df.columns:
        dtype = str(df[col].dtype)
        if 'int' in dtype: dtype = 'int'
        elif 'float' in dtype: dtype = 'float'
        elif 'datetime' in dtype: dtype = 'datetime'
        elif 'bool' in dtype: dtype = 'boolean'
        else: dtype = 'string'
        sample = str(df[col].iloc[0])[:50] if len(df) > 0 else ''
        cols.append({{"name": str(col), "dtype": dtype, "sample": sample}})
    
    print(json.dumps({{
        "columns": cols,
        "row_count": len(pd.read_csv(path)) if ext == 'csv' else None,
        "size_bytes": size,
    }}))
"""
        try:
            result = await self._execute_fn(session_id, code)
            if not result or not result.get("success"):
                return None

            import json
            data = json.loads(result.get("output", "").strip())
            if "error" in data:
                return None

            size_bytes = data.get("size_bytes", 0)
            if size_bytes < 1024:
                size = f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                size = f"{size_bytes / 1024:.1f} KB"
            else:
                size = f"{size_bytes / (1024 * 1024):.1f} MB"

            columns = [
                ColumnInfo(name=c["name"], dtype=c["dtype"], sample=c.get("sample", ""))
                for c in data.get("columns", [])
            ]

            return SourceSchema(
                source_type="local",
                source_id=source_id,
                display_name=display_name,
                columns=columns,
                row_count=data.get("row_count"),
                size=size,
            )

        except Exception as e:
            logger.warning(f"Local file schema failed for {source_id}: {e}")
            return None

    async def get_preview(self, source_id: str, limit: int = 5) -> list[dict]:
        """Not implemented for local files (would need session context)."""
        return []

    def get_python_snippet(self, source_id: str) -> str:
        # A PREFIX source (a rolled-up partition FOLDER, source_id ends with '/')
        # is a SET of part files, not one object → read the whole folder. Mirrors
        # S3's trailing-slash keying exactly: the snippet endpoint passes only
        # source_type + source_id (no metadata), so detect "prefix" from the slash.
        # read_local_prefix is the local parallel to read_s3_prefix.
        if source_id.endswith("/"):
            leaf = source_id.rstrip("/").rsplit("/", 1)[-1]
            var_name = (leaf or "parts").replace("-", "_").replace(" ", "_")
            return (f"# Read a whole local partition folder (its part files) into one DataFrame.\n"
                    f"{var_name} = read_local_prefix('{source_id}')\n"
                    f"{var_name}.head()")

        ext = source_id.rsplit('.', 1)[-1].lower() if '.' in source_id else "csv"
        var_name = source_id.rsplit('/', 1)[-1].rsplit('.', 1)[0].replace('-', '_').replace(' ', '_')

        if ext in ("joblib", "jbl", "pkl", "pickle", "sav"):
            # Trained-model artifact on the VM → load it and SCORE with it (feed RAW
            # rows; a train_model Pipeline one-hots internally). df is a stand-in.
            model_var = var_name if var_name != "model" else "clf"
            return (f"# Load a trained model from the sandbox and score with it\n"
                    f"{model_var} = load_model_local('{source_id}')\n"
                    f"model_info({model_var})              # features/classes it expects\n"
                    f"# preds = predict({model_var}, df)               # label per row (RAW rows)\n"
                    f"# scored = predict_batch({model_var}, df, output_col='pred')  # append a column\n"
                    f"# proba = predict_proba({model_var}, df)         # class probabilities")
        if ext == "onnx":
            return (f"# Load an ONNX model from the sandbox and run it framework-free\n"
                    f"session = load_onnx('{source_id}')  # local path or s3:// URI\n"
                    f"# preds = onnx_predict(session, df)   # feed RAW rows; encoding is in the graph")
        return f"# Read local file\n{var_name} = read_local('{source_id}')\n{var_name}.head()"

    def get_sql_snippet(self, source_id: str) -> str:
        # A PREFIX source (partition FOLDER, trailing '/') → glob the parts with
        # DuckDB read_parquet (parquet is the primary/real case, like S3). Mirrors
        # the S3 prefix SQL form.
        if source_id.endswith("/"):
            return f"SELECT * FROM read_parquet('{source_id}*.parquet') LIMIT 100"
        ext = source_id.rsplit('.', 1)[-1].lower() if '.' in source_id else ""
        if ext in ("joblib", "jbl", "pkl", "pickle", "sav", "onnx") or source_id.endswith("model.tar.gz"):
            # A model artifact isn't SQL-queryable (the panel hides the SQL button).
            return ("-- A trained model isn't queryable with SQL.\n"
                    "-- Use the Python insert instead: load_model_local(...) + predict(...).")
        return f"SELECT * FROM '{source_id}' LIMIT 100"
