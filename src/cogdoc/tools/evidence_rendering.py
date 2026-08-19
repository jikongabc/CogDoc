from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from xml.sax.saxutils import escape

from cogdoc.tools.citation_ledger import EVIDENCE_ID_PLACEHOLDER


EVIDENCE_BLOCK_SEPARATOR = "\n\n"
EMPTY_EVIDENCE_CONTEXT = "（未检索到任何相关的参考本地知识库内容。）"

_XML_TEXT_ENTITIES = {"\r": "&#xD;"}
_XML_ATTRIBUTE_ENTITIES = {
    '"': "&quot;",
    "'": "&apos;",
    "\t": "&#x9;",
    "\n": "&#xA;",
    "\r": "&#xD;",
}


def _meta(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    value = doc.get("meta")
    return value if isinstance(value, Mapping) else {}


def _xml_text(value: Any) -> str:
    """Escape model-visible character data without changing plain text."""

    return escape(str(value), _XML_TEXT_ENTITIES)


def _xml_attribute(value: Any) -> str:
    """Escape an attribute with a stable, double-quoted XML representation."""

    return escape(str(value), _XML_ATTRIBUTE_ENTITIES)


def render_evidence_block(
    doc: Mapping[str, Any],
    *,
    text_override: str | None = None,
    evidence_id_override: str | None = None,
) -> str:
    """Render one evidence block exactly as the QA generator sees it."""

    meta = _meta(doc)
    retrieval = doc.get("retrieval")
    retrieval = retrieval if isinstance(retrieval, Mapping) else {}
    evidence_id = (
        evidence_id_override
        if evidence_id_override is not None
        else str(retrieval.get("evidence_id") or "").strip()
    )
    evidence_id_attribute = (
        f' evidence_id="{_xml_attribute(evidence_id)}"' if evidence_id else ""
    )
    body = (
        str(doc.get("text") or "") if text_override is None else text_override
    ).strip()
    if meta.get("source_type") == "derived_knowledge":
        knowledge_id = meta.get("knowledge_id") or str(
            meta.get("chunk_id", "")
        ).replace("knowledge:", "")
        certainty = meta.get("certainty", "")
        related_source = meta.get("related_source", "")
        chunk_context = str(meta.get("context", "") or "").strip()
        if chunk_context:
            body = f"来源说明：\n{chunk_context}\n\n内容：\n{body}"
        return (
            f'<Knowledge knowledge_id="{_xml_attribute(knowledge_id)}" '
            f'certainty="{_xml_attribute(certainty)}" '
            f'related_source="{_xml_attribute(related_source)}"'
            f"{evidence_id_attribute}>\n"
            f"{_xml_text(body)}\n"
            "</Knowledge>"
        )

    source = meta.get("source", "未知文件")
    page = meta.get("page", 1)
    source_version_id = str(meta.get("source_version_id") or "")
    source_location = meta.get("source_location")
    location_attribute = ""
    if isinstance(source_location, Mapping) and source_location:
        location_attribute = f' location="{_xml_attribute(json.dumps(dict(source_location), ensure_ascii=False, sort_keys=True, separators=(",", ":")))}"'
    version_attribute = (
        f' source_version_id="{_xml_attribute(source_version_id)}"'
        if source_version_id
        else ""
    )
    chunk_id = meta.get("chunk_id", meta.get("chunk_index", 0))
    section_path = str(meta.get("section_path", "") or "").strip()
    chunk_context = str(meta.get("context", "") or "").strip()
    if section_path:
        body = f"章节路径：{section_path}\n\n{body}"
    if chunk_context:
        body = f"定位上下文：\n{chunk_context}\n\n正文：\n{body}"
    return (
        f'<Document source="{_xml_attribute(source)}" '
        f'page="{_xml_attribute(page)}" '
        f'chunk_id="{_xml_attribute(chunk_id)}"'
        f"{version_attribute}{location_attribute}"
        f"{evidence_id_attribute}>\n"
        f"{_xml_text(body)}\n"
        "</Document>"
    )


def evidence_block_char_count(doc: Mapping[str, Any], text: str) -> int:
    # Evidence Pack 在最终展示顺序确定前尚不知道具体编号。定长占位符
    # 与 E001..E999 等长，因此选择阶段的字符预算仍与最终 prompt 精确一致。
    return len(
        render_evidence_block(
            doc,
            text_override=text,
            evidence_id_override=EVIDENCE_ID_PLACEHOLDER,
        )
    )


def render_evidence_context(docs: Sequence[Mapping[str, Any]]) -> str:
    if not docs:
        return EMPTY_EVIDENCE_CONTEXT
    return EVIDENCE_BLOCK_SEPARATOR.join(render_evidence_block(doc) for doc in docs)
