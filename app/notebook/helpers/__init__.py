"""
Notebook Helper Functions — Pre-loaded utility library.

These functions are available in every notebook cell without importing.
They wrap common data operations (load, save, plot, profile) into
simple one-liners that handle AWS auth, error formatting, and defaults.

Usage (in any cell):
    df = read_s3_csv("my-bucket", "data/sales.csv")
    df = read_dynamodb("my-table")
    plot_bar(df, x='product', y='revenue', title='Sales')
    to_s3_csv(df, "my-bucket", "exports/output.csv")
"""
from ._common import *  # noqa: F401,F403
from ._common import _log_read, _log_write, logger  # noqa: F401
from .readers import *  # noqa: F401,F403
from .logs import *  # noqa: F401,F403
from .aurora import *  # noqa: F401,F403
from .aurora import _aurora_execute  # noqa: F401  (sql_engine imports this private name)
from .bedrock_kb import *  # noqa: F401,F403
from .llm import *  # noqa: F401,F403
from .ml import *  # noqa: F401,F403
from .sagemaker import *  # noqa: F401,F403
from .cost import *  # noqa: F401,F403
from .env import *  # noqa: F401,F403
from .packages import *  # noqa: F401,F403
from .packages import _register_runtime  # noqa: F401  (server.py wires the runtime)
from .export import *  # noqa: F401,F403
from .viz import *  # noqa: F401,F403
from .profiling import *  # noqa: F401,F403
