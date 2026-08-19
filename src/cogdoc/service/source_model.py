"""Backward-compatible imports for the source-domain contract.

The implementation lives at the package boundary so low-level tools do not
depend on the service orchestration layer.
"""

from cogdoc.source_model import (
    LEGACY_CONNECTOR_TYPE,
    SOURCE_CONTRACT_VERSION,
    SourceDocument,
    SourceKind,
    SourceLocation,
    SourceVersion,
    build_source_id,
    build_version_id,
    canonical_origin_uri,
    stamp_source_contract,
)

__all__ = [
    "LEGACY_CONNECTOR_TYPE",
    "SOURCE_CONTRACT_VERSION",
    "SourceDocument",
    "SourceKind",
    "SourceLocation",
    "SourceVersion",
    "build_source_id",
    "build_version_id",
    "canonical_origin_uri",
    "stamp_source_contract",
]
