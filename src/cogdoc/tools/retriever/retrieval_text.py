from cogdoc.graph.state import RetrievedDoc


# 构造检索索引用文本。
def retrieval_text(doc: RetrievedDoc) -> str:
    # 来源、章节路径和定位上下文只参与召回/向量化，原始正文仍作为返回内容。
    meta = doc.get("meta", {})
    source = str(meta.get("source", "") or "").strip()
    source_type = str(meta.get("source_type", "document") or "document")
    section_path = str(meta.get("section_path", "") or "").strip()
    chunk_type = str(meta.get("chunk_type", "") or "").strip()
    context = str(meta.get("context", "") or "").strip()
    text = str(doc.get("text", "") or "").strip()
    location = []
    if source and source_type != "derived_knowledge":
        location.append(f"来源：{source}")
    if section_path:
        location.append(f"章节：{section_path}")
    page_start = meta.get("page_start")
    page_end = meta.get("page_end")
    if source_type != "derived_knowledge" and page_start is not None:
        page_label = str(page_start)
        if page_end is not None and str(page_end) != page_label:
            page_label = f"{page_label}-{page_end}"
        location.append(f"页码：{page_label}")
    if chunk_type and chunk_type != "prose":
        location.append(f"内容类型：{chunk_type}")
    return "\n\n".join(part for part in ("\n".join(location), context, text) if part)
