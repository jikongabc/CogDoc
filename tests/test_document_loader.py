from cogdoc.tools.document_loader import (
    list_sources,
    load_source_chunks,
    select_sources_for_compare,
    select_source_for_summary,
    sort_document_chunks,
)
from cogdoc.tools.retriever.bm25_retriever import BM25Retriever


# 构造测试用文档。
def _doc(source: str, page: int, local_chunk_index: int, chunk_id: str = None) -> dict:
    return {
        "text": f"{source} p{page} c{local_chunk_index}",
        "meta": {
            "chunk_id": chunk_id or f"chunk:{source}:{local_chunk_index}",
            "source_sha256": f"sha:{source}",
            "local_chunk_index": local_chunk_index,
            "chunk_index": local_chunk_index,
            "source": source,
            "page": page,
            "page_start": page,
            "page_end": page,
            "origin": "file",
        },
    }


# 验证 sort document chunks uses source page and local index 场景。
def test_sort_document_chunks_uses_source_page_and_local_index():
    docs = [
        _doc("b.pdf", 1, 0),
        _doc("a.pdf", 2, 1),
        _doc("a.pdf", 1, 0),
    ]

    assert [doc["text"] for doc in sort_document_chunks(docs)] == [
        "a.pdf p1 c0",
        "a.pdf p2 c1",
        "b.pdf p1 c0",
    ]


# 验证 list sources is stable and unique 场景。
def test_list_sources_is_stable_and_unique():
    docs = [_doc("b.pdf", 1, 0), _doc("a.pdf", 1, 0), _doc("a.pdf", 2, 1)]

    assert list_sources(docs) == ["a.pdf", "b.pdf"]


# 验证 load source chunks filters single document 场景。
def test_load_source_chunks_filters_single_document():
    docs = [_doc("b.pdf", 1, 0), _doc("a.pdf", 2, 1), _doc("a.pdf", 1, 0)]

    loaded = load_source_chunks(docs, "a.pdf")

    assert [doc["text"] for doc in loaded] == ["a.pdf p1 c0", "a.pdf p2 c1"]


# 验证 select source for summary matches full name or stem 场景。
def test_select_source_for_summary_matches_full_name_or_stem():
    sources = ["paper-a.pdf", "paper-b.pdf"]

    assert select_source_for_summary("总结 paper-a.pdf", sources) == "paper-a.pdf"
    assert select_source_for_summary("总结 paper-b 的方法", sources) == "paper-b.pdf"
    assert select_source_for_summary("summarize paper-b.pdf.", sources) == "paper-b.pdf"


def test_select_source_for_summary_matches_adjacent_chinese_command_and_stem():
    sources = ["ACM竞赛简介.pdf", "大模型开发应用赛.pdf"]

    assert select_source_for_summary("总结大模型开发应用赛", sources) == "大模型开发应用赛.pdf"
    assert select_source_for_summary("请总结：大模型开发应用赛.pdf", sources) == "大模型开发应用赛.pdf"


# 验证 select source for summary does not match short stem 场景。
def test_select_source_for_summary_does_not_match_short_stem():
    assert (
        select_source_for_summary("总结 data-arch 的方法", ["a.pdf", "data-arch.pdf"])
        == "data-arch.pdf"
    )
    assert (
        select_source_for_summary("总结一个 ai 比赛", ["ai.pdf", "paper.pdf"]) is None
    )


# 验证 select source for summary does not match cjk stem inside compound word 场景。
def test_select_source_for_summary_does_not_match_cjk_stem_inside_compound_word():
    sources = ["技术方案.pdf", "b.pdf"]

    assert select_source_for_summary("总结 技术方案 的内容", sources) == "技术方案.pdf"
    assert select_source_for_summary("总结技术方案设计规范的内容", sources) is None


# 验证 select source for summary uses single source fallback 场景。
def test_select_source_for_summary_uses_single_source_fallback():
    assert select_source_for_summary("总结这篇文档", ["only.pdf"]) == "only.pdf"


# 验证 select source for summary returns none when ambiguous 场景。
def test_select_source_for_summary_returns_none_when_ambiguous():
    assert select_source_for_summary("总结这篇文档", ["a.pdf", "b.pdf"]) is None


# 验证 select sources for compare requires two explicit sources 场景。
def test_select_sources_for_compare_requires_two_explicit_sources():
    sources = ["paper-a.pdf", "paper-b.pdf", "paper-c.pdf"]

    assert select_sources_for_compare("对比 paper-a.pdf 和 paper-b.pdf", sources) == [
        "paper-a.pdf",
        "paper-b.pdf",
    ]
    assert select_sources_for_compare(
        "compare paper-a.pdf and paper-b.pdf.", sources
    ) == ["paper-a.pdf", "paper-b.pdf"]
    assert select_sources_for_compare("对比 paper-c.pdf 和 paper-a.pdf", sources) == [
        "paper-c.pdf",
        "paper-a.pdf",
    ]
    assert select_sources_for_compare("对比 paper-a 和 paper-c 的方法", sources) == [
        "paper-a.pdf",
        "paper-c.pdf",
    ]
    assert select_sources_for_compare("对比这些文档", sources) == []
    assert select_sources_for_compare("对比 paper-a.pdf", sources) == []


# 验证 select sources for compare does not substring match short filenames 场景。
def test_select_sources_for_compare_does_not_substring_match_short_filenames():
    assert select_sources_for_compare(
        "对比 data.pdf 和 b.pdf", ["a.pdf", "data.pdf", "b.pdf"]
    ) == ["data.pdf", "b.pdf"]


# 验证 select sources for compare does not match dotted filename variants 场景。
def test_select_sources_for_compare_does_not_match_dotted_filename_variants():
    sources = ["data.pdf", "data.v2.pdf", "b.pdf"]

    assert select_sources_for_compare("对比 data.v2.pdf 和 b.pdf", sources) == [
        "data.v2.pdf",
        "b.pdf",
    ]
    assert select_sources_for_compare("对比 data.pdf 和 b.pdf", sources) == [
        "data.pdf",
        "b.pdf",
    ]
    assert select_sources_for_compare("对比 data.pdf.old 和 b.pdf", sources) == []


# 验证 select sources for compare does not match cjk stem inside compound word 场景。
def test_select_sources_for_compare_does_not_match_cjk_stem_inside_compound_word():
    sources = ["技术方案.pdf", "数据报告.pdf"]

    assert select_sources_for_compare("对比 技术方案 和 数据报告", sources) == [
        "技术方案.pdf",
        "数据报告.pdf",
    ]
    assert (
        select_sources_for_compare("对比技术方案设计规范与数据报告体系", sources) == []
    )


# 验证 bm25 retriever exposes indexed document loader 场景。
def test_bm25_retriever_exposes_indexed_document_loader(tmp_path):
    retriever = BM25Retriever("summary_loader_test", persist_directory=str(tmp_path))
    retriever.index([_doc("b.pdf", 1, 0), _doc("a.pdf", 2, 1), _doc("a.pdf", 1, 0)])

    assert retriever.list_sources() == ["a.pdf", "b.pdf"]
    assert [doc["text"] for doc in retriever.load_source_chunks("a.pdf")] == [
        "a.pdf p1 c0",
        "a.pdf p2 c1",
    ]
