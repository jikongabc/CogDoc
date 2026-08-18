from datetime import datetime
from collections.abc import Mapping, Sequence
from typing import Any
from cogdoc.agents.answer_markers import (
    CITATION_WARNING_HEADING,
    NO_RELEVANT_CONTENT_MARKER,
)


CHAT_HISTORY_MESSAGE_LIMIT = 12

ROLE_LABELS = {
    "user": "用户",
    "assistant": "助手",
    "memory": "记忆",
}


# 格式化 recent chat history。
def format_recent_chat_history(
    chat_history: Sequence[Mapping[str, Any]] | None,
    limit: int = CHAT_HISTORY_MESSAGE_LIMIT,
    max_chars_per_message: int = 500,
) -> str:
    if not chat_history:
        return ""

    lines = []
    for message in chat_history[-limit:]:
        role = str(message.get("role", "")).strip()
        content = str(message.get("content", "")).strip()
        if not content:
            continue

        compact_content = " ".join(content.split())
        if len(compact_content) > max_chars_per_message:
            compact_content = compact_content[:max_chars_per_message].rstrip() + "..."

        label = ROLE_LABELS.get(role, role or "未知")
        lines.append(f"{label}: {compact_content}")

    return "\n".join(lines)


# 读取内容。
def _message_content(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("content", "")).strip()
    return str(getattr(message, "content", "")).strip()


# 完成 提取流程final回答 处理。
def extract_final_answer(task_type: str, output: Mapping[str, Any]) -> str:
    answer = str(output.get("answer", "") or "").strip()
    if task_type == "compare" and not answer:
        messages = output.get("messages", [])
        if messages:
            answer = _message_content(messages[-1])
    return answer


# 清理 answer for memory。
def clean_answer_for_memory(answer: str) -> str:
    marker_index = answer.find(CITATION_WARNING_HEADING)
    if marker_index >= 0:
        return answer[:marker_index].rstrip()
    return answer.strip()


# 完成 提取流程chatturn 处理。
def extract_chat_turn(
    task_type: str,
    output: Mapping[str, Any],
    query: str,
    timestamp: str | None = None,
) -> list[dict]:
    if task_type not in {"qa", "compare", "summary"}:
        return []

    answer = clean_answer_for_memory(extract_final_answer(task_type, output))
    query = (query or "").strip()
    if not query or not answer:
        return []

    if output.get("critique"):
        return []

    rollout = output.get("claim_verification_rollout")
    if (
        isinstance(rollout, Mapping)
        and str(rollout.get("mode") or "") == "shadow"
        and bool(rollout.get("would_intervene"))
    ):
        # Shadow mode releases the answer for observation, but a candidate that
        # enforce mode would repair/block must never become Agent memory.
        return []

    if task_type == "qa":
        if "critique" not in output:
            return []
        if not output.get("reranked_docs"):
            return []
        if NO_RELEVANT_CONTENT_MARKER in answer:
            return []
    elif task_type == "compare":
        # 只把真正产出对比画像的 compare 结果写入记忆，避免引导消息污染指代消解。
        if not output.get("document_profiles"):
            return []
    elif task_type == "summary":
        # 与 qa 对齐：未真正产出章节摘要（未指定文件/加载失败/章节为空等早退）不写入记忆。
        if not output.get("summary_section_results"):
            return []

    timestamp = timestamp or datetime.now().isoformat(timespec="seconds")
    return [
        {"role": "user", "content": query, "timestamp": timestamp},
        {"role": "assistant", "content": answer, "timestamp": timestamp},
    ]
