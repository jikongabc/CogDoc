from cogdoc.agents.answer_markers import NO_RELEVANT_CONTENT_MARKER
from cogdoc.agents.conversation_memory import (
    clean_answer_for_memory,
    extract_chat_turn,
    extract_final_answer,
    format_recent_chat_history,
)
from cogdoc.memory.manager import MemoryPolicy, build_memory_context


# 验证 extract chat turn skips qa fallback answer 场景。
def test_extract_chat_turn_skips_qa_fallback_answer():
    assert (
        extract_chat_turn(
            "qa",
            {
                "answer": f"{NO_RELEVANT_CONTENT_MARKER}，建议查阅更多资料。",
                "critique": "",
                "reranked_docs": [],
            },
            "它的作者是谁？",
            timestamp="2026-01-01T00:00:00",
        )
        == []
    )


# 验证 extract chat turn skips unvalidated qa answer 场景。
def test_extract_chat_turn_skips_unvalidated_qa_answer():
    assert (
        extract_chat_turn(
            "qa",
            {
                "answer": "未经过引用校验的答案",
                "reranked_docs": [{"text": "x", "meta": {}}],
            },
            "问题",
            timestamp="2026-01-01T00:00:00",
        )
        == []
    )


# 验证 extract chat turn keeps valid qa answer 场景。
def test_extract_chat_turn_keeps_valid_qa_answer():
    turn = extract_chat_turn(
        "qa",
        {
            "answer": "有效答案。[a.pdf:P1]",
            "critique": "",
            "reranked_docs": [{"text": "x", "meta": {}}],
        },
        "问题",
        timestamp="2026-01-01T00:00:00",
    )

    assert turn == [
        {"role": "user", "content": "问题", "timestamp": "2026-01-01T00:00:00"},
        {
            "role": "assistant",
            "content": "有效答案。[a.pdf:P1]",
            "timestamp": "2026-01-01T00:00:00",
        },
    ]


# 验证 extract chat turn skips any task with critique 场景。
def test_extract_chat_turn_skips_any_task_with_critique():
    assert (
        extract_chat_turn(
            "compare",
            {
                "answer": "## 方法\n- **a.pdf**：方法 A。\n\n## 引用校验警告\n单元格引用错误。",
                "critique": "单元格引用错误。",
            },
            "对比两篇文档",
            timestamp="2026-01-01T00:00:00",
        )
        == []
    )


def test_extract_chat_turn_skips_shadow_candidate_that_would_intervene():
    assert (
        extract_chat_turn(
            "qa",
            {
                "answer": "可能不受证据支持的回答。",
                "critique": "",
                "reranked_docs": [{"text": "证据"}],
                "claim_verification_rollout": {
                    "mode": "shadow",
                    "decision": "would_block",
                    "would_intervene": True,
                },
            },
            "问题",
        )
        == []
    )


# 验证 extract chat turn skips unknown task 场景。
def test_extract_chat_turn_skips_unknown_task():
    assert (
        extract_chat_turn(
            "unknown",
            {"answer": "我是面向本地知识库的文档问答助手。"},
            "你好",
            timestamp="2026-01-01T00:00:00",
        )
        == []
    )


# 验证 extract chat turn skips compare without profiles 场景。
def test_extract_chat_turn_skips_compare_without_profiles():
    # 点名不足等早退只产出引导消息、无 document_profiles，不应写入记忆。
    assert (
        extract_chat_turn(
            "compare",
            {"answer": "请在对比问题中点名至少 2 篇要对比的文件。"},
            "帮我对比一下",
            timestamp="2026-01-01T00:00:00",
        )
        == []
    )


# 验证 extract chat turn keeps valid compare answer 场景。
def test_extract_chat_turn_keeps_valid_compare_answer():
    turn = extract_chat_turn(
        "compare",
        {
            "answer": "## 方法\n- **a.pdf**：方法 A。[a.pdf:P1]",
            "critique": "",
            "document_profiles": [{"source": "a.pdf", "cells": []}],
        },
        "对比 a.pdf 和 b.pdf",
        timestamp="2026-01-01T00:00:00",
    )

    assert turn[0]["content"] == "对比 a.pdf 和 b.pdf"
    assert turn[1]["content"] == "## 方法\n- **a.pdf**：方法 A。[a.pdf:P1]"


# 验证 extract chat turn skips summary without sections 场景。
def test_extract_chat_turn_skips_summary_without_sections():
    # 未指定文件等早退只产出引导消息、无 summary_section_results，不应写入记忆。
    assert (
        extract_chat_turn(
            "summary",
            {"answer": "请在摘要问题中明确指定要总结的文件名。"},
            "帮我总结一下",
            timestamp="2026-01-01T00:00:00",
        )
        == []
    )


# 验证 extract chat turn keeps valid summary answer 场景。
def test_extract_chat_turn_keeps_valid_summary_answer():
    turn = extract_chat_turn(
        "summary",
        {
            "answer": "# a.pdf 结构化摘要\n## 方法\n方法 A。[a.pdf:P1]",
            "critique": "",
            "summary_section_results": [
                {"section_id": "method", "content": "方法 A。[a.pdf:P1]"}
            ],
        },
        "总结 a.pdf",
        timestamp="2026-01-01T00:00:00",
    )

    assert turn[0]["content"] == "总结 a.pdf"
    assert turn[1]["content"].startswith("# a.pdf 结构化摘要")


# 验证 clean answer for memory strips citation warning 场景。
def test_clean_answer_for_memory_strips_citation_warning():
    answer = clean_answer_for_memory(
        "## 方法\n- **a.pdf**：方法 A。\n\n## 引用校验警告\n单元格引用错误。"
    )

    assert answer == "## 方法\n- **a.pdf**：方法 A。"


# 验证 extract final answer reads compare messages fallback 场景。
def test_extract_final_answer_reads_compare_messages_fallback():
    assert (
        extract_final_answer(
            "compare",
            {
                "messages": [{"role": "assistant", "content": "对比答案"}],
            },
        )
        == "对比答案"
    )


# 验证 clean answer for memory without warning is noop 场景。
def test_clean_answer_for_memory_without_warning_is_noop():
    assert clean_answer_for_memory("正常答案") == "正常答案"


# 验证 format recent chat history uses recent messages only 场景。
def test_format_recent_chat_history_uses_recent_messages_only():
    history = [
        {"role": "user", "content": f"msg-{idx:02d}", "timestamp": None}
        for idx in range(14)
    ]

    rendered = format_recent_chat_history(history, limit=12)
    assert "msg-00" not in rendered
    assert "msg-01" not in rendered
    assert "msg-02" in rendered
    assert "msg-13" in rendered


# 验证已组装的分层记忆不会被再次裁剪。
def test_format_recent_chat_history_serializes_built_context():
    history = build_memory_context(
        [{"role": "user", "content": f"msg-{idx:02d}"} for idx in range(14)],
        {"decisions": ["历史决策"]},
        [{"content": "长期偏好", "importance": 1.0}],
        MemoryPolicy(short_term_message_limit=12),
    )

    rendered = format_recent_chat_history(history, limit=12)

    assert "记忆: 【长期记忆】" in rendered
    assert "记忆: 【中期记忆】" in rendered
    assert "长期偏好" in rendered
    assert "历史决策" in rendered
    assert "长期记忆: " not in rendered
    assert "中期记忆: " not in rendered
    assert "msg-00" not in rendered
    assert "msg-04" in rendered
