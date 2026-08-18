from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, Callable, Mapping
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from cogdoc import __version__
from cogdoc.api.access_control import (
    AccessControlMiddleware,
    TokenBucketRateLimiter,
    build_rate_limiter,
)
from cogdoc.api.audit import AuditStore
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.claim_verification_store import (
    ClaimVerificationObservationStore,
    SqliteClaimVerificationObservationStore,
)
from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.metrics import Metrics, MetricsMiddleware
from cogdoc.api.persistence import SqliteJobStore, SqliteSessionStore
from cogdoc.api.retrieval_feedback_store import RetrievalFeedbackStore
from cogdoc.api.retrieval_eval_draft_store import RetrievalEvalDraftStore
from cogdoc.api.research_job_store import ResearchJobStore
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.routes import (
    agent_router,
    access_router,
    auth_router,
    chat_router,
    claim_verification_router,
    documents_router,
    feedback_router,
    health_router,
    index_migrations_router,
    knowledge_router,
    retrieval_eval_drafts_router,
    retrieval_diagnostics_router,
    research_router,
    traces_router,
)
from cogdoc.api.schemas import ErrorCode, build_error_response
from cogdoc.api.session_store import SessionStore
from cogdoc.api.tenant_quota import TenantQuotaManager, TenantQuotaPolicy
from cogdoc.api.tenancy import Permission, Principal, Role
from cogdoc.api.webhooks import WebhookDispatcher
from cogdoc.agents.research_planner import propose_research_plan
import logging
from cogdoc.config.settings import get_settings
from cogdoc.observability.logger import configure_logging, log_event
from cogdoc.service.chat_service import ChatResult, run_chat, run_chat_sync
from cogdoc.service.ingest_service import cancel_all_timers, drain_purge_queue
from cogdoc.service.mutation_journal import shared_mutation_journal
from cogdoc.service.process_lock import (
    SingleInstanceError,
    acquire_single_instance_lock,
    locking_supported,
    release_single_instance_lock,
    strict_single_process,
)
from cogdoc.service.research_execution import ResearchExecutionManager
from cogdoc.service.research_observability import ResearchObserver
from cogdoc.service.research_planning_runtime import ResearchPlanningRuntime
from cogdoc.service.sweeper import BackgroundSweeper
from cogdoc.state_runtime import StateRuntime


ChatRunner = Callable[..., ChatResult]


# A lifespan-local file object would be finalized as soon as the context frame
# closes, implicitly dropping its flock even when a daemon research worker is
# still using process-local runtime state.  Keep every acquired handle alive at
# module scope until a fully drained shutdown releases it.  If shutdown remains
# undrained, normal descriptor cleanup at process exit releases the lock.
_RETAINED_SINGLE_INSTANCE_LOCKS: dict[int, object] = {}


def _retain_single_instance_lock(lock_fh: object | None) -> None:
    if lock_fh is not None:
        _RETAINED_SINGLE_INSTANCE_LOCKS[id(lock_fh)] = lock_fh


def _release_retained_single_instance_lock(lock_fh: object | None) -> None:
    if lock_fh is None:
        return
    release_single_instance_lock(lock_fh)
    _RETAINED_SINGLE_INSTANCE_LOCKS.pop(id(lock_fh), None)


# 创建反馈存储。
def _default_feedback_store():
    return StateRuntime.default_feedback_store()


def _default_feedback_analysis_store():
    return StateRuntime.default_feedback_analysis_store()


def _default_knowledge_store():
    return StateRuntime.default_knowledge_store()


def _default_retrieval_feedback_store():
    return StateRuntime.default_retrieval_feedback_store()


def _default_retrieval_eval_draft_store():
    return StateRuntime.default_retrieval_eval_draft_store()


def _research_acl_checker(auth_store, access_store, job: Mapping) -> bool:
    """Re-evaluate a durable research snapshot before each background phase."""

    authorization = job.get("authorization")
    if not isinstance(authorization, Mapping):
        return True
    try:
        tenant_id = str(authorization["tenant_id"])
        subject_id = str(authorization["created_by"])
        storage_id = str(job["kb_id"])
        role_value = str(authorization.get("creator_role") or "viewer")
        membership_id = None
        if authorization.get("auth_kind") == "user_session":
            if auth_store is None:
                return False
            membership = auth_store.membership(tenant_id, subject_id)
            if not isinstance(membership, Mapping):
                return False
            role_value = str(membership.get("role") or "")
            membership_id = str(
                membership.get("member_id") or membership.get("membership_id") or ""
            )
            if not membership_id:
                return False
        principal = Principal(
            tenant_id=tenant_id,
            subject_id=subject_id,
            role=Role(role_value),
            key_fingerprint=(
                "session:background-research"
                if authorization.get("auth_kind") == "user_session"
                else "background:research"
            ),
            membership_id=membership_id,
        )
        current = access_store.allowed_sources(
            principal,
            storage_id,
            tenant_id=tenant_id,
            permission=Permission.QUERY,
        )
        if not current.is_allowed:
            return False
        current_mode = str(current.mode.value)
        frozen_mode = str(authorization.get("mode") or "")
        if current_mode == "all":
            return frozen_mode in {"all", "subset"}
        if current_mode != "subset" or frozen_mode != "subset":
            return False
        frozen_sources = {
            str(item) for item in authorization.get("allowed_sources") or ()
        }
        return bool(frozen_sources and frozen_sources <= set(current.allowed_sources))
    except Exception:
        return False


# 构建未捕获异常响应。
def _unhandled_error_response(exc: Exception) -> JSONResponse:
    # 线程池关闭竞争窗口的调度异常归为暂时不可用，其余未预期异常归为内部错误；都不漏栈。
    if isinstance(exc, RuntimeError) and "shutdown" in str(exc):
        code, status, message = ErrorCode.MODEL_UNAVAILABLE, 503, "服务正在关闭，请重试"
    else:
        code, status, message = ErrorCode.INTERNAL_ERROR, 500, "服务内部错误"
    error = build_error_response(
        code, message, details={"error_class": type(exc).__name__}
    )
    return JSONResponse(status_code=status, content=error.model_dump())


# 创建服务应用。
def create_app(
    *,
    chat_runner: ChatRunner | None = None,
    chat_stream_runner: Callable | None = None,
    session_store: SessionStore | SqliteSessionStore | None = None,
    kb_registry: KnowledgeBaseRegistry | None = None,
    index_jobs: IndexJobManager | None = None,
    feedback_store: FeedbackStore | None = None,
    feedback_analysis_store: FeedbackAnalysisStore | None = None,
    knowledge_store: DerivedKnowledgeStore | None = None,
    retrieval_feedback_store: RetrievalFeedbackStore | None = None,
    retrieval_eval_draft_store: RetrievalEvalDraftStore | None = None,
    research_job_store: ResearchJobStore | None = None,
    research_execution_manager: ResearchExecutionManager | None = None,
    research_plan_generator: Callable | None = None,
    state_runtime: StateRuntime | None = None,
    webhook_dispatcher: WebhookDispatcher | None = None,
    derived_knowledge_index_refresher: Callable | None = None,
    derived_knowledge_index_statuser: Callable | None = None,
    close_state_runtime_on_shutdown: bool | None = None,
    api_keys: set[str] | None = None,
    api_principals: Mapping[str, Principal | Mapping[str, str]] | None = None,
    eval_review_api_keys: set[str] | None = None,
    rate_limiter: TokenBucketRateLimiter | None = None,
    audit_store: AuditStore | None = None,
    auth_store: AuthStore | None = None,
    resource_access_store: ResourceAccessStore | None = None,
    claim_verification_observation_store: Any | None = None,
    self_registration_enabled: bool | None = None,
    offload_workers: int | None = None,
) -> FastAPI:
    if auth_store is not None and resource_access_store is None:
        raise ValueError(
            "account authentication requires a fail-closed resource_access_store"
        )

    # 管理结果。
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.lifecycle_status = "starting"
        # 非命令行入口也要在启动时配置日志，否则节点日志会静默丢失。
        configure_logging()
        # 单进程独占锁，严格模式下拿不到锁就拒绝启动。
        lock_fh = acquire_single_instance_lock()
        if lock_fh is None and strict_single_process():
            # 无法取得锁时严格拒绝启动，避免单进程架构出现并发写。
            reason = (
                "平台不支持进程锁，无法保证单实例"
                if not locking_supported()
                else "已有 CogDoc 实例运行"
            )
            raise SingleInstanceError(f"{reason}；如确需放行请设 COGDOC_ALLOW_MULTI=1")
        if lock_fh is None:
            log_event(
                "startup", "single_instance_unconfirmed", {}, level=logging.WARNING
            )
        _retain_single_instance_lock(lock_fh)
        app.state.single_instance_lock_handle = lock_fh
        try:
            # 回放上次进程崩溃遗留的源文件变更，使源目录与当前索引代一致。
            recovered = shared_mutation_journal().recover_all()
            if recovered:
                log_event(
                    "startup",
                    "mutation_journal_recovered",
                    {},
                    level=logging.WARNING,
                    count=len(recovered),
                )
            # 必须在拿到单实例锁且变更日志恢复之后对账，避免误改其他实例的任务状态。
            app.state.index_jobs.reconcile_orphans()
            research_manager = getattr(app.state, "research_execution_manager", None)
            if research_manager is not None:
                research_manager.reconcile_orphans()
            # 重试上次遗留的删库外部资源清理，持久队列在此兜底。
            drain_purge_queue()
            # 后台清扫僵尸索引代、空闲执行器和锁表。
            sweeper = BackgroundSweeper(
                kb_ids_provider=lambda: [
                    str(r.get("storage_id") or r["kb_id"])
                    for r in app.state.kb_registry.list()
                ],
                index_jobs=app.state.index_jobs,
            )
            sweeper.start()
            app.state.sweeper = sweeper
            # 鉴权未配置时接口对外开放，启动时告警。
            if not app.state.auth_enabled:
                log_event(
                    "startup",
                    "auth_disabled",
                    {},
                    level=logging.WARNING,
                )
            app.state.lifecycle_status = "ready"
            yield
        finally:
            app.state.lifecycle_status = "stopping"
            research_drained = True
            planning_drained = True
            # 每步独立容错，进程锁放最外层，避免某个关闭异常跳过后续清理。
            try:
                active_sweeper = getattr(app.state, "sweeper", None)
                if active_sweeper is not None:
                    active_sweeper.stop()
            except Exception as exc:
                log_event(
                    "shutdown",
                    "sweeper_stop_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                research_manager = getattr(
                    app.state, "research_execution_manager", None
                )
                if research_manager is not None:
                    # Research leases are invalidated durably before this
                    # returns. Never let one opaque synchronous provider call
                    # hold the FastAPI lifespan open indefinitely.
                    research_drained = research_manager.shutdown(wait=False)
            except Exception as exc:
                research_drained = False
                log_event(
                    "shutdown",
                    "research_execution_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                # Automatic-planning source/model work has its own bounded
                # daemon executor. Signal active controls without allowing an
                # opaque call to hold the lifespan callback open.
                planning_executor = getattr(
                    app.state, "research_planning_executor", None
                )
                if planning_executor is not None:
                    result = planning_executor.shutdown(
                        wait=False,
                        cancel_futures=True,
                    )
                    # Unknown executor implementations must prove they drained;
                    # ``None`` is not sufficient to release process-local
                    # runtime state or the cross-process mutation lock.
                    planning_drained = result is True
            except Exception as exc:
                planning_drained = False
                log_event(
                    "shutdown",
                    "research_planning_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                app.state.index_migration_manager.shutdown(wait=True)
            except Exception as exc:
                log_event(
                    "shutdown",
                    "index_migration_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                # 先排空请求卸载线程池。
                app.state.offload_executor.shutdown(wait=True)
            except Exception as exc:
                log_event(
                    "shutdown",
                    "offload_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                # 再排空索引任务，它们提交时仍可能新建清理定时器。
                app.state.index_jobs.shutdown(wait=True)
            except Exception as exc:
                log_event(
                    "shutdown",
                    "index_jobs_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            drained = False
            try:
                # 所有定时器生产者都停止后再统一取消和等待。
                drained = cancel_all_timers() and research_drained and planning_drained
            except Exception as exc:
                log_event(
                    "shutdown",
                    "timer_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                should_close_runtime = getattr(
                    app.state,
                    "close_state_runtime_on_shutdown",
                    False,
                )
                if should_close_runtime and drained:
                    app.state.state_runtime.close()
                elif should_close_runtime:
                    log_event(
                        "shutdown",
                        "state_runtime_close_deferred_threads_alive",
                        {},
                        level=logging.WARNING,
                    )
            except Exception as exc:
                log_event(
                    "shutdown",
                    "state_runtime_close_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            for store_name, ownership_name in (
                ("auth_store", "close_auth_store_on_shutdown"),
                ("resource_access_store", "close_resource_access_store_on_shutdown"),
                (
                    "claim_verification_observation_store",
                    "close_claim_verification_observation_store_on_shutdown",
                ),
            ):
                try:
                    active_store = getattr(app.state, store_name, None)
                    if active_store is not None and getattr(
                        app.state, ownership_name, False
                    ):
                        active_store.close()
                except Exception as exc:
                    log_event(
                        "shutdown",
                        f"{store_name}_close_failed",
                        {},
                        level=logging.ERROR,
                        error_class=type(exc).__name__,
                    )
            # 仅在后台线程确已排空时才显式释放进程锁，否则留给进程退出自动释放。
            if drained:
                _release_retained_single_instance_lock(lock_fh)
                app.state.single_instance_lock_handle = None
            else:
                log_event(
                    "shutdown",
                    "lock_release_deferred_threads_alive",
                    {},
                    level=logging.WARNING,
                )
            app.state.lifecycle_status = "stopped"

    app = FastAPI(
        title="CogDoc API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.lifecycle_status = "created"
    app.state.auth_store = auth_store
    app.state.resource_access_store = resource_access_store
    # Keep account authentication distinct from optional static API-key auth.
    # Readiness requires both durable security stores only in account mode.
    app.state.account_auth_enabled = auth_store is not None
    app.state.self_registration_enabled = (
        get_settings().cogdoc_self_registration_enabled
        if self_registration_enabled is None
        else bool(self_registration_enabled)
    )
    # Injected stores are caller-owned. The module-level production stores are
    # closed explicitly by their process and can be shared by app factories.
    app.state.close_auth_store_on_shutdown = False
    app.state.close_resource_access_store_on_shutdown = False
    observation_settings = get_settings()
    app.state.claim_verification_observation_store = (
        claim_verification_observation_store
        if claim_verification_observation_store is not None
        else ClaimVerificationObservationStore(
            retention_days=(
                observation_settings.claim_verification_observation_retention_days
            ),
            max_per_tenant=(
                observation_settings.claim_verification_observation_max_per_tenant
            ),
        )
    )
    app.state.close_claim_verification_observation_store_on_shutdown = False
    # Create operational telemetry before background managers so both HTTP and
    # post-202 research work share one app-local Prometheus registry.
    app.state.metrics = Metrics()
    app.state.research_observer = ResearchObserver(app.state.metrics)
    store_overrides = (
        feedback_store,
        feedback_analysis_store,
        knowledge_store,
        retrieval_feedback_store,
        retrieval_eval_draft_store,
        research_job_store,
    )
    if state_runtime is not None and any(
        store is not None for store in store_overrides
    ):
        raise ValueError(
            "state_runtime cannot be combined with individual store overrides"
        )
    runtime = state_runtime or StateRuntime.from_settings(
        feedback_store=feedback_store,
        feedback_analysis_store=feedback_analysis_store,
        knowledge_store=knowledge_store,
        retrieval_feedback_store=retrieval_feedback_store,
        retrieval_eval_draft_store=retrieval_eval_draft_store,
        research_job_store=research_job_store,
    )
    app.state.state_runtime = runtime
    app.state.close_state_runtime_on_shutdown = (
        state_runtime is None
        if close_state_runtime_on_shutdown is None
        else bool(close_state_runtime_on_shutdown)
    )
    # 运行器和存储可注入，便于脱离真实图与持久态测试交付层。
    app.state.chat_runner = chat_runner or partial(
        run_chat_sync,
        state_runtime=runtime,
    )
    app.state.chat_stream_runner = chat_stream_runner or partial(
        run_chat,
        state_runtime=runtime,
    )
    app.state.session_store = session_store or SessionStore()
    # 入库注册表/任务管理器可注入，便于测试用假入库函数。
    app.state.kb_registry = kb_registry or KnowledgeBaseRegistry()
    from cogdoc.service.index_migration import IndexMigrationManager, IndexMigrationRunner

    app.state.index_migration_manager = IndexMigrationManager(
        IndexMigrationRunner(
            source_dir_for=app.state.kb_registry.source_dir,
            knowledge_store=runtime.knowledge_store,
            refresh_derived_knowledge=runtime.refresh_derived_knowledge_index,
        )
    )
    # 知识库存在性检查用于写入防复活，注入版由测试自行控制。
    if index_jobs is None:
        app.state.index_jobs = IndexJobManager(
            kb_exists=app.state.kb_registry.exists,
            knowledge_store=runtime.knowledge_store,
        )
    else:
        bind_knowledge_store = getattr(index_jobs, "bind_knowledge_store", None)
        if callable(bind_knowledge_store):
            try:
                bind_knowledge_store(runtime.knowledge_store)
            except Exception:
                if state_runtime is None:
                    try:
                        runtime.close()
                    except Exception:
                        pass
                raise
        app.state.index_jobs = index_jobs
    # 有界线程池限制本地算力并发，缓解高并发下精排/嵌入的坏邻居效应。
    app.state.offload_executor = ThreadPoolExecutor(
        max_workers=offload_workers or get_settings().cogdoc_offload_workers,
        thread_name_prefix="cogdoc-offload",
    )
    app.state.research_planning_executor = ResearchPlanningRuntime(
        max_workers=get_settings().cogdoc_research_planning_workers,
        max_pending=get_settings().cogdoc_research_planning_max_pending,
        thread_name_prefix="cogdoc-research-planning",
    )
    app.state.research_execution_manager = research_execution_manager or (
        ResearchExecutionManager.from_runtime(
            runtime.research_job_store,
            state_runtime=runtime,
            kb_exists=app.state.kb_registry.exists,
            max_workers=get_settings().cogdoc_research_workers,
            top_k=get_settings().cogdoc_research_retrieval_top_k,
            authorization_checker=(
                partial(
                    _research_acl_checker,
                    app.state.auth_store,
                    app.state.resource_access_store,
                )
                if app.state.resource_access_store is not None
                else None
            ),
        )
        if runtime.research_job_store is not None
        else None
    )
    bind_observer = getattr(app.state.research_execution_manager, "bind_observer", None)
    if callable(bind_observer):
        bind_observer(app.state.research_observer)
    bind_authorization = getattr(
        app.state.research_execution_manager,
        "bind_authorization_checker",
        None,
    )
    if app.state.resource_access_store is not None:
        if not callable(bind_authorization):
            raise ValueError(
                "resource_access_store requires an authorization-aware research manager"
            )
        bind_authorization(
            partial(
                _research_acl_checker,
                app.state.auth_store,
                app.state.resource_access_store,
            )
        )
    app.state.research_plan_generator = research_plan_generator or partial(
        propose_research_plan,
        observer=app.state.research_observer,
    )
    # 旧 app.state 属性保留为 runtime store 的身份别名，兼容路由与注入测试。
    app.state.feedback_store = runtime.feedback_store
    app.state.feedback_analysis_store = runtime.feedback_analysis_store
    app.state.knowledge_store = runtime.knowledge_store
    app.state.retrieval_feedback_store = runtime.retrieval_feedback_store
    app.state.retrieval_eval_draft_store = runtime.retrieval_eval_draft_store
    app.state.research_job_store = runtime.research_job_store
    app.state.webhook_dispatcher = webhook_dispatcher or WebhookDispatcher()

    # 访问控制留空则鉴权关闭，限流默认按配置令牌桶。
    settings = get_settings()
    resolved_review_keys = (
        settings.eval_review_api_key_set
        if eval_review_api_keys is None
        else set(eval_review_api_keys)
    )
    app.state.eval_review_api_keys = resolved_review_keys
    app.state.derived_knowledge_index_auto_refresh = (
        settings.cogdoc_derived_knowledge_index_auto_refresh
    )
    app.state.derived_knowledge_index_refresher = (
        derived_knowledge_index_refresher or runtime.refresh_derived_knowledge_index
    )
    app.state.derived_knowledge_index_statuser = (
        derived_knowledge_index_statuser or runtime.derived_knowledge_index_status
    )
    app.state.derived_knowledge_index_error_recorder = (
        runtime.record_derived_knowledge_index_error
    )
    resolved_keys = set(settings.api_key_set if api_keys is None else api_keys)
    raw_principals = (
        settings.api_principal_map if api_principals is None else api_principals
    )
    resolved_principals: dict[str, Principal] = {}
    for raw_key, raw_principal in raw_principals.items():
        if isinstance(raw_principal, Principal):
            principal = raw_principal
        elif isinstance(raw_principal, Mapping):
            principal = Principal.for_api_key(
                raw_key,
                tenant_id=str(raw_principal.get("tenant_id") or ""),
                subject_id=str(raw_principal.get("subject_id") or ""),
                role=str(raw_principal.get("role") or ""),
            )
        else:
            raise TypeError("api_principals values must be Principal or mapping")
        resolved_principals[raw_key] = principal
    app.state.explicit_principal_fingerprints = {
        principal.key_fingerprint for principal in resolved_principals.values()
    }
    # Review keys are administrator credentials and therefore also authenticate
    # ordinary endpoints; keeping one union avoids middleware rejecting them first.
    resolved_keys.update(resolved_review_keys)
    resolved_limiter = rate_limiter or build_rate_limiter(
        settings.rate_limit_per_minute, settings.rate_limit_burst
    )
    app.state.auth_enabled = bool(
        resolved_keys or resolved_principals or app.state.auth_store is not None
    )
    app.state.tenant_quota = TenantQuotaManager(
        app.state.kb_registry,
        TenantQuotaPolicy(
            max_knowledge_bases=settings.cogdoc_tenant_max_knowledge_bases,
            max_documents=settings.cogdoc_tenant_max_documents,
            max_storage_bytes=settings.cogdoc_tenant_max_storage_mb * 1024 * 1024,
        ),
    )
    # ``create_app`` is also the test/application-factory seam.  Audit is
    # opt-in there so independent in-process apps never append into one shared
    # production file; the module-level production app injects the durable
    # store explicitly below.
    app.state.audit_store = audit_store

    @app.get("/v1/tenant", tags=["tenant"])
    async def current_tenant(request: Request):
        from cogdoc.api.offload import run_sync
        from cogdoc.api.tenant_scope import request_principal

        principal = request_principal(request)
        quota = await run_sync(
            request.app.state.offload_executor,
            request.app.state.tenant_quota.snapshot,
            principal.tenant_id,
        )
        return {
            "schema_version": "v1",
            "tenant_id": principal.tenant_id,
            "subject_id": principal.subject_id,
            "role": principal.role.value,
            "permissions": sorted(
                permission.value for permission in principal.permissions
            ),
            "quota": quota,
        }

    @app.get("/v1/auth/config", tags=["auth"])
    async def auth_config():
        return {
            "schema_version": "v1",
            "account_auth_enabled": app.state.account_auth_enabled,
            "self_registration_enabled": app.state.self_registration_enabled,
        }

    @app.get("/v1/audit-events", tags=["tenant"])
    async def list_audit_events(
        request: Request,
        limit: int = Query(default=100, ge=1, le=1000),
        before_sequence: int | None = Query(default=None, ge=1),
    ):
        from cogdoc.api.tenant_scope import request_principal

        principal = request_principal(request)
        active_audit_store = request.app.state.audit_store
        if active_audit_store is None:
            return JSONResponse(
                status_code=503,
                content=build_error_response(
                    ErrorCode.INTERNAL_ERROR, "审计存储不可用"
                ).model_dump(),
            )
        from cogdoc.api.offload import run_sync

        events = await run_sync(
            request.app.state.offload_executor,
            active_audit_store.list,
            principal.tenant_id,
            limit=limit,
            before_sequence=before_sequence,
        )
        next_cursor = events[-1]["sequence"] if len(events) == limit else None
        return {
            "schema_version": "v1",
            "tenant_id": principal.tenant_id,
            "events": events,
            "next_before_sequence": next_cursor,
        }

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=resolved_keys,
        principals=resolved_principals,
        rate_limiter=resolved_limiter,
        auth_store=app.state.auth_store,
    )
    # 指标中间件在访问控制外层（后加=最外层），故 401/429 也被计入请求统计。
    app.add_middleware(MetricsMiddleware, metrics=app.state.metrics)

    # 处理未预期异常。
    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return _unhandled_error_response(exc)

    app.include_router(chat_router)
    app.include_router(claim_verification_router)
    app.include_router(auth_router)
    app.include_router(access_router)
    app.include_router(agent_router)
    app.include_router(health_router)
    app.include_router(index_migrations_router)
    app.include_router(documents_router)
    app.include_router(feedback_router)
    app.include_router(knowledge_router)
    app.include_router(retrieval_eval_drafts_router)
    app.include_router(retrieval_diagnostics_router)
    app.include_router(research_router)
    app.include_router(traces_router)
    return app


# 生产入口会话与入库任务落盘，进程重启不丢，默认创建仍便于测试隔离。
_settings = get_settings()
_db_path = _settings.state_db_path
_kb_registry = KnowledgeBaseRegistry()
_state_runtime = StateRuntime.from_settings(_settings)
_auth_store = (
    AuthStore(
        _db_path,
        session_ttl_seconds=_settings.cogdoc_auth_session_ttl_seconds,
        invite_ttl_seconds=_settings.cogdoc_auth_invite_ttl_seconds,
        max_failed_logins=_settings.cogdoc_auth_max_failed_logins,
        lockout_seconds=_settings.cogdoc_auth_lockout_seconds,
    )
    if _settings.cogdoc_account_auth_enabled
    else None
)
_resource_access_store = (
    ResourceAccessStore(_db_path, legacy_workspace_default=False)
    if _auth_store is not None
    else None
)
_claim_verification_observation_store = SqliteClaimVerificationObservationStore(
    _db_path,
    retention_days=_settings.claim_verification_observation_retention_days,
    max_per_tenant=_settings.claim_verification_observation_max_per_tenant,
)
app = create_app(
    state_runtime=_state_runtime,
    close_state_runtime_on_shutdown=True,
    session_store=SqliteSessionStore(_db_path, memory_policy=_settings.memory_policy),
    kb_registry=_kb_registry,
    index_jobs=IndexJobManager(
        job_store=SqliteJobStore(_db_path, reconcile_on_init=False),
        kb_exists=_kb_registry.exists,
        knowledge_store=_state_runtime.knowledge_store,
    ),
    audit_store=AuditStore(_settings.audit_log_path),
    auth_store=_auth_store,
    resource_access_store=_resource_access_store,
    claim_verification_observation_store=_claim_verification_observation_store,
    self_registration_enabled=_settings.cogdoc_self_registration_enabled,
)
app.state.close_auth_store_on_shutdown = _auth_store is not None
app.state.close_resource_access_store_on_shutdown = _resource_access_store is not None
app.state.close_claim_verification_observation_store_on_shutdown = True
