import re
import unicodedata
from collections.abc import Mapping
from typing import Any, List

from pydantic import BaseModel, Field

from cogdoc.agents.conversation_memory import (
    CHAT_HISTORY_MESSAGE_LIMIT,
    format_recent_chat_history,
)
from cogdoc.agents.qa_generator import Generator
from cogdoc.agents.structured_output import invoke_structured
from cogdoc.config.settings import Settings, get_settings


QUERY_REWRITER_SYSTEM_PROMPT = (
    "你是一位 RAG 检索优化专家，负责将用户原始提问改写为更适合在向量数据库（Vector Search）和关键词引擎（BM25）中精确召回的检索语句。\n\n"
    "【任务定义】\n输出 1 至 3 条改写查询，并把问题规划为最多 3 个可独立判定证据是否充分的原子需求。\n\n"
    "【改写规则】\n1. 去除语气词和问句结构（如'请问'、'是什么'、'为什么'、'如何'），保留核心名词、动词和专有名词。\n"
    "2. 每条改写语句之间须有明显差异，不得重复或近义替换。\n3. 不得引入原问题中不存在的概念或实体。\n"
    "4. 若当前提问包含代词、'上面/这个/那点'等省略表达，先结合近期对话补全真实检索对象。\n"
    "5. 若原问题已是简洁的关键词形式，可将其作为第一条直接输出，再补充 1-2 条不同角度的改写。\n"
    "6. 每个证据需求只能询问一个可验证事实；并列的对象、条件或指标应拆成不同需求。\n"
    "7. question 保留完整语义；retrieval_query 用于主检索；recovery_query 用不同表达做补充检索。\n"
    "8. 证据需求和查询都不得引入当前提问与近期对话中没有的实体。不要输出 requirement_id，系统会分配。\n\n"
    "【输出格式】\n只输出 JSON 对象，不要 Markdown，不要解释。\n"
    "【示例】\n原问题：大模型在医疗影像诊断中是怎么应用的？\n改写输出：\n"
    '{"queries":["大模型 医疗影像诊断 应用方法",'
    '"医疗影像诊断 大模型 使用方式","大模型 影像诊断 实施方法"],'
    '"evidence_requirements":[{"question":"大模型如何用于医疗影像诊断？",'
    '"retrieval_query":"大模型 医疗影像诊断 应用方法",'
    '"recovery_query":"医疗影像诊断 大模型 使用方式"}]}'
)
QUERY_REWRITER_USER_PROMPT_TEMPLATE = (
    "【近期对话】\n{history_text}\n\n【当前提问】\n{query}\n\n"
    "请结合必要的近期对话执行检索改写。"
)


# 模型只负责起草，requirement_id 由服务端确定性分配。
class EvidenceRequirementDraft(BaseModel):
    question: str = Field(description="可独立判断证据是否充分的原子问题")
    retrieval_query: str = Field(description="主检索查询")
    recovery_query: str = Field(description="召回不足时的补充检索查询")


# 改写结果限定在少量高价值检索查询和原子证据需求内。
class QueryRewriteOutput(BaseModel):
    queries: List[str] = Field(
        min_length=1,
        max_length=3,
        description="针对用户原始问题，裂变、改写出的 1-3 个最适合在本地知识库中检索的差异化查询语句。",
    )
    evidence_requirements: list[EvidenceRequirementDraft] = Field(
        default_factory=list,
        max_length=3,
        description="最多 3 个不含服务端标识的原子证据需求草案",
    )


def _fallback_requirement(query: str) -> dict[str, str]:
    return {
        "requirement_id": "r1",
        "question": query,
        "retrieval_query": query,
        "recovery_query": query,
    }


_CONTEXT_REFERENCE_PATTERN = re.compile(
    r"(?:这个|那个|上述|上面|前面|其中|它(?:的)?|其(?:中|的)?|前者|后者|"
    r"\b(?:it|its|this|that|these|those|former|latter)\b)",
    re.IGNORECASE,
)
_COMPLEX_QUERY_PATTERN = re.compile(
    r"(?:比较|对比|区别|异同|分别|各自|逐一|优缺点|利弊|哪个更|孰优孰劣)"
)
_QUERY_TERM_PATTERN = re.compile(
    r"(?:什么|多少|如何|为什么|是否|能否|何时|什么时候|哪里|哪种|哪个|谁)"
)
_CONJUNCTION_PATTERN = re.compile(r"(?:和|与|以及|并且|同时)")
_PARALLEL_POSSESSIVE_PATTERN = re.compile(
    r"(?:的[^，。；!?！？]{0,24}(?:和|与|以及)[^，。；!?！？]{0,24}的)"
)


def should_use_query_rewrite_fast_path(
    query: str,
    *,
    history_text: str = "",
    settings: Settings | None = None,
) -> bool:
    """Conservatively identify self-contained single-intent retrieval queries."""

    settings = settings or get_settings()
    normalized = " ".join(str(query or "").split())
    if not settings.qa_query_rewrite_fast_path_enabled or not normalized:
        return False
    if len(normalized) > 80 or len(re.findall(r"[?？]", normalized)) > 1:
        return False
    if _COMPLEX_QUERY_PATTERN.search(normalized):
        return False
    # A conjunction alone does not prove multiple intents (for example,
    # “如何安装和启动服务” is one operation). Two independent question terms
    # around a conjunction are a stronger signal that atomic planning is needed.
    if _CONJUNCTION_PATTERN.search(normalized) and len(
        _QUERY_TERM_PATTERN.findall(normalized)
    ) > 1:
        return False
    # “A 的日期和 B 的费用” has only one surface question phrase but names two
    # independently verifiable possessive facts.
    if _PARALLEL_POSSESSIVE_PATTERN.search(normalized):
        return False
    if history_text and _CONTEXT_REFERENCE_PATTERN.search(normalized):
        return False
    return True


def _normalize_requirements(
    drafts: list[EvidenceRequirementDraft], query: str
) -> list[dict[str, str]]:
    requirements: list[dict[str, str]] = []
    seen_questions: set[str] = set()
    for draft in drafts[:3]:
        question = " ".join(draft.question.split())
        if not question:
            continue
        question_key = unicodedata.normalize("NFKC", question).casefold()
        if question_key in seen_questions:
            continue
        seen_questions.add(question_key)
        retrieval_query = " ".join(draft.retrieval_query.split()) or question
        recovery_query = " ".join(draft.recovery_query.split()) or question
        requirements.append(
            {
                "requirement_id": f"r{len(requirements) + 1}",
                "question": question,
                "retrieval_query": retrieval_query,
                "recovery_query": recovery_query,
            }
        )
    return requirements or [_fallback_requirement(query)]


# 定义 QueryRewriteAgent 数据结构。
class QueryRewriteAgent:
    # 完成 重写问题问题 处理。
    @staticmethod
    def rewrite_query(state: Mapping[str, Any]) -> dict[str, Any]:
        # 改写失败时回退到原始 query，保证检索链路可继续。
        query = state.get("query", "")
        is_local = state.get("is_local", False)
        history_text = format_recent_chat_history(
            state.get("chat_history"), limit=CHAT_HISTORY_MESSAGE_LIMIT
        )

        if not query:
            return {"rewritten_queries": [], "evidence_requirements": []}
        if should_use_query_rewrite_fast_path(query, history_text=history_text):
            return {
                "rewritten_queries": [query],
                "evidence_requirements": [_fallback_requirement(query)],
                "query_rewrite_fast_path": True,
            }

        try:
            llm = Generator._get_client_for_node("query_rewriter", is_local=is_local)
            output = invoke_structured(
                llm,
                QueryRewriteOutput,
                [
                    {"role": "system", "content": QUERY_REWRITER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": QUERY_REWRITER_USER_PROMPT_TEMPLATE.format(
                            history_text=history_text or "（无）", query=query
                        ),
                    },
                ],
            )
            return {
                "rewritten_queries": output.queries,
                "evidence_requirements": _normalize_requirements(
                    output.evidence_requirements, query
                ),
            }
        except Exception:
            return {
                "rewritten_queries": [query],
                "evidence_requirements": [_fallback_requirement(query)],
            }
