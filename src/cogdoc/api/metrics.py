import math
import time
from collections.abc import Mapping
from threading import Lock
from fastapi import Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from cogdoc.service.claim_verification_rollout import ROLLOUT_DECISIONS
from cogdoc.service.research_observability import (
    normalize_research_action,
    normalize_research_background_outcome,
    normalize_research_coverage_status,
    normalize_research_lifecycle_outcome,
    normalize_research_provider,
    normalize_research_provider_isolation,
    normalize_research_provider_outcome,
    normalize_research_stage,
    normalize_research_termination_reason,
)


_CLAIM_AUDIT_STATUSES = {
    "not_run",
    "passed",
    "failed",
    "repaired",
    "rejected",
    "error",
}
_CLAIM_VERIFICATION_MODES = {"off", "shadow", "enforce"}
_REQUIREMENT_VERDICTS = {"supported", "missing", "contradictory"}


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(number, 0.0)


def _mapping_or_empty(value) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


# 每个 app 独立 CollectorRegistry：避免多次 create_app 在全局注册表重复注册、测试间互相串数。
class Metrics:
    # 每个 app 独立 CollectorRegistry：避免多次 create_app 在全局注册表重复注册、测试间互相串数。
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "cogdoc_http_requests_total",
            "HTTP 请求总数",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "cogdoc_http_request_duration_seconds",
            "HTTP 请求耗时",
            ["method", "route"],
            registry=self.registry,
        )
        self.in_progress = Gauge(
            "cogdoc_http_requests_in_progress",
            "在途 HTTP 请求数",
            registry=self.registry,
        )
        self.chat_results = Counter(
            "cogdoc_chat_results_total",
            "对话产出按任务类型与是否可信计数",
            ["task_type", "valid"],
            registry=self.registry,
        )
        self.claim_audit_runs = Counter(
            "cogdoc_claim_audit_runs_total",
            "生成后声明审计按任务与结果计数",
            ["task_type", "status"],
            registry=self.registry,
        )
        self.claim_audit_claims = Counter(
            "cogdoc_claim_audit_claims_total",
            "声明审计按判定结果计数",
            ["task_type", "verdict"],
            registry=self.registry,
        )
        self.claim_audit_citations = Counter(
            "cogdoc_claim_audit_citations_total",
            "事实声明按是否绑定有效引用计数",
            ["task_type", "covered"],
            registry=self.registry,
        )
        self.claim_audit_repairs = Counter(
            "cogdoc_claim_audit_repairs_total",
            "发生声明修复的请求按最终结果计数",
            ["task_type", "outcome"],
            registry=self.registry,
        )
        self.claim_audit_duration = Histogram(
            "cogdoc_claim_audit_duration_seconds",
            "最终一轮声明审计模型调用耗时",
            ["task_type"],
            registry=self.registry,
        )
        self.claim_verification_rollouts = Counter(
            "cogdoc_claim_verification_rollouts_total",
            "声明语义核验按灰度模式与最终投影决策计数",
            ["task_type", "mode", "decision"],
            registry=self.registry,
        )
        self.claim_verification_cohorts = Counter(
            "cogdoc_claim_verification_cohorts_total",
            "声明语义核验按配置模式、实际模式与分桶命中结果计数",
            ["task_type", "configured_mode", "effective_mode", "selected"],
            registry=self.registry,
        )
        self.retrieval_decisions = Counter(
            "cogdoc_retrieval_decisions_total",
            "QA 检索最终按放行或拒答计数",
            ["outcome"],
            registry=self.registry,
        )
        self.adaptive_retrieval_runs = Counter(
            "cogdoc_adaptive_retrieval_runs_total",
            "发生补检索的 QA 请求按最终结果计数",
            ["outcome"],
            registry=self.registry,
        )
        self.adaptive_retrieval_retries = Histogram(
            "cogdoc_adaptive_retrieval_retry_count",
            "单次 QA 请求执行的补检索次数",
            registry=self.registry,
        )
        self.evidence_requirement_assessments = Counter(
            "cogdoc_evidence_requirement_assessments_total",
            "生成前证据需求按校验结论计数",
            ["verdict"],
            registry=self.registry,
        )
        self.evidence_verifier_errors = Counter(
            "cogdoc_evidence_verifier_errors_total",
            "生成前证据校验器异常计数",
            registry=self.registry,
        )
        # Connector labels are deliberately restricted to the finite built-in
        # connector type/outcome sets. Connection, KB, tenant and job IDs stay
        # in logs/webhooks so Prometheus cardinality remains bounded.
        self.connector_sync_events = Counter(
            "cogdoc_connector_sync_events_total",
            "来源同步生命周期事件数",
            ["connector_type", "outcome"],
            registry=self.registry,
        )
        self.connector_sync_duration = Histogram(
            "cogdoc_connector_sync_duration_seconds",
            "来源同步终态耗时",
            ["connector_type", "outcome"],
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
            registry=self.registry,
        )
        self.connector_sync_backlog = Histogram(
            "cogdoc_connector_sync_backlog",
            "来源同步事件发生时的连接级积压采样",
            ["connector_type"],
            buckets=(0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 100, 250, 500),
            registry=self.registry,
        )
        self.connector_sync_documents = Counter(
            "cogdoc_connector_sync_documents_total",
            "终态来源同步抓取文档数",
            ["connector_type", "outcome"],
            registry=self.registry,
        )
        # Research 指标只使用闭集标签。job/kb/execution/section 等关联标识仅进日志与
        # trace，绝不进入 Prometheus label，避免长任务持续制造高基数时间序列。
        self.research_lifecycle = Counter(
            "cogdoc_research_lifecycle_total",
            "研究任务生命周期操作按动作与结果计数",
            ["action", "outcome"],
            registry=self.registry,
        )
        self.research_background = Counter(
            "cogdoc_research_background_total",
            "研究后台阶段按最终结果计数",
            ["stage", "outcome"],
            registry=self.registry,
        )
        self.research_background_duration = Histogram(
            "cogdoc_research_background_duration_seconds",
            "研究后台阶段执行耗时",
            ["stage"],
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
            registry=self.registry,
        )
        self.research_background_in_progress = Gauge(
            "cogdoc_research_background_in_progress",
            "研究后台阶段当前在途 worker 数",
            ["stage"],
            registry=self.registry,
        )
        self.research_terminations = Counter(
            "cogdoc_research_terminations_total",
            "研究任务因预算或控制信号终止的次数",
            ["reason"],
            registry=self.registry,
        )
        self.research_section_candidates = Histogram(
            "cogdoc_research_section_candidate_count",
            "单个研究章节检索到的去重候选数",
            buckets=(0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 100, 250),
            registry=self.registry,
        )
        self.research_section_evidence = Histogram(
            "cogdoc_research_section_evidence_count",
            "单个研究章节提交的公开证据数",
            buckets=(0, 1, 2, 3, 5, 8, 12, 20, 50),
            registry=self.registry,
        )
        self.research_coverage_audits = Counter(
            "cogdoc_research_coverage_audits_total",
            "研究章节原子需求覆盖审计按结果计数",
            ["status"],
            registry=self.registry,
        )
        self.research_provider_calls = Counter(
            "cogdoc_research_provider_calls_total",
            "研究外部提供方调用按提供方、隔离方式与结果计数",
            ["provider", "isolation", "outcome"],
            registry=self.registry,
        )
        self.research_provider_call_duration = Histogram(
            "cogdoc_research_provider_call_duration_seconds",
            "研究外部提供方单次调用耗时",
            ["provider", "isolation", "outcome"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 180, 300),
            registry=self.registry,
        )
        self._research_in_progress_lock = Lock()
        self._research_in_progress_counts: dict[str, int] = {}

    def observe_claim_audit(self, task_type: str, audit) -> None:
        # 指标是旁路可观测性：即使注入 runner 返回畸形 audit，也不能反向打断回答交付。
        if not isinstance(audit, Mapping):
            return
        raw_status = str(audit.get("status") or "not_run")
        status = raw_status if raw_status in _CLAIM_AUDIT_STATUSES else "unknown"
        self.claim_audit_runs.labels(task_type, status).inc()
        counts = _mapping_or_empty(audit.get("counts"))
        for verdict in ("supported", "unsupported", "insufficient"):
            count = _nonnegative_int(counts.get(verdict, 0))
            if count > 0:
                self.claim_audit_claims.labels(task_type, verdict).inc(count)
        skipped = _nonnegative_int(counts.get("skipped_statements", 0))
        if skipped > 0:
            self.claim_audit_claims.labels(task_type, "not_factual").inc(skipped)
        claim_count = _nonnegative_int(counts.get("claim_count", 0))
        cited = min(claim_count, _nonnegative_int(counts.get("cited", 0)))
        if cited:
            self.claim_audit_citations.labels(task_type, "true").inc(cited)
        if claim_count > cited:
            self.claim_audit_citations.labels(task_type, "false").inc(
                claim_count - cited
            )
        repair = _mapping_or_empty(audit.get("repair"))
        if repair.get("attempted"):
            outcome = "succeeded" if repair.get("succeeded") else "failed"
            self.claim_audit_repairs.labels(task_type, outcome).inc()
        verifier = _mapping_or_empty(audit.get("verifier"))
        duration_ms = _nonnegative_float_or_none(verifier.get("duration_ms"))
        # disabled/not_run 没有 verifier 调用，不能用 0 秒样本稀释真实延迟分布。
        if status != "not_run" and duration_ms is not None:
            self.claim_audit_duration.labels(task_type).observe(duration_ms / 1000.0)

    def observe_claim_verification_rollout(self, task_type: str, rollout) -> None:
        if not isinstance(rollout, Mapping):
            return
        raw_mode = str(rollout.get("mode") or "unknown")
        mode = raw_mode if raw_mode in _CLAIM_VERIFICATION_MODES else "unknown"
        raw_configured_mode = str(rollout.get("configured_mode") or mode)
        configured_mode = (
            raw_configured_mode
            if raw_configured_mode in _CLAIM_VERIFICATION_MODES
            else "unknown"
        )
        raw_decision = str(rollout.get("decision") or "unknown")
        decision = (
            raw_decision
            if raw_decision in ROLLOUT_DECISIONS
            else "unknown"
        )
        try:
            self.claim_verification_rollouts.labels(
                str(task_type or "unknown"), mode, decision
            ).inc()
            self.claim_verification_cohorts.labels(
                str(task_type or "unknown"),
                configured_mode,
                mode,
                str(bool(rollout.get("cohort_selected", True))).lower(),
            ).inc()
        except Exception:
            return

    def observe_retrieval(self, task_type: str, output) -> None:
        # 只统计最终 QA 状态；畸形或其他任务输出不能污染检索质量序列。
        if task_type != "qa" or not isinstance(output, Mapping):
            return
        if not any(
            key in output
            for key in (
                "retrieval_abstained",
                "retrieval_retry_count",
                "evidence_requirement_assessments",
            )
        ):
            return
        terminal_abstained = output.get("retrieval_abstained")
        # 缺失或畸形字段不是可推断的 accepted；只用显式图终态记决策与重试结果。
        if isinstance(terminal_abstained, bool):
            self.retrieval_decisions.labels(
                "abstained" if terminal_abstained else "accepted"
            ).inc()
            retry_count = _nonnegative_int(output.get("retrieval_retry_count", 0))
            self.adaptive_retrieval_retries.observe(retry_count)
            if retry_count > 0:
                self.adaptive_retrieval_runs.labels(
                    "exhausted" if terminal_abstained else "rescued"
                ).inc()
        assessments = output.get("evidence_requirement_assessments")
        if isinstance(assessments, list):
            for item in assessments:
                if not isinstance(item, Mapping):
                    continue
                raw_verdict = str(item.get("verdict") or "unknown")
                verdict = (
                    raw_verdict if raw_verdict in _REQUIREMENT_VERDICTS else "unknown"
                )
                self.evidence_requirement_assessments.labels(verdict).inc()
        if output.get("evidence_verifier_error"):
            self.evidence_verifier_errors.inc()

    def observe_research_lifecycle(self, action, outcome) -> None:
        """Record one API/control-plane attempt with bounded labels."""

        normalized_action = normalize_research_action(action)
        normalized_outcome = normalize_research_lifecycle_outcome(outcome)
        try:
            self.research_lifecycle.labels(
                normalized_action,
                normalized_outcome,
            ).inc()
        except Exception:
            # Metrics are a side channel and must never change the research result.
            return

    def research_background_started(self, stage) -> None:
        """Increment the clamped in-progress gauge for a scheduled worker."""

        normalized_stage = normalize_research_stage(stage)
        try:
            with self._research_in_progress_lock:
                count = self._research_in_progress_counts.get(normalized_stage, 0) + 1
                self._research_in_progress_counts[normalized_stage] = count
                self.research_background_in_progress.labels(normalized_stage).set(count)
        except Exception:
            return

    def research_background_finished(
        self,
        stage,
        outcome,
        *,
        duration_ms=None,
    ) -> None:
        """Record a worker outcome/duration and balance its in-progress gauge."""

        normalized_stage = normalize_research_stage(stage)
        normalized_outcome = normalize_research_background_outcome(outcome)
        duration = _nonnegative_float_or_none(duration_ms)
        try:
            self.research_background.labels(
                normalized_stage,
                normalized_outcome,
            ).inc()
            if duration is not None:
                self.research_background_duration.labels(normalized_stage).observe(
                    duration / 1000.0
                )
        except Exception:
            pass
        try:
            with self._research_in_progress_lock:
                count = max(
                    0,
                    self._research_in_progress_counts.get(normalized_stage, 0) - 1,
                )
                self._research_in_progress_counts[normalized_stage] = count
                self.research_background_in_progress.labels(normalized_stage).set(count)
        except Exception:
            return

    def observe_research_termination(self, reason, *, count=1) -> None:
        """Record a bounded budget/control termination reason."""

        normalized_reason = normalize_research_termination_reason(reason)
        amount = min(_nonnegative_int(count), 1_000_000_000)
        if not amount:
            return
        try:
            self.research_terminations.labels(normalized_reason).inc(amount)
        except Exception:
            return

    def observe_research_section(
        self, *, candidate_count=None, evidence_count=None
    ) -> None:
        """Observe bounded section-size distributions without identifier labels."""

        candidates = _nonnegative_int(candidate_count)
        evidence = _nonnegative_int(evidence_count)
        try:
            self.research_section_candidates.observe(candidates)
            self.research_section_evidence.observe(evidence)
        except Exception:
            return

    def observe_research_coverage_audit(self, audit_or_status) -> None:
        """Record only the coverage status; model detail never becomes a label."""

        try:
            raw_status = (
                audit_or_status.get("status")
                if isinstance(audit_or_status, Mapping)
                else audit_or_status
            )
        except Exception:
            raw_status = None
        status = normalize_research_coverage_status(raw_status)
        try:
            self.research_coverage_audits.labels(status).inc()
        except Exception:
            return

    def observe_research_provider_call(
        self,
        provider,
        isolation,
        outcome,
        *,
        duration_ms=None,
    ) -> None:
        """Record one provider attempt using only closed, low-cardinality labels."""

        normalized_provider = normalize_research_provider(provider)
        normalized_isolation = normalize_research_provider_isolation(isolation)
        normalized_outcome = normalize_research_provider_outcome(outcome)
        duration = _nonnegative_float_or_none(duration_ms)
        try:
            labels = (
                normalized_provider,
                normalized_isolation,
                normalized_outcome,
            )
            self.research_provider_calls.labels(*labels).inc()
            if duration is not None:
                self.research_provider_call_duration.labels(*labels).observe(
                    duration / 1000.0
                )
        except Exception:
            return

    # 渲染。
    def render(self) -> bytes:
        return generate_latest(self.registry)


# 路由 label。
def _route_label(request: Request) -> str:
    # 用路由模板（/v1/index-jobs/{job_id}）而非原始路径，避免路径参数撑爆标签基数。
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


# 统计每个请求的计数、耗时与在途数；置于访问控制外层，故 401/429 也计入。
class MetricsMiddleware:
    # 统计每个请求的计数、耗时与在途数；置于访问控制外层，故 401/429 也计入。
    def __init__(self, app: ASGIApp, metrics: Metrics):
        self.app = app
        self._metrics = metrics

    # 分发结果。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        start = time.perf_counter()
        self._metrics.in_progress.inc()
        # 默认 500：call_next 抛出未被兜底的异常时也记一条并让异常透传，不静默丢指标。
        status = "500"

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = str(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            self._metrics.in_progress.dec()
            route = _route_label(request)
            elapsed = time.perf_counter() - start
            self._metrics.requests.labels(request.method, route, status).inc()
            self._metrics.duration.labels(request.method, route).observe(elapsed)


__all__ = ["CONTENT_TYPE_LATEST", "Metrics", "MetricsMiddleware"]
