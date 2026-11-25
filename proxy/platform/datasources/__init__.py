"""
Data Source Providers — schema discovery, preview, and snippet generation.

Each data source type implements the DataSourceProvider interface and is
registered in registry.py. Prefer the registry helpers (get_provider,
all_providers, discover_all, provider_metadata, reader_docs, sql_syntax_docs)
over importing concrete provider classes directly.
"""

# Public surface: the INTERFACE + shared data types, and the REGISTRY helpers.
# Concrete provider classes (S3SchemaProvider, AthenaSchemaProvider, ...) are
# INTENTIONALLY not re-exported here — callers must depend on the interface and
# reach providers through the registry (get_provider/all_providers/discover_*),
# never by importing a concrete type. The registry itself imports the concrete
# classes from their submodules (that's its job as the composition root).
from .interface import DataSourceProvider, SourceSchema, ColumnInfo, SourceRef
from . import registry
from .registry import (
    get_provider,
    all_providers,
    provider_classes,
    provider_metadata,
    discover_all,
    discover_all_sync,
    discover_full_catalog,
    discover_full_catalog_sync,
    reader_docs,
    sql_syntax_docs,
)

__all__ = [
    # Interface + shared data types
    "DataSourceProvider",
    "SourceSchema",
    "ColumnInfo",
    "SourceRef",
    # Registry (the ONLY way callers should reach concrete providers)
    "registry",
    "get_provider",
    "all_providers",
    "provider_classes",
    "provider_metadata",
    "discover_all",
    "discover_all_sync",
    "discover_full_catalog",
    "discover_full_catalog_sync",
    "reader_docs",
    "sql_syntax_docs",
]
