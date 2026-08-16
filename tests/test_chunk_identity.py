import pytest
from cogdoc.tools.chunk_identity import build_chunk_id, build_parent_chunk_id
from cogdoc.tools.chunk_identity import (
    CHUNKING_STRATEGY_VERSION,
    DEFAULT_CHUNK_CONTEXT_CHARS,
    CHUNK_IDENTITY_VERSION,
    DEFAULT_CHUNK_CHAR_OVERLAP,
    DEFAULT_CHUNK_CHAR_SIZE,
    MIN_CHUNK_CHARS,
)
from cogdoc.tools.chunker import chunk_paper, summarize_chunks


SOURCE_SHA = "a" * 64


# 构造测试页。
def _page(page: int, text: str) -> dict:
    # 构造最小 ParsedPage 输入。
    return {
        "page": page,
        "source": "paper.pdf",
        "text": text,
        "is_ocr_fallback": False,
    }


# 验证 chunk id contract uses source hash name page span and local index。
def test_chunk_id_contract_uses_source_hash_name_page_span_and_local_index():
    # chunk_id 必须包含文件哈希、文件名、页跨度和局部序号。
    assert (
        build_chunk_id(SOURCE_SHA, "paper.pdf", 2, 3, 4)
        == f"sha256:{SOURCE_SHA}:src:paper.pdf:p2-p3:c4"
    )


# 验证 chunk id distinguishes same content different name。
def test_chunk_id_distinguishes_same_content_different_name():
    # 同内容不同名文档必须得到不同 chunk_id，否则删一个会误伤另一个。
    a = build_chunk_id(SOURCE_SHA, "a.pdf", 1, 1, 0)
    b = build_chunk_id(SOURCE_SHA, "b.pdf", 1, 1, 0)
    assert a != b


# 验证 chunk identity version includes chunking parameters。
def test_chunk_identity_version_includes_chunking_parameters():
    # 切块参数变化必须让 manifest 失效。
    assert f"cs{DEFAULT_CHUNK_CHAR_SIZE}" in CHUNK_IDENTITY_VERSION
    assert f"ov{DEFAULT_CHUNK_CHAR_OVERLAP}" in CHUNK_IDENTITY_VERSION
    assert f"min{MIN_CHUNK_CHARS}" in CHUNK_IDENTITY_VERSION
    assert f"ctx{DEFAULT_CHUNK_CONTEXT_CHARS}" in CHUNK_IDENTITY_VERSION
    assert "parent_child_section_index" in CHUNK_IDENTITY_VERSION
    assert CHUNKING_STRATEGY_VERSION in CHUNK_IDENTITY_VERSION


# 验证章节父块使用文件身份和章节序号构造稳定标识。
def test_parent_chunk_id_contract_uses_source_identity_and_section_index():
    assert (
        build_parent_chunk_id(SOURCE_SHA, "paper.pdf", 3)
        == f"sha256:{SOURCE_SHA}:src:paper.pdf:section:3"
    )

    with pytest.raises(ValueError, match="section_index"):
        build_parent_chunk_id(SOURCE_SHA, "paper.pdf", -1)


# 验证 chunk id requires source sha256。
def test_chunk_id_requires_source_sha256():
    # 没有文件哈希不能生成稳定身份。
    with pytest.raises(ValueError):
        build_chunk_id("", "paper.pdf", 1, 1, 0)


# 验证 chunk id requires source name。
def test_chunk_id_requires_source_name():
    # 没有文件名不能生成稳定身份。
    with pytest.raises(ValueError):
        build_chunk_id(SOURCE_SHA, "", 1, 1, 0)


# 验证 chunker requires source sha256。
def test_chunker_requires_source_sha256():
    # chunker 必须显式接收文件哈希。
    with pytest.raises(ValueError):
        chunk_paper([_page(1, "正文足够长，可以触发稳定身份契约校验。" * 4)])


# 验证 chunker writes stable chunk identity fields。
def test_chunker_writes_stable_chunk_identity_fields():
    # chunker 输出必须携带完整身份字段。
    text = "第一段用于测试稳定切块身份。" * 12
    chunks = chunk_paper(
        [_page(1, text)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=120,
        chunk_char_overlap=0,
    )

    assert chunks
    first_meta = chunks[0]["meta"]
    assert first_meta["source_sha256"] == SOURCE_SHA
    assert first_meta["local_chunk_index"] == 0
    assert first_meta["chunk_id"] == build_chunk_id(
        SOURCE_SHA,
        first_meta["source"],
        first_meta["page_start"],
        first_meta["page_end"],
        first_meta["local_chunk_index"],
    )


# 验证 semantic chunker keeps sentence boundaries and overlap。
def test_semantic_chunker_keeps_sentence_boundaries_and_overlap():
    # 优先按句子组成 chunk，overlap 复用完整语义单元，避免从句中间开头。
    sentences = [
        "第一句说明背景和范围。",
        "第二句说明目标和对象。",
        "第三句说明流程和方法。",
        "第四句说明结果和产出。",
        "第五句说明限制和注意。",
    ]
    chunks = chunk_paper(
        [_page(1, "".join(sentences))],
        source_sha256=SOURCE_SHA,
        chunk_char_size=40,
        chunk_char_overlap=12,
        chunk_context_chars=20,
    )

    assert len(chunks) >= 2
    assert all(len(chunk["text"]) <= 40 for chunk in chunks)
    assert chunks[0]["text"].endswith("。")
    assert chunks[1]["text"].startswith(sentences[1])
    assert sentences[1] in chunks[0]["text"]
    assert "前文：" in chunks[1]["meta"]["context"]
    assert sentences[0] in chunks[1]["meta"]["context"]
    assert sentences[3] in chunks[0]["meta"]["context"]
    assert sentences[4] not in chunks[0]["meta"]["context"]


# 验证 semantic chunker caps combined semantic units。
def test_semantic_chunker_caps_combined_semantic_units():
    # 多个短语义单元组合时，最终 chunk 仍必须遵守 chunk_char_size 硬上限。
    text = "一二三四五六七八九十。一二三四五六七八九十。一二三四五六七八九十。甲乙丙丁戊己庚辛壬癸子丑申酉戌亥。"
    chunks = chunk_paper(
        [_page(1, text)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=40,
        chunk_char_overlap=0,
    )

    assert chunks
    assert all(len(chunk["text"]) <= 40 for chunk in chunks)


# 验证 long text without punctuation still respects max size。
def test_long_text_without_punctuation_still_respects_max_size():
    # 没有段落/标点边界时退回固定窗口，但每块仍不超过 chunk_char_size。
    chunks = chunk_paper(
        [_page(1, "x" * 180)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=50,
        chunk_char_overlap=0,
    )

    assert chunks
    assert all(len(chunk["text"]) <= 50 for chunk in chunks)


# 验证章节边界成为硬切分边界，并为每个 child 写入稳定父级结构元数据。
def test_chunker_writes_parent_child_structure_without_crossing_sections():
    introduction = "1 Introduction\n" + "背景信息用于说明研究问题和范围。" * 10
    methods = "2 Methods\n" + "方法信息用于说明训练流程和参数。" * 10
    chunks = chunk_paper(
        [_page(1, f"{introduction}\n\n{methods}")],
        source_sha256=SOURCE_SHA,
        chunk_char_size=90,
        chunk_char_overlap=20,
        chunk_context_chars=120,
    )

    assert chunks
    assert all(
        not ("Introduction" in chunk["text"] and "Methods" in chunk["text"])
        for chunk in chunks
    )
    grouped: dict[str, list[dict]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk["meta"]["parent_chunk_id"], []).append(chunk)
    assert len(grouped) == 2
    assert {chunk["meta"]["section_path"] for chunk in chunks} == {
        "Introduction",
        "Methods",
    }
    assert all(
        [doc["meta"]["child_index_in_parent"] for doc in siblings]
        == list(range(len(siblings)))
        for siblings in grouped.values()
    )
    assert all(
        all(doc["meta"]["parent_child_count"] == len(siblings) for doc in siblings)
        for siblings in grouped.values()
    )
    assert all(chunk["meta"]["parent_char_count"] > 0 for chunk in chunks)
    introduction_chunks = [
        chunk for chunk in chunks if chunk["meta"]["section_title"] == "Introduction"
    ]
    assert all(
        "Methods" not in chunk["meta"].get("context", "")
        for chunk in introduction_chunks
    )


# 无可靠标题的旧式文档不伪造父级结构，运行时会使用邻块兼容路径。
def test_chunker_keeps_unstructured_documents_without_parent_metadata():
    chunks = chunk_paper(
        [_page(1, "这是连续的普通正文，用于验证保守结构识别。" * 12)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=100,
        chunk_char_overlap=0,
    )

    assert chunks
    assert all("parent_chunk_id" not in chunk["meta"] for chunk in chunks)


# 结构硬边界不能让短 preamble 或短章节从索引中消失。
def test_chunker_preserves_short_structured_sections_and_preamble():
    text = (
        "Cover\n\n"
        "1 Introduction\nTiny result.\n\n"
        "2 Methods\n" + "方法信息用于说明训练流程和参数。" * 8
    )
    chunks = chunk_paper(
        [_page(1, text)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=90,
        chunk_char_overlap=20,
        chunk_context_chars=120,
    )

    assert any(chunk["text"] == "Cover" for chunk in chunks)
    assert any("Tiny result." in chunk["text"] for chunk in chunks)
    short_intro = next(
        chunk
        for chunk in chunks
        if chunk["meta"].get("section_title") == "Introduction"
    )
    assert short_intro["meta"]["child_index_in_parent"] == 0
    assert short_intro["meta"]["section_path"] == "Introduction"
    preamble = next(chunk for chunk in chunks if chunk["text"] == "Cover")
    assert preamble["meta"]["section_level"] == 0
    assert preamble["meta"]["child_index_in_parent"] == 0


# 没有结构标题的短文本继续使用旧 MIN_CHUNK_CHARS 过滤行为。
def test_chunker_keeps_legacy_minimum_for_short_unstructured_text():
    chunks = chunk_paper(
        [_page(1, "Tiny unstructured text.")],
        source_sha256=SOURCE_SHA,
        chunk_char_size=90,
        chunk_char_overlap=0,
    )

    assert chunks == []


@pytest.mark.parametrize("overlap", [80, 120])
def test_chunker_rejects_overlap_not_smaller_than_chunk_size(overlap):
    with pytest.raises(
        ValueError, match="chunk_char_overlap must be smaller than chunk_char_size"
    ):
        chunk_paper(
            [_page(1, "1 Introduction\n" + "正文内容足够长。" * 20)],
            source_sha256=SOURCE_SHA,
            chunk_char_size=80,
            chunk_char_overlap=overlap,
        )


def test_chunker_isolates_tables_lists_and_fenced_code_with_auditable_metadata():
    text = (
        "# API Guide\n\n"
        "Intro paragraph explains authentication and endpoint behavior in detail.\n\n"
        "| Field | Type | Meaning |\n"
        "|---|---|---|\n"
        "| user_id | string | User identity |\n"
        "| limit | integer | Result limit |\n\n"
        "Steps:\n"
        "- Create a token\n"
        "- Send the request\n"
        "- Check the response\n\n"
        "```python\n"
        "def fetch(user_id):\n"
        "    return client.get(f'/users/{user_id}')\n"
        "```\n\n"
        "Final paragraph contains troubleshooting and operational guidance."
    )

    chunks = chunk_paper(
        [_page(1, text)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=140,
        chunk_char_overlap=20,
    )

    by_type = {chunk["meta"]["chunk_type"]: chunk for chunk in chunks}
    assert {"prose", "table", "list", "code"}.issubset(by_type)
    assert by_type["table"]["text"].startswith("| Field |")
    assert by_type["list"]["text"].startswith("Steps:\n- Create")
    assert by_type["code"]["text"].startswith("```python")
    assert by_type["code"]["text"].endswith("```")
    assert all(
        chunk["meta"]["chunk_char_count"] == len(chunk["text"]) for chunk in chunks
    )
    assert all(0.0 < chunk["meta"]["chunk_quality_score"] <= 1.0 for chunk in chunks)
    assert all(
        chunk["meta"]["chunking_strategy_version"] == CHUNKING_STRATEGY_VERSION
        for chunk in chunks
    )


def test_chunker_adapts_dense_cjk_prose_but_keeps_structured_blocks_hard_bounded():
    prose = "这是用于验证中文高信息密度自适应切块的完整句子。" * 30
    chunks = chunk_paper(
        [_page(1, prose)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=100,
        chunk_char_overlap=0,
    )

    assert chunks
    assert all(len(chunk["text"]) <= 85 for chunk in chunks)
    assert {chunk["meta"]["document_profile"] for chunk in chunks} == {"cjk_prose"}


def test_chunker_filters_noise_and_exact_duplicates_within_one_parent():
    text = (
        "# Notes\n\n"
        "------------------------\n\n"
        "同一条有效说明用于测试重复过滤。\n\n"
        "同一条有效说明用于测试重复过滤。"
    )
    chunks = chunk_paper(
        [_page(1, text)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=20,
        chunk_char_overlap=0,
    )

    combined = "\n".join(chunk["text"] for chunk in chunks)
    assert "------------------------" not in combined
    assert combined.count("同一条有效说明用于测试重复过滤。") == 1


def test_chunking_stats_are_stable_and_contain_no_document_text():
    chunks = chunk_paper(
        [_page(1, "1 Introduction\n" + "背景说明用于统计。" * 20)],
        source_sha256=SOURCE_SHA,
        chunk_char_size=80,
        chunk_char_overlap=0,
    )

    stats = summarize_chunks(chunks)

    assert stats.chunk_count == len(chunks)
    assert stats.char_count == sum(len(chunk["text"]) for chunk in chunks)
    assert stats.max_chunk_chars <= 80
    assert sum(stats.chunk_type_counts.values()) == len(chunks)
