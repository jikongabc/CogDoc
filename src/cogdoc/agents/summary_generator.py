import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, List, Tuple, cast
from langchain_core.messages import HumanMessage, SystemMessage
from cogdoc.config.settings import get_settings
from cogdoc.agents.answer_markers import CITATION_WARNING_HEADING
from cogdoc.agents.claim_evidence_verifier import (
    CLAIM_AUDIT_EXEMPTION_GUIDANCE,
    CLAIM_AUDIT_EXEMPTION_UPSTREAM_ERROR,
    make_claim_audit_exemption,
)
from cogdoc.agents.no_evidence import NO_EVIDENCE_MARKER, is_no_evidence_statement
from cogdoc.agents.qa_generator import Generator
from cogdoc.graph.state import (
    Evidence,
    RetrievedDoc,
    SummarySectionPlan,
    SummarySectionResult,
)
from cogdoc.service.claim_audit_projection import (
    ClaimAuditProjectionSegment,
    build_claim_audit_projection,
)
from cogdoc.tools.evidence_rendering import render_evidence_context
from cogdoc.tools.citation_ledger import (
    evidence_ids_for_docs,
    ensure_evidence_ids,
    validate_evidence_citations,
)
from cogdoc.tools.tokenizer import tokenize_mixed_text


SUMMARY_SECTION_SYSTEM_PROMPT = (
    "你是一位严谨的技术文档摘要助手。你的唯一工作是：仅依据给定的 <Document> 标签文本，为指定章节写一段简短的中文摘要。\n\n"
    "【硬性约束】\n1. 只能使用 <Document> 标签内的信息，禁止引入任何标签外的知识、常识或推测。\n"
    "2. 不要输出引用标签、页码、文件名或 <Document> 标签，程序会在生成后自动绑定引用。\n"
    "3. 不要使用占位词，不要输出章节标题，不要解释规则。\n\n【范围与篇幅】\n"
    "- 章节名是内容归类维度，不代表文档必须是论文；通知、规程、赛事规则也要按最接近维度归纳。\n"
    "- 只写当前指定章节的内容，不复述其它章节、不重复章节标题、不加前言/结语/过渡语。\n- 输出 2-4 句中文短句。\n\n"
    "【无依据时】\n- 只有当所有给定片段都没有任何可归入本章节的信息时，才输出一行：文档中未明确说明（不加引用、不编造）。\n"
    "- 若片段中有目标、对象、赛制、模块、要求、评分、奖项、日程、注意事项等赛事/通知信息，必须按章节聚焦摘要，不要因为不是论文实验而输出“文档中未明确说明”。\n\n"
    "【输出】只输出摘要正文（或“文档中未明确说明”），不要输出章节标题、不要解释、不要任何额外文字。"
)

SUMMARY_RETRY_SYSTEM_PROMPT = (
    "你是一位严谨的中文资料整理助手。下面片段已经由程序筛选为与指定摘要维度相关。你的任务是从片段中提炼事实，不要因为文档不是论文而回答“文档中未明确说明”。\n\n"
    "【要求】\n1. 只能依据 <Document> 标签内文字。\n"
    "2. 若能找到任何与该维度相关的目标、对象、流程、规则、要求、评分、奖项、日程、注意事项或价值，就写 2-3 句中文摘要。\n"
    "3. 不要输出章节标题、引用、页码或解释。\n4. 只有片段完全没有相关事实时，才输出：文档中未明确说明。"
)
SUMMARY_SECTION_USER_PROMPT_TEMPLATE = (
    "【目标文档】{source}\n【本次章节】{title}\n【章节聚焦】{instruction}\n"
    "【用户摘要意图】{query}\n\n下面是从该文档中筛选出的、与本章节最相关的片段：\n"
    "【参考资料开始】\n{context}\n【参考资料结束】\n\n"
    "请据此写出本章节摘要，2-4 句，严格遵守上面的全部约束。"
)
SUMMARY_RETRY_USER_PROMPT_TEMPLATE = (
    "【目标文档】{source}\n【摘要维度】{title}\n【维度说明】{instruction}\n"
    "【用户意图】{query}\n\n【参考资料开始】\n{context}\n【参考资料结束】\n\n"
    "请重新提炼该维度摘要。"
)

EVIDENCE_UNIT_FAILURE_MESSAGE = "本单元证据处理未完成，请重试。"


MAX_SECTION_CONTEXT_CHUNKS = 8
LOCAL_LARGE_SECTION_CONTEXT_CHUNKS = 6
CITATION_PATTERN = re.compile(
    r"[\[［]\s*([^:：\]］]+?)\s*[:：]\s*[pPｐＰ]?\s*([0-9０-９]+)\s*[\]］]"
)
EVIDENCE_CITATION_PATTERN = re.compile(r"\[E[0-9]{3,}\]")
_SEMANTIC_TEXT_PATTERN = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
_SENTENCE_TERMINATORS = frozenset("。！？!?;；")
_MARKDOWN_CLOSERS = ("***", "___", "**", "__", "~~", "*", "_")
_MARKDOWN_FENCE_PATTERN = re.compile(r"^\s*(?:`{3,}|~{3,})")
_LINE_ENDING_PATTERN = re.compile(r"(?:\r\n|\r|\n)$")

# 云端逐单元 LLM 调用相互独立，并发执行降低 Summary/Compare 端到端延迟；本地走串行。
CLOUD_SECTION_MAX_WORKERS = get_settings().cloud_section_max_workers


def _summary_claim_audit_projection(
    answer: str,
    results: List[SummarySectionResult],
) -> dict[str, Any]:
    """Project rendered sections by generation origin, independent of task logic."""

    segments: list[ClaimAuditProjectionSegment] = []
    for index, result in enumerate(results):
        content = str(result.get("content") or EVIDENCE_UNIT_FAILURE_MESSAGE).strip()
        raw_status = result.get("status")
        source_status = str(raw_status or "legacy_generated")
        unit_id = str(result.get("unit_id") or "").strip()
        segment_id = f"summary:section:{result.get('section_id', index)}"
        obligation_ids = (unit_id,) if unit_id else ()
        if raw_status == "no_evidence" or is_no_evidence_statement(content):
            segment = ClaimAuditProjectionSegment.deterministic(
                segment_id,
                content,
                source_status=source_status,
                obligation_ids=obligation_ids,
            )
        elif raw_status in (None, "", "generated") and (
            content != EVIDENCE_UNIT_FAILURE_MESSAGE
        ):
            segment = ClaimAuditProjectionSegment.generated(
                segment_id,
                content,
                source_status=source_status,
                obligation_ids=obligation_ids,
            )
        else:
            segment = ClaimAuditProjectionSegment.operational(
                segment_id,
                content,
                source_status=source_status,
                obligation_ids=obligation_ids,
            )
        segments.append(segment)
    return build_claim_audit_projection(answer, segments).to_state()


# 解析 section workers。
def resolve_section_workers(is_local: bool, task_count: int) -> int:
    # 本地 Ollama 并发会放大显存/内存压力，退回串行；单任务无需起线程池。
    if task_count <= 1 or is_local:
        return 1
    return min(task_count, CLOUD_SECTION_MAX_WORKERS)


# 运行 section cells。
def run_section_cells(tasks: List, worker: Callable, is_local: bool) -> List:
    # ThreadPoolExecutor.map 按输入顺序返回结果，与完成先后无关，保证列序/章节序确定。
    workers = resolve_section_workers(is_local, len(tasks))
    if workers == 1:
        return [worker(task) for task in tasks]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(worker, tasks))


# 分词 for section。
def tokenize_for_section(text: str) -> set[str]:
    # 把章节选择文本转成去重 token 集合。
    return set(tokenize_mixed_text(text))


# 选择章节文档列表。
def select_section_docs(
    docs: List[RetrievedDoc],
    plan: SummarySectionPlan,
    query: str,
    doc_tokens: List[Tuple[RetrievedDoc, set[str]]] | None = None,
    max_chunks: int = MAX_SECTION_CONTEXT_CHUNKS,
) -> List[RetrievedDoc]:
    # 按维度召回相关 chunk，并保持原文顺序。
    if len(docs) <= max_chunks:
        return docs

    section_text = f"{query} {plan['title']} {plan['instruction']}"
    section_tokens = tokenize_for_section(section_text)
    doc_tokens = doc_tokens or [
        (doc, tokenize_for_section(doc["text"])) for doc in docs
    ]

    scored = []
    for idx, (doc, tokens) in enumerate(doc_tokens):
        score = len(section_tokens & tokens)
        scored.append((score, idx, doc))

    selected = [item for item in scored if item[0] > 0]
    if not selected:
        selected = scored[:max_chunks]
    else:
        selected = sorted(selected, key=lambda item: (-item[0], item[1]))[:max_chunks]

    return [doc for _, _, doc in sorted(selected, key=lambda item: item[1])]


# 格式化 summary context。
def format_summary_context(docs: List[RetrievedDoc]) -> str:
    # Summary、Compare、QA 与 claim verifier 共用同一模型可见证据口径。
    return render_evidence_context(docs)


# 构建 section citations。
def build_section_citations(docs: List[RetrievedDoc]) -> str:
    # 引用由任务级账本中已经冻结的精确证据身份确定性生成。
    return "".join(f"[{evidence_id}]" for evidence_id in evidence_ids_for_docs(docs))


# 判断 no evidence summary 是否成立。
def is_no_evidence_summary(content: str) -> bool:
    # 共享的严格判定不允许逗号、冒号、补充从句或任何引用标签混入跳过路径。
    return is_no_evidence_statement(content)


def _inside_markdown_protected_span(content: str, index: int) -> bool:
    line_start = content.rfind("\n", 0, index) + 1
    prefix = content[line_start:index]
    suffix = content[index:]

    # Inline code、链接文本和链接目标中的标点不是句子边界，不能在其中插入 EID。
    if prefix.count("`") % 2:
        return True
    if prefix.rfind("](") > prefix.rfind(")"):
        return True
    if prefix.rfind("[") > prefix.rfind("]") and re.search(r"\]\([^\n]*", suffix):
        return True
    token = re.split(r"\s", prefix)[-1]
    if re.search(r"(?:https?://|www\.)\S*$", token, re.I):
        return True
    if prefix.rfind("<") > prefix.rfind(">"):
        return True
    return False


def _is_sentence_terminator(content: str, index: int) -> bool:
    if _inside_markdown_protected_span(content, index):
        return False
    char = content[index]
    if char in _SENTENCE_TERMINATORS:
        return True
    if char != ".":
        return False
    previous = content[index - 1] if index else ""
    following = content[index + 1] if index + 1 < len(content) else ""
    if previous.isdigit() and following.isdigit():
        return False

    # ASCII 句号只在句末语境中分句，避免拆开文件名、版本号和小数。
    cursor = index + 1
    while cursor < len(content):
        closer = next(
            (token for token in _MARKDOWN_CLOSERS if content.startswith(token, cursor)),
            None,
        )
        if closer is None:
            break
        cursor += len(closer)
    return cursor >= len(content) or content[cursor].isspace()


def _citation_insertion_index(sentence: str) -> int:
    end = len(sentence)
    while end and sentence[end - 1].isspace():
        end -= 1

    insertion = end
    if insertion and (
        sentence[insertion - 1] in _SENTENCE_TERMINATORS
        or sentence[insertion - 1] == "."
    ):
        insertion -= 1

    # `**事实**。` 应变成 `**事实[E001]**。`；不把引用落到 Markdown
    # 闭合标记外面，也不破坏原有标记和空白。
    while insertion:
        closer = next(
            (
                token
                for token in _MARKDOWN_CLOSERS
                if sentence[:insertion].endswith(token)
            ),
            None,
        )
        if closer is None:
            break
        insertion -= len(closer)
    return insertion


def _bind_sentence_citations(sentence: str, citations: str) -> str:
    # 判定无依据必须针对原句；带 EID 或其他附加内容的句子不得
    # 因删除标签后变成固定句而进入 no-evidence 跳过路径。
    no_evidence = is_no_evidence_summary(sentence)
    normalized = EVIDENCE_CITATION_PATTERN.sub("", CITATION_PATTERN.sub("", sentence))
    if no_evidence or not _SEMANTIC_TEXT_PATTERN.search(normalized):
        return normalized

    insertion = _citation_insertion_index(normalized)
    return f"{normalized[:insertion]}{citations}{normalized[insertion:]}"


def _bind_line_citations(line: str, citations: str) -> str:
    pieces: list[str] = []
    start = 0
    for index in range(len(line)):
        if _is_sentence_terminator(line, index):
            pieces.append(_bind_sentence_citations(line[start : index + 1], citations))
            start = index + 1
    pieces.append(_bind_sentence_citations(line[start:], citations))
    return "".join(pieces)


# 绑定章节引用列表。
def attach_section_citations(content: str, docs: List[RetrievedDoc]) -> str:
    # 模型只写正文，程序给每个事实句绑定该单元实际使用的精确证据集合。
    if not content or not content.strip() or not docs:
        return content

    citations = build_section_citations(docs)
    if not citations:
        return content

    pieces: list[str] = []
    in_fence = False
    for line in content.splitlines(keepends=True):
        ending_match = _LINE_ENDING_PATTERN.search(line)
        ending = ending_match.group(0) if ending_match else ""
        body = line[: -len(ending)] if ending else line
        is_fence = bool(_MARKDOWN_FENCE_PATTERN.match(body))
        if in_fence or is_fence:
            pieces.append(line)
            if is_fence:
                in_fence = not in_fence
            continue
        pieces.append(f"{_bind_line_citations(body, citations)}{ending}")
    return "".join(pieces)


# 完成 allcontentsno证据 处理。
def all_contents_no_evidence(contents: Iterable[str]) -> bool:
    # 仅全为显式无依据声明时才允许跳过缺引用校验。
    contents = list(contents)
    return bool(contents) and all(
        is_no_evidence_summary(content) for content in contents
    )


# 构建 summary evidence。
def build_summary_evidence(docs: List[RetrievedDoc]) -> List[Evidence]:
    # 将参与生成的 chunk 转成前端展示 evidence。
    return [
        Evidence(
            evidence_id=doc.get("retrieval", {}).get("evidence_id", ""),
            chunk_id=doc.get("meta", {}).get("chunk_id", ""),
            chunk_index=doc.get("meta", {}).get("chunk_index", -1),
            source=doc.get("meta", {}).get("source", ""),
            source_id=doc.get("meta", {}).get("source_id", ""),
            source_version_id=doc.get("meta", {}).get("source_version_id", ""),
            media_type=doc.get("meta", {}).get("media_type", ""),
            location=dict(doc.get("meta", {}).get("source_location") or {}),
            page=doc.get("meta", {}).get("page", 0),
            page_start=doc.get("meta", {}).get(
                "page_start", doc.get("meta", {}).get("page", 0)
            ),
            page_end=doc.get("meta", {}).get(
                "page_end", doc.get("meta", {}).get("page", 0)
            ),
            text_preview=doc["text"][:100],
        )
        for doc in docs
    ]


# 构建 cell evidence。
def build_cell_evidence(content: str, docs: List[RetrievedDoc]) -> List[Evidence]:
    # 明确无依据的 cell/section 不展示支撑 chunk，避免 evidence 面板误导审计。
    if is_no_evidence_summary(content):
        return []
    return build_summary_evidence(docs)


# 完成 章节上下文limit 处理。
def section_context_limit(is_local: bool, doc_count: int) -> int:
    # 本地模型按文档长度分档，避免长文档 4k context 截断，同时不过度牺牲摘要覆盖面。
    if is_local and doc_count > 16:
        return LOCAL_LARGE_SECTION_CONTEXT_CHUNKS
    return MAX_SECTION_CONTEXT_CHUNKS


# 收集证据条目列表。
def collect_evidence_items(
    evidence_groups: Iterable[Iterable[Evidence]],
    fallback_docs: List[RetrievedDoc],
    fallback_when_empty: bool = True,
) -> List[Evidence]:
    # 旧结果缺 evidence 字段时才回退全文 docs。
    seen = set()
    evidence: List[Evidence] = []

    for group in evidence_groups:
        for item in group:
            key = (
                item.get("chunk_id", ""),
                item.get("source", ""),
                item.get("page_start", item.get("page", 0)),
                item.get("page_end", item.get("page", 0)),
                item.get("chunk_index", -1),
            )
            if key in seen:
                continue
            seen.add(key)
            evidence.append(item)

    if evidence:
        return evidence
    if fallback_when_empty:
        return build_summary_evidence(fallback_docs)
    return []


# 收集章节证据。
def collect_section_evidence(
    results: List[SummarySectionResult],
    fallback_docs: List[RetrievedDoc],
) -> List[Evidence]:
    # 汇总各章节 evidence，必要时回退到全文 chunk。
    return collect_evidence_items(
        (result.get("evidence", []) for result in results),
        fallback_docs,
        fallback_when_empty=not any("evidence" in result for result in results),
    )


# 追加 citation warning。
def append_citation_warning(answer: str, critique: str, unit_label: str) -> str:
    # 原始 critique 可能包含 `[E001]` 示例或伪造 ID；它仅保存在状态
    # 字段，不能混入待 finalizer 解析的答案正文。
    _ = critique
    return (
        f"{answer}\n\n"
        f"{CITATION_WARNING_HEADING}\n"
        f"部分{unit_label}的证据引用未通过校验，请重新生成或查看审计详情。"
    )


# 完成 摘要消息列表 处理。
def _summary_messages(source: str, plan: SummarySectionPlan, query: str, context: str):
    # 构造首次章节摘要调用的消息。
    return [
        SystemMessage(content=SUMMARY_SECTION_SYSTEM_PROMPT),
        HumanMessage(
            content=SUMMARY_SECTION_USER_PROMPT_TEMPLATE.format(
                source=source,
                title=plan["title"],
                instruction=plan["instruction"],
                query=query,
                context=context,
            )
        ),
    ]


# 完成 摘要retry消息列表 处理。
def _summary_retry_messages(
    source: str, plan: SummarySectionPlan, query: str, context: str
):
    # 构造本地模型无依据误判后的重试消息。
    return [
        SystemMessage(content=SUMMARY_RETRY_SYSTEM_PROMPT),
        HumanMessage(
            content=SUMMARY_RETRY_USER_PROMPT_TEMPLATE.format(
                source=source,
                title=plan["title"],
                instruction=plan["instruction"],
                query=query,
                context=context,
            )
        ),
    ]


# 生成 section cell。
def generate_section_cell(
    llm,
    source: str,
    plan: SummarySectionPlan,
    docs: List[RetrievedDoc],
    query: str,
    is_local: bool,
    doc_tokens: List[Tuple[RetrievedDoc, set[str]]],
    build_messages: Callable[[str, SummarySectionPlan, str, str], list],
    build_retry_messages: (
        Callable[[str, SummarySectionPlan, str, str], list] | None
    ) = None,
    max_chunks: int | None = None,
) -> Tuple[str, List[Evidence]]:
    # Summary 和 Compare 共用单元生成、引用绑定与 evidence 契约。
    max_chunks = max_chunks or section_context_limit(is_local, len(docs))
    section_docs = select_section_docs(
        docs, plan, query, doc_tokens=doc_tokens, max_chunks=max_chunks
    )
    context = format_summary_context(section_docs)
    content = llm.invoke(build_messages(source, plan, query, context)).content

    if (
        is_local
        and section_docs
        and is_no_evidence_summary(content)
        and build_retry_messages is not None
    ):
        content = llm.invoke(build_retry_messages(source, plan, query, context)).content

    content = attach_section_citations(content, section_docs)
    return content, build_cell_evidence(content, section_docs)


# 负责按章节生成单文档摘要。
class SectionSummaryAgent:
    # 负责按章节生成单文档摘要。
    @staticmethod
    def summarize_sections(state: dict) -> dict:
        # 生成所有章节摘要并保持章节顺序。
        docs: List[RetrievedDoc] = state.get("summary_docs", [])
        plans: List[SummarySectionPlan] = state.get("summary_section_plans", [])
        query = state.get("query", "")
        source = state.get("summary_source", "")
        is_local = state.get("is_local", False)

        raw_unit_results = state.get("evidence_unit_results")
        unit_results = raw_unit_results if isinstance(raw_unit_results, list) else None
        if not plans or (not docs and unit_results is None):
            return {"summary_section_results": []}

        if docs:
            docs, evidence_ledger = ensure_evidence_ids(docs)
        else:
            evidence_ledger = list(state.get("evidence_ledger") or [])

        generation_ready_statuses = {"ready", "supported", "contradictory"}
        batch_can_generate = bool(state.get("evidence_unit_batch_can_generate", True))
        needs_generation = unit_results is None or (
            batch_can_generate
            and any(
                isinstance(item, dict)
                and item.get("status") in generation_ready_statuses
                and item.get("gate_action", "generate") == "generate"
                for item in unit_results
            )
        )
        llm = None
        client_error = ""
        if needs_generation:
            try:
                llm = Generator._get_client_for_node(
                    "summary_generator", is_local=is_local
                )
            except Exception as exc:
                client_error = type(exc).__name__

        # 构建 section result。
        def build_section_result(plan: SummarySectionPlan) -> SummarySectionResult:
            # 生成单个章节结果。
            unit_result = next(
                (
                    item
                    for item in unit_results or []
                    if isinstance(item, dict)
                    and isinstance(item.get("binding"), dict)
                    and item["binding"].get("section_id") == plan["section_id"]
                ),
                None,
            )
            status = str(unit_result.get("status") or "") if unit_result else ""
            unit_id = str(unit_result.get("unit_id") or "") if unit_result else ""
            gate_action = (
                str(unit_result.get("gate_action") or "") if unit_result else ""
            )
            if status == "no_evidence":
                return SummarySectionResult(
                    section_id=plan["section_id"],
                    title=plan["title"],
                    content=f"{NO_EVIDENCE_MARKER}。",
                    unit_id=unit_id,
                    evidence=[],
                    status=status,
                )
            if unit_result is not None and (
                status not in generation_ready_statuses
                or gate_action not in {"", "generate"}
                or not batch_can_generate
            ):
                return SummarySectionResult(
                    section_id=plan["section_id"],
                    title=plan["title"],
                    content=EVIDENCE_UNIT_FAILURE_MESSAGE,
                    unit_id=unit_id,
                    evidence=[],
                    status=status or "generation_error",
                    failure_stage=(
                        "verification"
                        if status == "verification_error"
                        or gate_action not in {"", "generate"}
                        else "retrieval"
                    ),
                    error_class=str(unit_result.get("error_class") or ""),
                )
            if llm is None:
                return SummarySectionResult(
                    section_id=plan["section_id"],
                    title=plan["title"],
                    content=EVIDENCE_UNIT_FAILURE_MESSAGE,
                    unit_id=unit_id,
                    evidence=[],
                    status="generation_error",
                    failure_stage="generation",
                    error_class=client_error or "GeneratorUnavailable",
                )

            section_docs = (
                list(unit_result.get("selected_docs") or [])
                if unit_result is not None
                else docs
            )
            section_tokens = [
                (doc, tokenize_for_section(doc["text"])) for doc in section_docs
            ]
            try:
                content, evidence = generate_section_cell(
                    llm,
                    source,
                    plan,
                    section_docs,
                    query,
                    is_local,
                    section_tokens,
                    _summary_messages,
                    _summary_retry_messages,
                )
            except Exception as exc:
                return SummarySectionResult(
                    section_id=plan["section_id"],
                    title=plan["title"],
                    content=EVIDENCE_UNIT_FAILURE_MESSAGE,
                    unit_id=unit_id,
                    evidence=[],
                    status="generation_error",
                    failure_stage="generation",
                    error_class=type(exc).__name__,
                )
            return SummarySectionResult(
                section_id=plan["section_id"],
                title=plan["title"],
                content=content,
                unit_id=unit_id,
                evidence=evidence,
                status="generated",
            )

        results = run_section_cells(plans, build_section_result, is_local)
        return {
            "summary_docs": docs,
            "evidence_ledger": evidence_ledger,
            "summary_section_results": results,
        }


# 负责合并章节摘要并执行最终引用校验。
class GlobalSummaryAgent:
    # 负责合并章节摘要并执行最终引用校验。
    @staticmethod
    def build_final_summary(state: dict) -> dict:
        # 组装最终摘要答案和审计字段。
        source = state.get("summary_source", "")
        docs: List[RetrievedDoc] = state.get("summary_docs", [])
        results: List[SummarySectionResult] = state.get("summary_section_results", [])

        if not results:
            answer = f"未能生成文档 {source} 的摘要章节。"
            return {
                "answer": answer,
                "messages": [{"role": "assistant", "content": answer}],
                "claim_audit_exemption": make_claim_audit_exemption(
                    answer,
                    CLAIM_AUDIT_EXEMPTION_GUIDANCE,
                ),
            }

        render_results: List[SummarySectionResult] = []
        for result in results:
            normalized = cast(SummarySectionResult, dict(result))
            content = str(result.get("content") or "").strip()
            if not content:
                normalized["content"] = EVIDENCE_UNIT_FAILURE_MESSAGE
                normalized["status"] = "generation_error"
            render_results.append(normalized)

        lines = [f"# {source} 结构化摘要"]
        for result in render_results:
            lines.append(f"\n## {result['title']}\n{result['content']}")
        answer = "\n".join(lines)

        # 只有模型生成的事实章节需要引用；确定性 no-evidence/错误行不需要。
        critique = ""
        section_contents = [result.get("content", "") for result in render_results]
        evidence_ledger = state.get("evidence_ledger")
        generated_sections = any(
            result.get("status") in {None, "", "generated"} for result in render_results
        )
        if generated_sections and not all_contents_no_evidence(section_contents):
            if evidence_ledger is None:
                docs, evidence_ledger = ensure_evidence_ids(docs)
            check_res = validate_evidence_citations(
                answer,
                evidence_ledger,
            )
            if not check_res["is_valid"]:
                critique = check_res["critique"]
                answer = append_citation_warning(answer, critique, "章节")

        output = {
            "answer": answer,
            "messages": [{"role": "assistant", "content": answer}],
            "sources": [doc["meta"] for doc in docs],
            "summary_docs": docs,
            "evidence_ledger": evidence_ledger or [],
            "evidence": collect_section_evidence(render_results, docs),
            "critique": critique,
            "claim_audit_projection": _summary_claim_audit_projection(
                answer,
                render_results,
            ),
        }
        if not generated_sections and any(
            result.get("status") not in {None, "", "no_evidence"}
            for result in render_results
        ):
            output["error"] = "summary_evidence_units_incomplete"
            output["claim_audit_exemption"] = make_claim_audit_exemption(
                answer,
                CLAIM_AUDIT_EXEMPTION_UPSTREAM_ERROR,
            )
        return output
