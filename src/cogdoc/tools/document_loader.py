import os
import re
from typing import Iterable, List, Optional, Tuple
from cogdoc.graph.state import RetrievedDoc


# 完成 sortdocument分块列表 处理。
def sort_document_chunks(chunks: Iterable[RetrievedDoc]) -> List[RetrievedDoc]:
    return sorted(
        chunks,
        key=lambda doc: (
            str(doc.get("meta", {}).get("source", "")),
            int(
                doc.get("meta", {}).get(
                    "page_start", doc.get("meta", {}).get("page", 0)
                )
            ),
            int(
                doc.get("meta", {}).get("page_end", doc.get("meta", {}).get("page", 0))
            ),
            int(
                doc.get("meta", {}).get(
                    "local_chunk_index", doc.get("meta", {}).get("chunk_index", 0)
                )
            ),
            str(doc.get("meta", {}).get("chunk_id", "")),
        ),
    )


# 列出 sources。
def list_sources(chunks: Iterable[RetrievedDoc]) -> List[str]:
    sources = {
        str(doc.get("meta", {}).get("source", ""))
        for doc in chunks
        if doc.get("meta", {}).get("source")
    }
    return sorted(sources)


# 加载 source chunks。
def load_source_chunks(
    chunks: Iterable[RetrievedDoc], source: str
) -> List[RetrievedDoc]:
    return sort_document_chunks(
        doc for doc in chunks if str(doc.get("meta", {}).get("source", "")) == source
    )


# 常见汉字区间，用于判断 stem 边缘是否被同为中文的字符续接。
_CJK_RANGE = "一-鿿"


# 判断 cjk 是否成立。
def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


# 返回matchstart。
def _source_match_start(
    query_lower: str, name_lower: str, allow_trailing_dot: bool
) -> Optional[int]:
    if not name_lower:
        return None

    left_exclude = r"a-z0-9_\-."
    if allow_trailing_dot:
        # 完整文件名带 .pdf 强锚点，边界保持宽松，只防右侧粘连更长点号文件。
        right_boundary = r"(?![a-z0-9_\-]|\.[a-z0-9_\-])"
    else:
        # stem 匹配同时排除点号变体和中文复合词误命中。
        right_exclude = r"a-z0-9_\-."
        if _is_cjk(name_lower[-1]):
            right_exclude += _CJK_RANGE
        if _is_cjk(name_lower[0]):
            left_exclude += _CJK_RANGE
        right_boundary = rf"(?![{right_exclude}])"

    match = re.search(
        rf"(?<![{left_exclude}]){re.escape(name_lower)}{right_boundary}", query_lower
    )
    if match is None:
        return None
    return match.start()


# 完成 full来源matchstart 处理。
def _full_source_match_start(query_lower: str, source_lower: str) -> Optional[int]:
    # 完整文件名后允许句末英文句点，例如 "compare a.pdf and b.pdf."。
    return _source_match_start(query_lower, source_lower, allow_trailing_dot=True)


# 完成 stem来源matchstart 处理。
def _stem_source_match_start(query_lower: str, stem_lower: str) -> Optional[int]:
    # stem 匹配不允许右侧点号，避免 stem "data" 命中 data.v2.pdf。
    return _source_match_start(query_lower, stem_lower, allow_trailing_dot=False)


# 选择来源for摘要。
def select_source_for_summary(query: str, sources: List[str]) -> Optional[str]:
    if not sources:
        return None
    if len(sources) == 1:
        return sources[0]

    query_lower = query.lower()

    # 中文用户通常不会在动词和文件名之间加空格（如“总结项目方案”）。
    # 仅当去掉明确的摘要指令后，剩余文本与完整文件名或 stem 完全一致时命中，
    # 避免放宽下面用于防止中文复合词误匹配的边界规则。
    normalized_query = query_lower.strip()
    for prefix in ("请总结", "帮我总结", "总结", "摘要", "概括", "归纳"):
        if not normalized_query.startswith(prefix):
            continue
        subject = normalized_query[len(prefix) :].strip(" ：:，,。.!！?？")
        for source in sources:
            source_lower = source.lower()
            stem_lower = os.path.splitext(source_lower)[0]
            if subject in {source_lower, stem_lower}:
                return source

    for source in sources:
        if _full_source_match_start(query_lower, source.lower()) is not None:
            return source

    for source in sources:
        stem = os.path.splitext(source)[0].lower()
        if len(stem) >= 3 and _stem_source_match_start(query_lower, stem) is not None:
            return source

    return None


# 选择源文件列表for对比。
def select_sources_for_compare(query: str, sources: List[str]) -> List[str]:
    if not sources:
        return []

    query_lower = query.lower()
    matched: List[Tuple[int, int, str]] = []

    for source_index, source in enumerate(sources):
        positions = []
        full_pos = _full_source_match_start(query_lower, source.lower())
        if full_pos is not None:
            positions.append(full_pos)
        stem = os.path.splitext(source)[0].lower()
        stem_pos = (
            _stem_source_match_start(query_lower, stem) if len(stem) >= 3 else None
        )
        if stem_pos is not None:
            positions.append(stem_pos)
        if positions:
            matched.append((min(positions), source_index, source))

    if len(matched) < 2:
        return []
    return [source for _, _, source in sorted(matched)]
