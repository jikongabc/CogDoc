from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cogdoc.api.schemas import ChunkPreview
from cogdoc.graph.state import RetrievedDoc
from cogdoc.service.ingest_service import _chunk_text_hash
from cogdoc.service.kb_readers import kb_read_lease

_CHUNK_PREVIEW_CHARS = 360
_CONTEXT_PREVIEW_CHARS = 180


# 构建短文本预览。
def preview_text(text: Any, limit: int) -> str:
    return " ".join(("" if text is None else str(text)).split())[:limit]


# 读取来源文件分块。
def source_chunks(kb_id: str, source: str) -> list[RetrievedDoc]:
    from cogdoc.service.retriever_factory import RetrieverFactory

    with kb_read_lease(kb_id):
        return RetrieverFactory.get_engine(kb_id).load_source_chunks(source)


# 构建 chunk 预览。
def chunk_preview(
    doc: Mapping[str, Any], anchor_text: str | None = None
) -> ChunkPreview:
    raw_meta = doc.get("meta")
    meta: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
    page = meta.get("page")
    text = str(doc.get("text") or "")
    anchor = str(anchor_text or "").strip()
    return ChunkPreview(
        chunk_id=str(meta.get("chunk_id", "")),
        document_id=str(meta.get("document_id", "")),
        chunk_index=meta.get("chunk_index"),
        source=str(meta.get("source", "") or ""),
        source_sha256=str(meta.get("source_sha256", "") or ""),
        page=page,
        page_start=meta.get("page_start", page),
        page_end=meta.get("page_end", page),
        text_hash=_chunk_text_hash(text),
        anchor_hit=bool(anchor and anchor in text),
        text_preview=preview_text(text, _CHUNK_PREVIEW_CHARS),
        context_preview=preview_text(meta.get("context", ""), _CONTEXT_PREVIEW_CHARS),
    )
