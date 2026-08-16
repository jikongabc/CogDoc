DEFAULT_CHUNK_CHAR_SIZE = 600
DEFAULT_CHUNK_CHAR_OVERLAP = 60
MIN_CHUNK_CHARS = 30
DEFAULT_CHUNK_CONTEXT_CHARS = 160
CHUNKING_STRATEGY_VERSION = "adaptive-structural-v2"
DOCUMENT_ID_VERSION = "source-name-v1"

# 切块和检索索引输入变化时必须同步更新版本。
CHUNK_IDENTITY_BASE_VERSION = (
    "source_sha256_name_page_span_local_v7_document_acl_"
    "parent_child_section_index_adaptive_blocks"
)
CHUNK_IDENTITY_VERSION = (
    f"{CHUNK_IDENTITY_BASE_VERSION}"
    f"_cs{DEFAULT_CHUNK_CHAR_SIZE}"
    f"_ov{DEFAULT_CHUNK_CHAR_OVERLAP}"
    f"_min{MIN_CHUNK_CHARS}"
    f"_ctx{DEFAULT_CHUNK_CONTEXT_CHARS}"
    f"_strategy_{CHUNKING_STRATEGY_VERSION}"
)


def build_document_id(source_name: str) -> str:
    """Return the stable document ACL identity within one knowledge base."""

    if not isinstance(source_name, str) or not source_name:
        raise ValueError("source_name is required to build a stable document_id")
    import hashlib

    digest = hashlib.sha256(
        b"cogdoc-document-id-source-name-v1\0" + source_name.encode("utf-8")
    ).hexdigest()
    return f"doc-{digest}"


# 构建分块id。
def build_chunk_id(
    source_sha256: str,
    source_name: str,
    page_start: int,
    page_end: int,
    local_chunk_index: int,
) -> str:
    # chunk_id 由文件内容、文件名和 chunk 局部位置共同决定；纳入文件名以区分同内容不同名文档。
    if not source_sha256:
        raise ValueError("source_sha256 is required to build a stable chunk_id")
    if not source_name:
        raise ValueError("source_name is required to build a stable chunk_id")

    return (
        f"sha256:{source_sha256}:src:{source_name}"
        f":p{int(page_start)}-p{int(page_end)}:c{int(local_chunk_index)}"
    )


# 构建章节父块的稳定身份；父块仅用于上下文组织，引用仍使用子块 chunk_id。
def build_parent_chunk_id(
    source_sha256: str,
    source_name: str,
    section_index: int,
) -> str:
    if not source_sha256:
        raise ValueError("source_sha256 is required to build a stable parent_chunk_id")
    if not source_name:
        raise ValueError("source_name is required to build a stable parent_chunk_id")
    if section_index < 0:
        raise ValueError("section_index must be non-negative")

    return f"sha256:{source_sha256}:src:{source_name}:section:{int(section_index)}"
