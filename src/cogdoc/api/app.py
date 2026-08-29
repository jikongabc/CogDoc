import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from functools import partial
import hashlib
from pathlib import Path
import threading
import time
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
from cogdoc.api.audit_exports import AuditExportManager, AuditExportStore
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.claim_verification_store import (
    ClaimVerificationObservationStore,
    SqliteClaimVerificationObservationStore,
)
from cogdoc.api.claim_verification_review_store import (
    ClaimVerificationReviewStore,
    SqliteClaimVerificationReviewStore,
)
from cogdoc.api.connector_scope import assert_active_kb_incarnation
from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.metrics import Metrics, MetricsMiddleware
from cogdoc.api.oidc import (
    OIDCClient,
    OIDCFlowStore,
    OIDCManager,
    OIDCProviderConfig,
)
from cogdoc.api.persistence import SqliteJobStore, SqliteSessionStore
from cogdoc.api.retrieval_feedback_store import RetrievalFeedbackStore
from cogdoc.api.retrieval_eval_draft_store import RetrievalEvalDraftStore
from cogdoc.api.research_job_store import (
    ResearchJobStateConflictError,
    ResearchJobStore,
    SqliteResearchJobStore,
)
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.scim import SCIMAccess, parse_scim_access_registry
from cogdoc.api.routes import (
    agent_router,
    access_router,
    audit_exports_router,
    auth_router,
    chat_router,
    claim_verification_router,
    connector_credentials_router,
    connector_oauth_router,
    connections_router,
    documents_router,
    feedback_router,
    health_router,
    ha_operations_router,
    index_migrations_router,
    knowledge_router,
    oidc_router,
    scim_router,
    service_accounts_router,
    service_account_policy_router,
    retrieval_eval_drafts_router,
    retrieval_diagnostics_router,
    research_router,
    source_operations_router,
    tasks_router,
    traces_router,
)
from cogdoc.api.schemas import ErrorCode, build_error_response
from cogdoc.api.session_store import SessionStore
from cogdoc.ha.session_store import DistributedSessionStore
from cogdoc.ha.api_state import DistributedKnowledgeBaseRegistry
from cogdoc.ha.feedback import (
    DistributedFeedbackAnalysisStore,
    DistributedFeedbackStore,
)
from cogdoc.ha.derived_knowledge import DistributedDerivedKnowledgeStore
from cogdoc.ha.retrieval_feedback import (
    DistributedRetrievalEvalDraftStore,
    DistributedRetrievalFeedbackStore,
)
from cogdoc.api.tenant_quota import TenantQuotaManager, TenantQuotaPolicy
from cogdoc.api.tenancy import (
    ROLE_PERMISSIONS,
    Permission,
    Principal,
    Role,
    fingerprint_api_key,
)
from cogdoc.api.webhooks import WebhookDispatcher, validate_webhook_url
from cogdoc.agents.research_planner import propose_research_plan
import logging
from cogdoc.config.settings import get_settings
from cogdoc.observability.logger import configure_logging, log_event
from cogdoc.service.chat_service import ChatResult, run_chat, run_chat_sync
from cogdoc.service.chroma_cleanup import sweep_orphan_segment_directories
from cogdoc.service.ingest_service import cancel_all_timers, drain_purge_queue
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.kb_readers import KBReadUnavailable
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
            session_id = str(authorization.get("session_id") or "")
            frozen_membership_id = str(authorization.get("membership_id") or "")
            session_is_active = getattr(auth_store, "session_is_active", None)
            if (
                not session_id
                or not frozen_membership_id
                or not callable(session_is_active)
                or not session_is_active(
                    session_id=session_id,
                    user_id=subject_id,
                    workspace_id=tenant_id,
                )
            ):
                return False
            membership = auth_store.membership(tenant_id, subject_id)
            if not isinstance(membership, Mapping):
                return False
            role_value = str(membership.get("role") or "")
            membership_id = str(
                membership.get("member_id") or membership.get("membership_id") or ""
            )
            if not membership_id or membership_id != frozen_membership_id:
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
    # 删除屏障与线程池关闭竞争窗口都属于可重试的暂时不可用；其余未预期
    # 异常归为内部错误。响应只暴露异常类型，不泄漏堆栈或内部消息。
    if isinstance(exc, KBReadUnavailable):
        code, status, message = (
            ErrorCode.MODEL_UNAVAILABLE,
            503,
            "知识库正在变更，请稍后重试",
        )
    elif isinstance(exc, RuntimeError) and "shutdown" in str(exc):
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
    session_store: SessionStore
    | SqliteSessionStore
    | DistributedSessionStore
    | None = None,
    ha_index_provider: Any | None = None,
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
    derived_knowledge_index_clearer: Callable[[str], None] | None = None,
    close_state_runtime_on_shutdown: bool | None = None,
    api_keys: set[str] | None = None,
    api_principals: Mapping[str, Principal | Mapping[str, str]] | None = None,
    eval_review_api_keys: set[str] | None = None,
    rate_limiter: TokenBucketRateLimiter | None = None,
    audit_store: AuditStore | None = None,
    audit_export_manager: AuditExportManager | None = None,
    auth_store: AuthStore | None = None,
    oidc_manager: Any | None = None,
    scim_access_registry: Mapping[str, Any] | None = None,
    resource_access_store: ResourceAccessStore | None = None,
    claim_verification_observation_store: Any | None = None,
    claim_verification_review_store: Any | None = None,
    self_registration_enabled: bool | None = None,
    offload_workers: int | None = None,
    chat_stream_workers: int | None = None,
    artifact_io_workers: int | None = None,
    connector_cleanup_workers: int | None = None,
    connection_store: Any | None = None,
    connector_sync_store: Any | None = None,
    connector_credential_vault: Any | None = None,
    connector_oauth: Any | None = None,
    connector_oauth_redirect_uris: Mapping[str, str] | None = None,
    source_catalog: Any | None = None,
    source_artifact_store: Any | None = None,
    sync_manager: Any | None = None,
    ha_runtime: Any | None = None,
) -> FastAPI:
    if auth_store is not None and resource_access_store is None:
        raise ValueError(
            "account authentication requires a fail-closed resource_access_store"
        )
    if oidc_manager is not None:
        if auth_store is None:
            raise ValueError("OIDC requires account authentication")
        if getattr(oidc_manager, "auth_store", None) is not auth_store:
            raise ValueError("injected OIDC manager must share the app AuthStore")
    if scim_access_registry and auth_store is None:
        raise ValueError("SCIM requires account authentication")
    scim_policies: dict[str, SCIMAccess] = {}
    for fingerprint, access in dict(scim_access_registry or {}).items():
        if (
            not isinstance(fingerprint, str)
            or not isinstance(access, SCIMAccess)
            or fingerprint != access.token_fingerprint
        ):
            raise ValueError("SCIM access registry contains an invalid entry")
        previous = scim_policies.setdefault(access.workspace_id, access)
        if (
            previous.issuer != access.issuer
            or previous.default_role != access.default_role
            or dict(previous.group_role_map) != dict(access.group_role_map)
        ):
            raise ValueError(
                "SCIM tokens for one workspace must share one provisioning policy"
            )
    if connector_oauth is not None:
        if connector_credential_vault is None:
            raise ValueError("injected connector_oauth requires a credential vault")
        if (
            getattr(connector_oauth, "credential_vault", None)
            is not connector_credential_vault
        ):
            raise ValueError(
                "injected connector_oauth must share the app credential vault"
            )
        coordinator_redirects = getattr(connector_oauth, "redirect_uris", None)
        supplied_redirects = dict(connector_oauth_redirect_uris or {})
        if (
            not isinstance(coordinator_redirects, Mapping)
            or dict(coordinator_redirects) != supplied_redirects
        ):
            raise ValueError(
                "injected connector_oauth redirect URIs must match its adapters"
            )
    if sync_manager is not None:
        if connection_store is None or connector_sync_store is None:
            raise ValueError("injected sync_manager requires explicit connector stores")
        manager_runtime = getattr(sync_manager, "runtime", None)
        if (
            getattr(sync_manager, "connection_store", None) is not connection_store
            or getattr(sync_manager, "sync_store", None) is not connector_sync_store
            or getattr(manager_runtime, "store", None) is not connector_sync_store
        ):
            raise ValueError(
                "injected sync_manager must share the app connector stores"
            )

    # 管理结果。
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.lifecycle_status = "starting"
        app.state.readiness_probe_cache = None
        # 非命令行入口也要在启动时配置日志，否则节点日志会静默丢失。
        configure_logging()
        # 单进程独占锁，严格模式下拿不到锁就拒绝启动。
        lock_fh = acquire_single_instance_lock()
        multi_writer_safe = bool(
            app.state.ha_runtime is not None
            and getattr(app.state.ha_runtime, "api_multi_writer_safe", False)
        )
        if lock_fh is None and strict_single_process() and not multi_writer_safe:
            # 无法取得锁时严格拒绝启动，避免单进程架构出现并发写。
            reason = (
                "平台不支持进程锁，无法保证单实例"
                if not locking_supported()
                else "已有 CogDoc 实例运行"
            )
            raise SingleInstanceError(f"{reason}；如确需放行请设 COGDOC_ALLOW_MULTI=1")
        if lock_fh is None and not multi_writer_safe:
            log_event(
                "startup", "single_instance_unconfirmed", {}, level=logging.WARNING
            )
        _retain_single_instance_lock(lock_fh)
        app.state.single_instance_lock_handle = lock_fh
        try:
            if getattr(app.state.state_runtime, "closed", False):
                raise RuntimeError(
                    "this application-owned StateRuntime is closed; create a new app"
                )
            if getattr(app.state, "offload_executor_shutdown", False):
                app.state.offload_executor = ThreadPoolExecutor(
                    max_workers=app.state.offload_worker_count,
                    thread_name_prefix="cogdoc-offload",
                )
                app.state.offload_executor_shutdown = False
            if getattr(app.state, "chat_stream_executor_shutdown", False):
                app.state.chat_stream_executor = ThreadPoolExecutor(
                    max_workers=app.state.chat_stream_worker_count,
                    thread_name_prefix="cogdoc-chat-stream",
                )
                app.state.chat_stream_executor_shutdown = False
            if getattr(app.state, "source_artifact_executor_shutdown", False):
                app.state.source_artifact_executor = ThreadPoolExecutor(
                    max_workers=app.state.source_artifact_worker_count,
                    thread_name_prefix="cogdoc-artifact-io",
                )
                app.state.source_artifact_executor_shutdown = False
            if getattr(app.state, "connector_cleanup_executor_shutdown", False):
                app.state.connector_cleanup_executor = ThreadPoolExecutor(
                    max_workers=app.state.connector_cleanup_worker_count,
                    thread_name_prefix="cogdoc-connector-cleanup",
                )
                app.state.connector_cleanup_executor_shutdown = False
            if getattr(app.state, "derived_index_executor_shutdown", False):
                app.state.derived_index_executor = ThreadPoolExecutor(
                    max_workers=app.state.derived_index_worker_count,
                    thread_name_prefix="cogdoc-derived-index",
                )
                app.state.derived_index_executor_shutdown = False
            if app.state.ha_runtime is not None:
                app.state.ha_runtime.start()
            recover_derived_refreshes = getattr(
                getattr(app.state, "ha_derived_knowledge_index", None),
                "recover_pending",
                None,
            )
            if app.state.derived_knowledge_index_auto_refresh and callable(
                recover_derived_refreshes
            ):
                try:
                    app.state.derived_index_executor.submit(recover_derived_refreshes)
                except RuntimeError as exc:
                    # The durable outbox remains authoritative and will be
                    # retried by the next startup/request-time refresh.
                    log_event(
                        "startup",
                        "derived_knowledge_refresh_recovery_submit_failed",
                        {},
                        level=logging.ERROR,
                        error_class=type(exc).__name__,
                    )
            scrub_ha_indexes = getattr(
                getattr(app.state, "ha_chat_index_provider", None),
                "scrub_current",
                None,
            )
            if callable(scrub_ha_indexes):
                from cogdoc.api.offload import run_sync

                if not await run_sync(
                    app.state.offload_executor,
                    scrub_ha_indexes,
                    limit=100,
                ):
                    raise RuntimeError("HA current index scrub failed")
            if app.state.audit_export_manager is not None:
                app.state.audit_export_manager.reopen()
                app.state.audit_export_manager.recover()
            # Every executor shut down by the previous lifespan is terminal.
            # Reopen all app-owned managers before accepting or reconciling
            # work so repeated embedded/test lifespans have identical service
            # semantics to the first startup.
            for manager_name in (
                "index_jobs",
                "index_migration_manager",
                "research_planning_executor",
                "research_execution_manager",
            ):
                manager = getattr(app.state, manager_name, None)
                reopen = getattr(manager, "reopen", None)
                if callable(reopen):
                    reopen()
            oauth_sessions = getattr(app.state, "connector_oauth_session_store", None)
            purge_oauth_sessions = getattr(oauth_sessions, "purge_expired", None)
            if callable(purge_oauth_sessions):
                from cogdoc.api.offload import run_sync

                await run_sync(
                    app.state.offload_executor,
                    purge_oauth_sessions,
                    limit=1000,
                )
            oidc_flows = getattr(app.state, "oidc_flow_store", None)
            purge_oidc_flows = getattr(oidc_flows, "purge_expired", None)
            if callable(purge_oidc_flows):
                from cogdoc.api.offload import run_sync

                await run_sync(
                    app.state.offload_executor,
                    purge_oidc_flows,
                    limit=1000,
                )
            auth_directory = getattr(app.state, "auth_store", None)
            reconcile_scim_policy = getattr(
                auth_directory, "reconcile_scim_policy", None
            )
            if callable(reconcile_scim_policy):
                from cogdoc.api.offload import run_sync

                for access in app.state.scim_policies.values():
                    await run_sync(
                        app.state.offload_executor,
                        reconcile_scim_policy,
                        workspace_id=access.workspace_id,
                        default_role=access.default_role,
                        group_role_map=access.group_role_map,
                    )
            reconcile_oauth_bindings = getattr(
                app.state, "reconcile_connector_oauth_bindings", None
            )
            if callable(reconcile_oauth_bindings):
                from cogdoc.api.offload import run_sync

                await run_sync(
                    app.state.offload_executor,
                    reconcile_oauth_bindings,
                )
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
            # Start no mutating workers until deletion recovery and orphan cleanup finish.
            drain_purge_queue()
            # 此时已持单实例锁、mutation journal 已恢复，且尚未接收请求；
            # 只有这个生命周期窗口可以安全回收 Chroma 历史逻辑删除遗留目录。
            try:
                orphan_cleanup = (
                    sweep_orphan_segment_directories(
                        app.state.state_runtime.derived_knowledge_index_persist_directory
                    )
                    if not app.state.ha_document_multiwriter_mode
                    else {"scanned": 0, "removed": 0, "bytes_reclaimed": 0}
                )
                if orphan_cleanup["removed"]:
                    log_event(
                        "startup",
                        "chroma_orphan_segments_reclaimed",
                        {},
                        count=orphan_cleanup["removed"],
                        bytes_reclaimed=orphan_cleanup["bytes_reclaimed"],
                    )
            except Exception as exc:
                log_event(
                    "startup",
                    "chroma_orphan_segment_cleanup_failed",
                    {},
                    level=logging.WARNING,
                    error_class=type(exc).__name__,
                )
            if (
                not app.state.ha_document_multiwriter_mode
                or app.state.ha_connector_multiwriter_mode
            ):
                app.state.sync_manager.recover()
            if app.state.ha_index_mirror is not None:
                app.state.ha_index_mirror.start()
            research_manager = getattr(app.state, "research_execution_manager", None)
            if research_manager is not None:
                start_dispatcher = getattr(research_manager, "start_dispatcher", None)
                if callable(start_dispatcher):
                    start_dispatcher()
                research_manager.reconcile_orphans()

            # 后台清扫僵尸索引代、空闲执行器和锁表。
            maintenance_tasks: dict[str, Callable[[], object]] = {}
            research_dispatch_store = getattr(
                app.state, "ha_research_dispatch_store", None
            )
            if research_dispatch_store is not None:
                ha_config = getattr(ha_runtime, "config", None)
                retention_seconds = float(
                    getattr(ha_config, "retention_seconds", 7 * 86_400.0)
                )
                maintenance_limit = int(
                    getattr(ha_config, "maintenance_batch_size", 100)
                )
                maintenance_tasks["ha_research_dispatch_prune"] = lambda: (
                    research_dispatch_store.prune_terminal(
                        before=time.time() - retention_seconds,
                        limit=maintenance_limit,
                    )
                )
            chat_store = getattr(app.state, "session_store", None)
            if isinstance(chat_store, DistributedSessionStore):
                chat_ttl = int(getattr(chat_store, "ttl_seconds", 0))
                chat_maintenance_limit = int(
                    getattr(
                        getattr(ha_runtime, "config", None),
                        "maintenance_batch_size",
                        100,
                    )
                )
                if chat_ttl > 0:
                    maintenance_tasks["ha_chat_session_prune"] = lambda: (
                        chat_store.prune_expired(
                            before=time.time() - chat_ttl,
                            limit=chat_maintenance_limit,
                        )
                    )
                else:
                    maintenance_tasks["ha_chat_execution_lease_prune"] = lambda: (
                        chat_store.prune_execution_leases(
                            before=time.time(),
                            limit=chat_maintenance_limit,
                        )
                    )
            index_generations = getattr(ha_runtime, "index_generations", None)
            prune_readers = getattr(index_generations, "prune_reader_leases", None)
            if callable(prune_readers):
                maintenance_tasks["ha_index_reader_lease_prune"] = lambda: (
                    prune_readers(
                        before=time.time(),
                        limit=int(
                            getattr(
                                getattr(ha_runtime, "config", None),
                                "maintenance_batch_size",
                                100,
                            )
                        ),
                    )
                )
            sweeper = BackgroundSweeper(
                kb_ids_provider=lambda: [
                    str(r.get("storage_id") or r["kb_id"])
                    for r in app.state.kb_registry.list()
                ],
                index_jobs=app.state.index_jobs,
                maintenance_tasks=maintenance_tasks,
            )
            sweeper.start()
            app.state.sweeper = sweeper
            # 鉴权未配置时仅保留回环本地 owner 模式，仍告警提醒生产部署启用身份。
            if not app.state.auth_enabled:
                log_event(
                    "startup",
                    "auth_disabled",
                    {},
                    level=logging.WARNING,
                    access_scope="loopback_only",
                )
            app.state.lifecycle_status = "ready"
            yield
        finally:
            app.state.lifecycle_status = "stopping"
            app.state.readiness_probe_cache = None
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
                if app.state.ha_index_mirror is not None:
                    if not app.state.ha_index_mirror.stop():
                        raise TimeoutError("HA index mirror did not stop")
            except Exception as exc:
                log_event(
                    "shutdown",
                    "ha_index_mirror_stop_failed",
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
                app.state.sync_manager.shutdown(wait=True)
            except Exception as exc:
                log_event(
                    "shutdown",
                    "sync_manager_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                if app.state.audit_export_manager is not None:
                    app.state.audit_export_manager.shutdown()
            except Exception as exc:
                log_event(
                    "shutdown",
                    "audit_export_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            try:
                app.state.connector_cleanup_executor.shutdown(wait=True)
            except Exception as exc:
                log_event(
                    "shutdown",
                    "connector_cleanup_executor_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            finally:
                app.state.connector_cleanup_executor_shutdown = True
            try:
                app.state.derived_index_executor.shutdown(
                    wait=True, cancel_futures=False
                )
            except Exception as exc:
                log_event(
                    "shutdown",
                    "derived_index_executor_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            finally:
                app.state.derived_index_executor_shutdown = True
            try:
                # Artifact verification can hash multi-gigabyte payloads. It
                # has a dedicated bounded pool so downloads cannot starve the
                # shared control-plane offload workers.
                app.state.source_artifact_executor.shutdown(wait=True)
            except Exception as exc:
                log_event(
                    "shutdown",
                    "source_artifact_executor_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            finally:
                app.state.source_artifact_executor_shutdown = True
            try:
                # A provider can still be blocked inside a bounded network
                # call.  Do not let one abandoned SSE response block lifespan
                # shutdown; queued streams are cancelled and running streams
                # observe their per-request stop signal when control returns.
                app.state.chat_stream_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                log_event(
                    "shutdown",
                    "chat_stream_executor_shutdown_failed",
                    {},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            finally:
                app.state.chat_stream_executor_shutdown = True
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
            finally:
                # A FastAPI app can enter its lifespan more than once in an
                # embedded server or test harness. The next startup replaces
                # this terminal ThreadPoolExecutor before accepting traffic.
                app.state.offload_executor_shutdown = True
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
            try:
                # Local index commits can enqueue their HA mirror only while
                # IndexJobManager drains.  The distributed runtime therefore
                # remains available until every local commit callback finishes.
                if app.state.ha_runtime is not None:
                    app.state.ha_runtime.shutdown()
            except Exception as exc:
                log_event(
                    "shutdown",
                    "ha_runtime_shutdown_failed",
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
                ("oidc_manager", "close_oidc_manager_on_shutdown"),
                ("oidc_flow_store", "close_oidc_flow_store_on_shutdown"),
                ("auth_store", "close_auth_store_on_shutdown"),
                ("resource_access_store", "close_resource_access_store_on_shutdown"),
                (
                    "claim_verification_observation_store",
                    "close_claim_verification_observation_store_on_shutdown",
                ),
                (
                    "claim_verification_review_store",
                    "close_claim_verification_review_store_on_shutdown",
                ),
                ("connection_store", "close_connection_store_on_shutdown"),
                (
                    "connector_sync_store",
                    "close_connector_sync_store_on_shutdown",
                ),
                (
                    "connector_credential_vault",
                    "close_connector_credential_vault_on_shutdown",
                ),
                (
                    "connector_oauth_session_store",
                    "close_connector_oauth_session_store_on_shutdown",
                ),
                ("source_catalog", "close_source_catalog_on_shutdown"),
                (
                    "external_acl_sync_store",
                    "close_external_acl_sync_store_on_shutdown",
                ),
                (
                    "audit_export_store",
                    "close_audit_export_store_on_shutdown",
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
    app.state.ha_runtime = ha_runtime
    app.state.ha_document_multiwriter_mode = bool(
        ha_runtime is not None and getattr(ha_runtime, "api_multi_writer_safe", False)
    )
    app.state.ha_connector_backend = (
        getattr(ha_runtime, "backend", None)
        if app.state.ha_document_multiwriter_mode
        else None
    )
    app.state.ha_connector_multiwriter_mode = bool(
        app.state.ha_document_multiwriter_mode
        and app.state.ha_connector_backend is not None
    )
    if (
        app.state.ha_document_multiwriter_mode
        and app.state.ha_connector_backend is not None
    ):
        shared_backend = app.state.ha_connector_backend
        for label, store in (
            ("authentication", auth_store),
            ("resource access", resource_access_store),
            (
                "OIDC flow",
                getattr(oidc_manager, "flow_store", None)
                if oidc_manager is not None
                else None,
            ),
        ):
            if (
                store is not None
                and getattr(store, "backend", None) is not shared_backend
            ):
                raise ValueError(f"HA {label} store must use the runtime backend")
    app.state.ha_identity_multiwriter_mode = bool(
        app.state.ha_document_multiwriter_mode
        and auth_store is not None
        and resource_access_store is not None
    )
    app.state.ha_identity_config_registry = None
    if app.state.ha_identity_multiwriter_mode:
        from cogdoc.ha.identity_config import DistributedIdentityConfigRegistry

        assert auth_store is not None
        scrypt = auth_store.scrypt_params
        oidc_config = (
            getattr(getattr(oidc_manager, "client", None), "config", None)
            if oidc_manager is not None
            else None
        )
        oidc_contract = None
        if oidc_config is not None:
            oidc_secret = getattr(oidc_config, "client_secret", None)
            oidc_contract = {
                "issuer": oidc_config.issuer,
                "client_id": oidc_config.client_id,
                "client_secret_sha256": (
                    hashlib.sha256(str(oidc_secret).encode()).hexdigest()
                    if oidc_secret is not None
                    else None
                ),
                "redirect_uri": oidc_config.redirect_uri,
                "scopes": list(oidc_config.scopes),
                "allowed_endpoint_hosts": list(oidc_config.allowed_endpoint_hosts),
                "allowed_return_urls": list(oidc_config.allowed_return_urls),
                "clock_skew_seconds": oidc_config.clock_skew_seconds,
            }
        scim_contract = [
            {
                "token_fingerprint": access.token_fingerprint,
                "workspace_id": access.workspace_id,
                "issuer": access.issuer,
                "label": access.label,
                "default_role": access.default_role,
                "group_role_map": dict(access.group_role_map),
            }
            for _fingerprint, access in sorted((scim_access_registry or {}).items())
        ]
        identity_contract = {
            "schema": "identity-plane-v1",
            "scrypt": {
                "n": scrypt.n,
                "r": scrypt.r,
                "p": scrypt.p,
                "salt_bytes": scrypt.salt_bytes,
                "dklen": scrypt.dklen,
            },
            "session_ttl_seconds": auth_store.session_ttl_seconds,
            "invite_ttl_seconds": auth_store.invite_ttl_seconds,
            "max_failed_logins": auth_store.max_failed_logins,
            "lockout_seconds": auth_store.lockout_seconds,
            "self_registration_enabled": (
                get_settings().cogdoc_self_registration_enabled
                if self_registration_enabled is None
                else bool(self_registration_enabled)
            ),
            "oidc": oidc_contract,
            "scim": scim_contract,
        }
        identity_registry = DistributedIdentityConfigRegistry(
            app.state.ha_connector_backend
        )
        identity_registry.register(
            "identity-plane-v1",
            get_settings().cogdoc_ha_identity_config_version,
            identity_contract,
        )
        app.state.ha_identity_config_registry = identity_registry
    if app.state.ha_connector_multiwriter_mode and sync_manager is not None:
        raise ValueError(
            "HA connector writers construct their fenced sync manager internally"
        )
    if app.state.ha_connector_multiwriter_mode and (
        getattr(ha_runtime, "api_mutation_coordinator", None) is None
        or getattr(ha_runtime, "source_generations", None) is None
        or getattr(ha_runtime, "connector_commits", None) is None
    ):
        raise ValueError(
            "HA connector writers require mutation, source, and commit authorities"
        )
    source_generations = getattr(ha_runtime, "source_generations", None)
    current_source_manifest = getattr(source_generations, "current_manifest", None)
    app.state.ha_source_manifest_reader = (
        current_source_manifest
        if app.state.ha_document_multiwriter_mode and callable(current_source_manifest)
        else None
    )
    app.state.ha_index_mirror = None
    app.state.auth_store = auth_store
    app.state.oidc_manager = oidc_manager
    app.state.oidc_flow_store = (
        getattr(oidc_manager, "flow_store", None) if oidc_manager is not None else None
    )
    app.state.scim_access_registry = dict(scim_access_registry or {})
    app.state.scim_policies = dict(scim_policies)
    app.state.resource_access_store = resource_access_store

    @app.middleware("http")
    async def restrict_ha_document_writer_surface(request: Request, call_next):
        """Keep local-state APIs unreachable on horizontally scaled writers."""

        if not app.state.ha_document_multiwriter_mode:
            return await call_next(request)
        path = request.url.path.rstrip("/") or "/"
        method = request.method.upper()
        parts = [part for part in path.split("/") if part]
        health = path in {
            "/healthz",
            "/health/live",
            "/readyz",
            "/health/ready",
            "/metrics",
        }
        ha_control = len(parts) >= 2 and parts[:2] == ["v1", "ha"]
        index_job = (
            method == "GET"
            and len(parts) in {2, 3}
            and parts[:2] == ["v1", "index-jobs"]
        )
        global_sync_jobs = method == "GET" and parts == ["v1", "sync-jobs"]
        kb_collection = parts == ["v1", "knowledge-bases"] and method in {
            "GET",
            "POST",
        }
        kb_read = (
            len(parts) == 3
            and parts[:2] == ["v1", "knowledge-bases"]
            and method in {"GET", "DELETE"}
        )
        document_surface = (
            len(parts) >= 4
            and parts[:2] == ["v1", "knowledge-bases"]
            and parts[3] == "documents"
            and method in {"GET", "POST", "DELETE"}
        )
        source_operations_surface = (
            len(parts) >= 4
            and parts[:2] == ["v1", "knowledge-bases"]
            and parts[3] in {"source-catalog", "source-artifacts"}
        )
        connector_surface = (
            len(parts) >= 4
            and parts[:2] == ["v1", "knowledge-bases"]
            and parts[3]
            in {
                "connections",
                "connection-health",
                "sync-jobs",
                "connector-credentials",
                "connector-oauth",
            }
        )
        connector_oauth_callback = (
            len(parts) == 5
            and parts[:3] == ["v1", "auth", "connector-oauth"]
            and parts[3] == "callback"
        )
        identity_surface = (
            parts[:2] == ["v1", "auth"]
            or parts[:2] == ["v1", "workspaces"]
            or parts[:2] == ["scim", "v2"]
            or (
                len(parts) >= 4
                and parts[:2] == ["v1", "knowledge-bases"]
                and "access" in parts[3:]
            )
        )
        research_surface = parts[:2] == ["v1", "research-jobs"]
        chat_surface = parts[:2] in (
            ["v1", "chat"],
            ["v1", "sessions"],
            ["v1", "memory"],
        ) or (
            len(parts) == 2
            and parts[0] == "v1"
            and parts[1] in {"summary", "compare", "retrieve"}
        )
        feedback_surface = parts[:2] in (
            ["v1", "feedback"],
            ["v1", "feedback-analysis"],
            ["v1", "retrieval-feedback"],
            ["v1", "retrieval-eval-drafts"],
        ) or parts[:2] in (["v1", "feedback-loop-metrics"], ["v1", "review-queue"])
        knowledge_surface = parts[:2] == ["v1", "knowledge"]
        if (
            health
            or ha_control
            or index_job
            or (app.state.ha_connector_multiwriter_mode and global_sync_jobs)
            or kb_collection
            or kb_read
            or document_surface
            or source_operations_surface
            or (app.state.ha_identity_multiwriter_mode and identity_surface)
            or (app.state.ha_research_multiwriter_mode and research_surface)
            or (app.state.ha_chat_multiwriter_mode and chat_surface)
            or (app.state.ha_feedback_multiwriter_mode and feedback_surface)
            or (
                app.state.ha_feedback_multiwriter_mode
                and app.state.ha_derived_knowledge_index is not None
                and knowledge_surface
            )
            or (app.state.ha_connector_multiwriter_mode and connector_surface)
            or (app.state.ha_connector_multiwriter_mode and connector_oauth_callback)
        ):
            return await call_next(request)
        error = build_error_response(
            ErrorCode.MODEL_UNAVAILABLE,
            "该 HA 节点仅承载已迁移到共享状态的 API",
        )
        return JSONResponse(status_code=503, content=error.model_dump())

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
    app.state.close_oidc_manager_on_shutdown = False
    app.state.close_oidc_flow_store_on_shutdown = False
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
    app.state.claim_verification_review_store = (
        claim_verification_review_store
        if claim_verification_review_store is not None
        else ClaimVerificationReviewStore(
            retention_days=(
                observation_settings.claim_verification_review_retention_days
            ),
            max_per_tenant=(
                observation_settings.claim_verification_review_max_per_tenant
            ),
        )
    )
    app.state.close_claim_verification_review_store_on_shutdown = False
    # Create operational telemetry before background managers so both HTTP and
    # post-202 research work share one app-local Prometheus registry.
    app.state.metrics = Metrics()
    app.state.research_observer = ResearchObserver(app.state.metrics)
    if webhook_dispatcher is not None:
        app.state.webhook_dispatcher = webhook_dispatcher
    else:
        try:
            app.state.webhook_dispatcher = WebhookDispatcher()
        except ValueError as exc:
            # Notifications are optional. A malformed destination must be
            # observable without taking the API, login, or ingestion paths down.
            log_event(
                "startup",
                "webhook_disabled_invalid_configuration",
                {},
                level=logging.ERROR,
                error_class=type(exc).__name__,
            )
            app.state.webhook_dispatcher = WebhookDispatcher(url="")
    if (
        app.state.ha_document_multiwriter_mode
        and app.state.ha_connector_backend is not None
    ):
        selected_research_store = (
            state_runtime.research_job_store
            if state_runtime is not None
            else research_job_store
        )
        if selected_research_store is not None and (
            getattr(selected_research_store, "backend", None)
            is not app.state.ha_connector_backend
        ):
            raise ValueError("HA research store must use the runtime backend")
        if state_runtime is None and research_job_store is None:
            research_job_store = SqliteResearchJobStore(
                None,
                backend=app.state.ha_connector_backend,
            )
            selected_research_store = research_job_store
    else:
        selected_research_store = (
            state_runtime.research_job_store
            if state_runtime is not None
            else research_job_store
        )
    if (
        app.state.ha_document_multiwriter_mode
        and app.state.ha_connector_backend is not None
        and state_runtime is None
    ):
        if feedback_store is None:
            feedback_store = DistributedFeedbackStore(app.state.ha_connector_backend)
        if feedback_analysis_store is None:
            feedback_analysis_store = DistributedFeedbackAnalysisStore(
                app.state.ha_connector_backend
            )
        if retrieval_feedback_store is None:
            retrieval_feedback_store = DistributedRetrievalFeedbackStore(
                app.state.ha_connector_backend
            )
        if retrieval_eval_draft_store is None:
            retrieval_eval_draft_store = DistributedRetrievalEvalDraftStore(
                app.state.ha_connector_backend
            )
        if knowledge_store is None:
            knowledge_store = DistributedDerivedKnowledgeStore(
                app.state.ha_connector_backend
            )
    app.state.ha_research_multiwriter_mode = bool(
        app.state.ha_document_multiwriter_mode
        and app.state.ha_connector_backend is not None
        and selected_research_store is not None
        and getattr(selected_research_store, "backend", None)
        is app.state.ha_connector_backend
    )
    app.state.ha_research_dispatch_store = None
    if app.state.ha_research_multiwriter_mode:
        from cogdoc.ha.research import ResearchDispatchStore

        app.state.ha_research_dispatch_store = ResearchDispatchStore(
            app.state.ha_connector_backend
        )
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
    app.state.ha_feedback_multiwriter_mode = bool(
        app.state.ha_document_multiwriter_mode
        and app.state.ha_connector_backend is not None
        and getattr(runtime.feedback_store, "backend", None)
        is app.state.ha_connector_backend
        and getattr(runtime.feedback_analysis_store, "backend", None)
        is app.state.ha_connector_backend
        and getattr(runtime.retrieval_feedback_store, "backend", None)
        is app.state.ha_connector_backend
        and getattr(runtime.retrieval_eval_draft_store, "backend", None)
        is app.state.ha_connector_backend
        and getattr(runtime.knowledge_store, "backend", None)
        is app.state.ha_connector_backend
    )
    app.state.close_state_runtime_on_shutdown = (
        state_runtime is None
        and not any(store is not None for store in store_overrides)
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
    resolved_kb_registry: Any = kb_registry or KnowledgeBaseRegistry()
    app.state.kb_registry = resolved_kb_registry
    chat_config = getattr(ha_runtime, "config", None)
    app.state.ha_derived_knowledge_index = None
    if (
        app.state.ha_feedback_multiwriter_mode
        and ha_runtime is not None
        and getattr(ha_runtime, "object_store", None) is not None
    ):
        from cogdoc.ha.derived_knowledge_index import (
            DERIVED_KNOWLEDGE_CHUNK_VERSION,
            HADerivedKnowledgeIndex,
        )

        derived_cache_root = (
            Path(
                str(
                    getattr(chat_config, "index_replica_cache_root", "")
                    or (Path(get_settings().cogdoc_data_dir) / "ha-index-cache")
                )
            )
            / "derived-knowledge"
        )
        app.state.ha_derived_knowledge_index = HADerivedKnowledgeIndex(
            app.state.ha_connector_backend,
            ha_runtime.object_store,
            runtime.knowledge_store,
            resolved_kb_registry,
            worker_id=str(getattr(chat_config, "worker_id", "api-reader")),
            cache_root=derived_cache_root,
            reader_lease_seconds=float(
                getattr(chat_config, "chat_index_reader_lease_seconds", 600.0)
            ),
        )
        runtime.bind_derived_knowledge_index(app.state.ha_derived_knowledge_index)
        maintenance = getattr(ha_runtime, "maintenance", None)
        bind_repository = getattr(maintenance, "bind_index_repository", None)
        if callable(bind_repository):
            bind_repository(
                app.state.ha_derived_knowledge_index.repository,
                chunk_version=DERIVED_KNOWLEDGE_CHUNK_VERSION,
            )
        bind_refresh_recoverer = getattr(
            maintenance, "bind_derived_refresh_recoverer", None
        )
        if callable(bind_refresh_recoverer):

            def schedule_derived_refresh_recovery() -> None:
                app.state.derived_index_executor.submit(
                    app.state.ha_derived_knowledge_index.recover_pending
                )

            bind_refresh_recoverer(schedule_derived_refresh_recovery)
    app.state.ha_auxiliary_retrieval_enabled = bool(
        app.state.ha_derived_knowledge_index is not None
        and app.state.ha_feedback_multiwriter_mode
    )
    if (
        app.state.ha_document_multiwriter_mode
        and app.state.ha_connector_backend is not None
    ):
        if session_store is not None and (
            getattr(session_store, "backend", None)
            is not app.state.ha_connector_backend
        ):
            raise ValueError("HA chat session store must use the runtime backend")
        if session_store is None:
            session_store = DistributedSessionStore(
                app.state.ha_connector_backend,
                max_sessions=int(
                    getattr(chat_config, "chat_max_sessions_per_scope", 1024)
                ),
                ttl_seconds=int(
                    getattr(chat_config, "chat_session_ttl_seconds", 604800)
                ),
                max_display_messages=int(
                    getattr(chat_config, "chat_max_display_messages", 2000)
                ),
                max_session_bytes=int(
                    getattr(chat_config, "chat_max_session_bytes", 4 * 1024 * 1024)
                ),
                memory_policy=get_settings().memory_policy,
            )
    app.state.session_store = session_store or SessionStore()
    bind_knowledge_authority = getattr(
        runtime.knowledge_store, "bind_authority_checker", None
    )
    if callable(bind_knowledge_authority):
        if not isinstance(app.state.session_store, DistributedSessionStore):
            raise ValueError(
                "shared derived knowledge requires a distributed authority checker"
            )
        bind_knowledge_authority(app.state.session_store.check_authority_locked)
    for auxiliary_store in (
        runtime.feedback_store,
        runtime.feedback_analysis_store,
        runtime.retrieval_feedback_store,
        runtime.retrieval_eval_draft_store,
    ):
        bind_auxiliary_authority = getattr(
            auxiliary_store, "bind_authority_checker", None
        )
        if not callable(bind_auxiliary_authority):
            continue
        if not isinstance(app.state.session_store, DistributedSessionStore):
            raise ValueError(
                "shared auxiliary state requires a distributed authority checker"
            )
        bind_auxiliary_authority(app.state.session_store.check_authority_locked)
    app.state.ha_chat_index_provider = ha_index_provider
    if ha_index_provider is not None:
        if getattr(ha_index_provider, "registry", None) is not app.state.kb_registry:
            raise ValueError("HA chat index provider must use the app KB registry")
        runtime_replica = getattr(ha_runtime, "index_replica", None)
        if (
            runtime_replica is not None
            and getattr(ha_index_provider, "replica", None) is not runtime_replica
        ):
            raise ValueError("HA chat index provider must use the runtime replica")
    app.state.ha_chat_session_lease_seconds = float(
        getattr(chat_config, "chat_session_lease_seconds", 300.0)
    )
    app.state.ha_chat_multiwriter_mode = bool(
        app.state.ha_document_multiwriter_mode
        and isinstance(app.state.session_store, DistributedSessionStore)
        and ha_index_provider is not None
        and callable(getattr(ha_index_provider, "pin", None))
    )
    app.state.ha_chat_coordinator = None
    app.state.ha_chat_state_runtime = None
    if app.state.ha_chat_multiwriter_mode:
        from cogdoc.ha.chat_execution import HAChatCoordinator, HAChatStateRuntimeView

        # Only immutable/shared auxiliary state may influence HA answers.
        ha_chat_runtime = HAChatStateRuntimeView(
            runtime,
            shared_auxiliary=app.state.ha_auxiliary_retrieval_enabled,
        )
        app.state.ha_chat_state_runtime = ha_chat_runtime
        if chat_runner is None:
            app.state.chat_runner = partial(
                run_chat_sync,
                state_runtime=ha_chat_runtime,
            )
        if chat_stream_runner is None:
            app.state.chat_stream_runner = partial(
                run_chat,
                state_runtime=ha_chat_runtime,
            )

        app.state.ha_chat_coordinator = HAChatCoordinator(
            app.state.session_store,
            ha_index_provider,
            app.state.kb_registry,
            worker_id=str(getattr(chat_config, "worker_id", "api-reader")),
            session_lease_seconds=app.state.ha_chat_session_lease_seconds,
        )
    # 入库注册表/任务管理器可注入，便于测试用假入库函数。
    from cogdoc.service.index_migration import (
        IndexMigrationManager,
        IndexMigrationRunner,
    )

    app.state.index_migration_manager = IndexMigrationManager(
        IndexMigrationRunner(
            source_dir_for=resolved_kb_registry.source_dir,
            knowledge_store=runtime.knowledge_store,
            refresh_derived_knowledge=runtime.refresh_derived_knowledge_index,
        )
    )
    # 知识库存在性检查用于写入防复活，注入版由测试自行控制。
    if index_jobs is None:
        app.state.index_jobs = IndexJobManager(
            kb_exists=resolved_kb_registry.exists,
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
    if (
        ha_runtime is not None
        and getattr(ha_runtime, "index_generations", None) is not None
        and callable(getattr(ha_runtime, "publish_generation", None))
    ):
        from cogdoc.ha.index_mirror import HAIndexMirror

        app.state.ha_index_mirror = HAIndexMirror(
            ha_runtime,
            app.state.kb_registry,
        )
        bind_after_index_commit = getattr(
            app.state.index_jobs, "bind_after_index_commit", None
        )
        if not callable(bind_after_index_commit):
            raise ValueError(
                "HA index mirroring requires IndexJobManager.bind_after_index_commit"
            )
        bind_after_index_commit(app.state.ha_index_mirror.mirror_result)
    if app.state.ha_connector_multiwriter_mode:
        coordinator = getattr(ha_runtime, "api_mutation_coordinator", None)
        if getattr(
            app.state.index_jobs, "_mutation_coordinator", None
        ) is not coordinator or getattr(
            app.state.index_jobs, "_source_generation_store", None
        ) is not getattr(ha_runtime, "source_generations", None):
            raise ValueError(
                "HA connector writers require the shared fenced index manager"
            )
    if ha_runtime is not None:
        app.state.metrics.bind_ha(ha_runtime)
    from cogdoc.connectors.connection_store import ConnectionStore
    from cogdoc.connectors.credential_store import (
        ACTIVE_KEY_VERSION_ENV,
        MASTER_KEYS_ENV,
        CredentialRevisionConflict,
        CredentialVault,
    )
    from cogdoc.connectors.base import SyncCancelled
    from cogdoc.connectors.factory import build_connector
    from cogdoc.connectors.http_transport import HttpTransport
    from cogdoc.connectors.oauth import (
        MAX_OAUTH_RESPONSE_BYTES,
        AtlassianOAuthAdapter,
        MicrosoftOAuthAdapter,
        NotionOAuthAdapter,
        OAuthCoordinator,
        OAuthProviderAdapter,
        OAuthSessionStore,
    )
    from cogdoc.connectors.manager import SyncManager
    from cogdoc.connectors.materialized_sink import MaterializedSyncSink
    from cogdoc.connectors.sync_runtime import ConnectorSyncRuntime
    from cogdoc.connectors.sync_store import ConnectorSyncStore
    from cogdoc.service.external_acl import (
        ExternalAclSynchronizer,
        ExternalAclSyncStore,
        WorkspaceIdentityResolver,
    )
    from cogdoc.service.source_catalog import SourceCatalog
    from cogdoc.service.connector_observability import ConnectorOperationsObserver
    from cogdoc.service.source_artifact_store import SourceArtifactStore
    from cogdoc.service.kb_epoch import shared_epoch_store
    from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store

    connector_settings = get_settings()
    connector_db_path = connector_settings.state_db_path
    app.state.connector_confluence_allowed_hosts = frozenset(
        host.strip().casefold()
        for host in connector_settings.cogdoc_confluence_allowed_hosts.split(",")
        if host.strip()
    )
    app.state.connector_s3_endpoint_allowed_hosts = frozenset(
        host.strip().casefold()
        for host in connector_settings.cogdoc_s3_endpoint_allowed_hosts.split(",")
        if host.strip()
    )
    app.state.connector_local_allowed_roots = tuple(
        root.strip()
        for root in connector_settings.cogdoc_local_connector_allowed_roots.split(",")
        if root.strip()
    )
    app.state.connector_git_allowed_roots = tuple(
        root.strip()
        for root in connector_settings.cogdoc_git_connector_allowed_roots.split(",")
        if root.strip()
    )
    app.state.connector_url_allowed_hosts = frozenset(
        host.strip().casefold()
        for host in connector_settings.cogdoc_url_connector_allowed_hosts.split(",")
        if host.strip()
    )
    connector_store_options = {
        "max_connections_global": (
            connector_settings.cogdoc_connector_max_connections_global
        ),
        "max_connections_per_tenant": (
            connector_settings.cogdoc_connector_max_connections_per_tenant
        ),
        "max_connections_per_kb": (
            connector_settings.cogdoc_connector_max_connections_per_kb
        ),
    }
    app.state.connection_store = connection_store or (
        ConnectionStore(
            None,
            backend=app.state.ha_connector_backend,
            **connector_store_options,
        )
        if app.state.ha_connector_backend is not None
        else ConnectionStore(connector_db_path, **connector_store_options)
    )
    app.state.connector_sync_store = connector_sync_store or (
        ConnectorSyncStore(None, backend=app.state.ha_connector_backend)
        if app.state.ha_connector_backend is not None
        else ConnectorSyncStore(connector_db_path)
    )
    distributed_catalog = getattr(ha_runtime, "source_catalog", None)
    distributed_artifacts = getattr(ha_runtime, "source_artifact_store", None)
    if app.state.ha_connector_multiwriter_mode and (
        distributed_catalog is None or distributed_artifacts is None
    ):
        raise ValueError("HA multi-writer mode requires shared source stores")
    app.state.source_catalog = (
        source_catalog or distributed_catalog or SourceCatalog(connector_db_path)
    )
    app.state.source_artifact_store = source_artifact_store or distributed_artifacts
    if app.state.source_artifact_store is None:
        app.state.source_artifact_store = SourceArtifactStore(
            connector_settings.source_artifact_dir,
            max_file_bytes=connector_settings.cogdoc_source_artifact_max_file_mb
            * 1024
            * 1024,
            max_bytes_per_tenant=(
                connector_settings.cogdoc_source_artifact_max_tenant_mb * 1024 * 1024
            ),
            # The sink inserts before pruning so one temporary overflow slot is
            # required to retain the configured number of newest versions.
            max_versions_per_source=(
                connector_settings.cogdoc_source_artifact_max_versions + 1
            ),
            user_max_versions_per_source=(
                connector_settings.cogdoc_source_artifact_max_versions
            ),
        )

    def require_connector_backend(name: str, store: Any) -> None:
        if not app.state.ha_connector_multiwriter_mode:
            return
        actual = getattr(store, "backend", None)
        if actual is None:
            actual = getattr(getattr(store, "_conn", None), "backend", None)
        if actual is not app.state.ha_connector_backend:
            raise ValueError(f"HA {name} must use the runtime shared backend")

    require_connector_backend("connection_store", app.state.connection_store)
    require_connector_backend("connector_sync_store", app.state.connector_sync_store)
    require_connector_backend("source_catalog", app.state.source_catalog)
    require_connector_backend("source_artifact_store", app.state.source_artifact_store)
    if connector_credential_vault is not None:
        app.state.connector_credential_vault = connector_credential_vault
    elif connector_settings.cogdoc_connector_vault_keys.strip():
        vault_options = (
            {
                "backend": app.state.ha_connector_backend,
            }
            if app.state.ha_connector_backend is not None
            else {}
        )
        app.state.connector_credential_vault = CredentialVault(
            None if vault_options else connector_db_path,
            env={
                MASTER_KEYS_ENV: connector_settings.cogdoc_connector_vault_keys,
                ACTIVE_KEY_VERSION_ENV: (
                    connector_settings.cogdoc_connector_vault_active_key_id
                ),
            },
            **vault_options,
        )
    else:
        app.state.connector_credential_vault = None
    if app.state.connector_credential_vault is not None:
        require_connector_backend(
            "connector_credential_vault", app.state.connector_credential_vault
        )
    app.state.connector_oauth_session_store = None
    # Cross-store reference changes own separate transactions. Serialize their
    # route-level mutations without blocking the event loop; HA uses a durable
    # lease so the same invariant spans API processes.
    if app.state.ha_connector_multiwriter_mode:
        from cogdoc.ha.connector_lock import DistributedConnectorReferenceLock

        app.state.connector_credential_reference_lock = (
            DistributedConnectorReferenceLock(
                app.state.ha_connector_backend,
                owner_id=str(
                    getattr(getattr(ha_runtime, "config", None), "worker_id", "ha-api")
                ),
                executor_provider=lambda: app.state.offload_executor,
                lease_seconds=max(
                    600.0,
                    float(
                        getattr(
                            getattr(ha_runtime, "config", None),
                            "mutation_lease_seconds",
                            120.0,
                        )
                    ),
                ),
            )
        )
    else:
        app.state.connector_credential_reference_lock = asyncio.Lock()
    app.state.connector_oauth_session_ttl_seconds = (
        connector_settings.cogdoc_connector_oauth_session_ttl_seconds
    )
    app.state.connector_oauth_redirect_uris = dict(connector_oauth_redirect_uris or {})
    if connector_oauth is not None:
        if app.state.connector_credential_vault is None:
            raise ValueError("injected connector_oauth requires a credential vault")
        if (
            getattr(connector_oauth, "credential_vault", None)
            is not app.state.connector_credential_vault
        ):
            raise ValueError(
                "injected connector_oauth must share the app credential vault"
            )
        app.state.connector_oauth = connector_oauth
        app.state.connector_oauth_session_store = getattr(
            connector_oauth, "session_store", None
        )
    elif (
        app.state.connector_credential_vault is not None
        and connector_settings.cogdoc_connector_oauth_public_base_url.strip()
    ):
        oauth_base = (
            connector_settings.cogdoc_connector_oauth_public_base_url.strip().rstrip(
                "/"
            )
        )
        redirect_uris = {
            provider: f"{oauth_base}/v1/auth/connector-oauth/callback/{provider}"
            for provider in ("notion", "atlassian", "microsoft")
        }
        adapters: dict[str, OAuthProviderAdapter] = {}
        oauth_timeout = connector_settings.cogdoc_connector_oauth_timeout_seconds
        if (
            connector_settings.cogdoc_notion_oauth_client_id.strip()
            and connector_settings.cogdoc_notion_oauth_client_secret.strip()
        ):
            adapters["notion"] = NotionOAuthAdapter(
                client_id=connector_settings.cogdoc_notion_oauth_client_id,
                client_secret=connector_settings.cogdoc_notion_oauth_client_secret,
                redirect_uri=redirect_uris["notion"],
                transport=HttpTransport(
                    allowed_hosts={"api.notion.com"},
                    timeout_seconds=oauth_timeout,
                    max_response_bytes=MAX_OAUTH_RESPONSE_BYTES,
                ),
            )
        if (
            connector_settings.cogdoc_atlassian_oauth_client_id.strip()
            and connector_settings.cogdoc_atlassian_oauth_client_secret.strip()
        ):
            adapters["atlassian"] = AtlassianOAuthAdapter(
                client_id=connector_settings.cogdoc_atlassian_oauth_client_id,
                client_secret=(connector_settings.cogdoc_atlassian_oauth_client_secret),
                redirect_uri=redirect_uris["atlassian"],
                scopes=[
                    "read:page:confluence",
                    "read:content-details:confluence",
                    "read:me",
                    "offline_access",
                ],
                transport=HttpTransport(
                    allowed_hosts={"auth.atlassian.com", "api.atlassian.com"},
                    timeout_seconds=oauth_timeout,
                    max_response_bytes=MAX_OAUTH_RESPONSE_BYTES,
                ),
            )
        if connector_settings.cogdoc_microsoft_oauth_client_id.strip():
            adapters["microsoft"] = MicrosoftOAuthAdapter(
                client_id=connector_settings.cogdoc_microsoft_oauth_client_id,
                client_secret=(
                    connector_settings.cogdoc_microsoft_oauth_client_secret or None
                ),
                redirect_uri=redirect_uris["microsoft"],
                scopes=["offline_access", "Files.Read.All", "Sites.Read.All"],
                tenant=connector_settings.cogdoc_microsoft_oauth_tenant,
                transport=HttpTransport(
                    allowed_hosts={"login.microsoftonline.com"},
                    timeout_seconds=oauth_timeout,
                    max_response_bytes=MAX_OAUTH_RESPONSE_BYTES,
                ),
            )
        if adapters:
            oauth_sessions = OAuthSessionStore(
                (
                    None
                    if app.state.ha_connector_backend is not None
                    else connector_db_path
                ),
                app.state.connector_credential_vault,
                backend=app.state.ha_connector_backend,
                epoch_reader=shared_epoch_store().current,
            )
            app.state.connector_oauth_session_store = oauth_sessions
            app.state.connector_oauth_redirect_uris = {
                provider: redirect_uris[provider] for provider in adapters
            }
            app.state.connector_oauth = OAuthCoordinator(
                oauth_sessions,
                app.state.connector_credential_vault,
                adapters,
                connection_reader=lambda connection_id: app.state.connection_store.get(
                    connection_id, include_secret_refs=True
                ),
            )
        else:
            app.state.connector_oauth = None
    else:
        app.state.connector_oauth = None
    if app.state.connector_oauth_session_store is not None:
        require_connector_backend(
            "connector_oauth_session_store",
            app.state.connector_oauth_session_store,
        )
        bind_epoch_reader = getattr(
            app.state.connector_oauth_session_store, "bind_epoch_reader", None
        )
        if callable(bind_epoch_reader):
            bind_epoch_reader(shared_epoch_store().current)
    # ``create_app`` is a reusable application-factory seam: a single app may
    # enter its lifespan repeatedly in embedded deployments and tests. Keep
    # these process-local SQLite handles alive just like the pre-existing
    # observation stores; the owning process releases them on exit.
    app.state.close_connection_store_on_shutdown = False
    app.state.close_connector_sync_store_on_shutdown = False
    app.state.close_source_catalog_on_shutdown = False
    app.state.close_connector_credential_vault_on_shutdown = False
    app.state.close_connector_oauth_session_store_on_shutdown = False
    app.state.external_acl_sync_store = ExternalAclSyncStore(
        None if app.state.ha_connector_multiwriter_mode else connector_db_path,
        backend=(
            app.state.ha_connector_backend
            if app.state.ha_connector_multiwriter_mode
            else None
        ),
    )
    app.state.close_external_acl_sync_store_on_shutdown = False

    class _UnavailableIdentityResolver:
        def resolve(self, tenant_id, grant):
            del tenant_id, grant
            return None

    acl_synchronizer = (
        ExternalAclSynchronizer(
            app.state.resource_access_store,
            (
                WorkspaceIdentityResolver(app.state.auth_store)
                if app.state.auth_store is not None
                else _UnavailableIdentityResolver()
            ),
            app.state.external_acl_sync_store,
        )
        if app.state.resource_access_store is not None
        else None
    )

    def submit_connector_index(kb_id: str):
        coordinator = getattr(ha_runtime, "api_mutation_coordinator", None)
        if app.state.ha_connector_multiwriter_mode:
            current_lease = getattr(coordinator, "current_lease", None)
            lease = current_lease() if callable(current_lease) else None
            if lease is None:
                raise RuntimeError(
                    "HA connector index requires a live KB mutation lease"
                )
            submit_delegated = getattr(
                app.state.index_jobs, "submit_with_mutation_lease", None
            )
            if not callable(submit_delegated):
                raise RuntimeError("HA index manager cannot accept delegated leases")
            return submit_delegated(kb_id, lease)
        return app.state.index_jobs.submit(kb_id)

    def submit_connector_index_idempotent(kb_id: str, idempotency_key: str):
        if not app.state.ha_connector_multiwriter_mode:
            return app.state.index_jobs.submit(kb_id)
        coordinator = getattr(ha_runtime, "api_mutation_coordinator", None)
        current_lease = getattr(coordinator, "current_lease", None)
        lease = current_lease() if callable(current_lease) else None
        if lease is None:
            raise RuntimeError("HA connector index requires a live KB mutation lease")
        return app.state.index_jobs.submit_with_mutation_lease(
            kb_id,
            lease,
            idempotency_key=idempotency_key,
        )

    @contextmanager
    def connector_execution_context(job):
        if not app.state.ha_connector_multiwriter_mode:
            yield
            return
        coordinator = getattr(ha_runtime, "api_mutation_coordinator", None)
        source_store = getattr(ha_runtime, "source_generations", None)
        lease_factory = getattr(coordinator, "lease", None)
        materialize = getattr(source_store, "materialize_current", None)
        if not callable(lease_factory) or not callable(materialize):
            raise RuntimeError("HA connector mutation authority is unavailable")
        storage_id = str(job["kb_id"])
        with lease_factory(storage_id):
            # Rebase the node-local cache from the authoritative source head
            # before any connector delta or commit recovery touches it.
            materialize(storage_id, app.state.kb_registry.source_dir(storage_id))
            yield

    def build_sync_sink(connection):
        return MaterializedSyncSink(
            source_dir=app.state.kb_registry.source_dir(connection["kb_id"]),
            catalog=app.state.source_catalog,
            index_submitter=submit_connector_index,
            index_status_reader=app.state.index_jobs.get,
            keyed_index_submitter=(
                submit_connector_index_idempotent
                if app.state.ha_connector_multiwriter_mode
                else None
            ),
            owner_id=connection["owner_id"],
            workspace_visible=connection["workspace_visible"],
            acl_sync=acl_synchronizer,
            artifact_store=app.state.source_artifact_store,
            artifact_versions_to_keep=(
                connector_settings.cogdoc_source_artifact_max_versions
            ),
            index_timeout_seconds=(
                connector_settings.cogdoc_connector_index_timeout_seconds
            ),
            commit_store=(
                getattr(ha_runtime, "connector_commits", None)
                if app.state.ha_connector_multiwriter_mode
                else None
            ),
            quota_reserver=(
                lambda tenant_id, kb_id, source_dir, baseline_dir, proposed_dir, reservation_key: (
                    app.state.tenant_quota.reserve_connector_snapshot(
                        tenant_id,
                        kb_id,
                        source_dir,
                        baseline_dir,
                        proposed_dir,
                        reservation_key,
                    )
                )
            ),
            quota_releaser=app.state.tenant_quota.release,
            quota_checker=app.state.tenant_quota.assert_live,
        )

    def cleanup_sync_terminal(job) -> None:
        commit_store = getattr(ha_runtime, "connector_commits", None)
        if app.state.ha_connector_multiwriter_mode and commit_store is not None:
            commit_store.finalize(str(job["job_id"]))
        MaterializedSyncSink.cleanup_work(
            source_dir=app.state.kb_registry.source_dir(str(job["kb_id"])),
            connection_id=str(job["connection_id"]),
            job_id=str(job["job_id"]),
        )

    def _cleanup_connector_connection_under_authority(
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        expected_kb_epoch: int,
        authorization_guard: Callable[[], None] | None = None,
    ) -> dict[str, int]:
        """Finalize a fenced connection teardown before dropping its handle."""

        def require_cleanup_authority() -> None:
            assert_active_kb_incarnation(
                app.state.kb_registry,
                tenant_id,
                kb_id,
                expected_kb_epoch,
            )
            if authorization_guard is not None:
                authorization_guard()
            connection = app.state.connection_store.get(
                connection_id, include_secret_refs=True
            )
            if (
                connection is None
                or connection["tenant_id"] != tenant_id
                or connection["kb_id"] != kb_id
            ):
                raise KeyError(connection_id)
            if connection["enabled"] or not connection.get("deleting"):
                raise RuntimeError("connection deletion fence is not active")

        delete_managed = getattr(
            app.state.external_acl_sync_store, "delete_managed", None
        )
        managed_document_ids = getattr(
            app.state.external_acl_sync_store, "managed_document_ids", None
        )
        if not callable(delete_managed) or not callable(managed_document_ids):
            raise RuntimeError("external ACL state store does not support cleanup")
        managed_by = f"connector:{connection_id}"
        acl_document_ids = managed_document_ids(tenant_id, kb_id, managed_by)
        if app.state.resource_access_store is not None:
            retiring_document_ids = getattr(
                app.state.resource_access_store, "retiring_document_ids", None
            )
            if not callable(retiring_document_ids):
                raise RuntimeError("ACL state stores do not support connection cleanup")
            acl_document_ids = tuple(
                dict.fromkeys(
                    (
                        *acl_document_ids,
                        *retiring_document_ids(tenant_id, kb_id, managed_by),
                    )
                )
            )

        def clean_acl_state(
            cleanup_tenant_id: str,
            cleanup_kb_id: str,
            managed_by: str,
            document_ids: tuple[str, ...],
        ) -> None:
            del document_ids
            delete_managed(
                cleanup_tenant_id,
                cleanup_kb_id,
                managed_by,
            )

        require_cleanup_authority()
        cleanup_connection = app.state.connection_store.get(
            connection_id,
            include_secret_refs=True,
        )
        if cleanup_connection is None:
            raise KeyError(connection_id)

        def record_cleanup_index_job(job_id: str) -> None:
            with kb_write_lock(kb_id):
                require_cleanup_authority()
                app.state.connection_store.record_delete_index_job(
                    connection_id,
                    job_id,
                )

        result = MaterializedSyncSink.cleanup_connection(
            source_dir=app.state.kb_registry.source_dir(kb_id),
            tenant_id=tenant_id,
            kb_id=kb_id,
            connection_id=connection_id,
            catalog=app.state.source_catalog,
            index_submitter=lambda cleanup_kb_id: submit_connector_index_idempotent(
                cleanup_kb_id,
                f"connection-delete:{connection_id}:epoch:{expected_kb_epoch}",
            ),
            index_status_reader=app.state.index_jobs.get,
            resource_access_store=app.state.resource_access_store,
            acl_document_ids=acl_document_ids,
            work_job_ids=app.state.connector_sync_store.connection_job_ids(
                tenant_id,
                kb_id,
                connection_id,
            ),
            acl_state_cleaner=clean_acl_state,
            authority_guard=require_cleanup_authority,
            cleanup_index_job_id=cleanup_connection.get("delete_index_job_id"),
            index_job_recorder=record_cleanup_index_job,
            index_timeout_seconds=(
                connector_settings.cogdoc_connector_index_timeout_seconds
            ),
        )
        return result

    def cleanup_connector_connection(
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        expected_kb_epoch: int,
        authorization_guard: Callable[[], None] | None = None,
    ) -> dict[str, int]:
        if not app.state.ha_connector_multiwriter_mode:
            return _cleanup_connector_connection_under_authority(
                tenant_id,
                kb_id,
                connection_id,
                expected_kb_epoch,
                authorization_guard,
            )
        coordinator = getattr(ha_runtime, "api_mutation_coordinator", None)
        source_store = getattr(ha_runtime, "source_generations", None)
        lease_factory = getattr(coordinator, "lease", None)
        materialize = getattr(source_store, "materialize_current", None)
        if not callable(lease_factory) or not callable(materialize):
            raise RuntimeError("HA connector cleanup authority is unavailable")
        with lease_factory(kb_id):
            materialize(kb_id, app.state.kb_registry.source_dir(kb_id))
            return _cleanup_connector_connection_under_authority(
                tenant_id,
                kb_id,
                connection_id,
                expected_kb_epoch,
                authorization_guard,
            )

    def finalize_connector_connection_delete(
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        expected_kb_epoch: int,
        authorization_guard: Callable[[], None] | None = None,
    ) -> dict[str, int]:
        """Atomically retire live sync state and drop the durable retry handle."""

        def require_cleanup_authority() -> None:
            assert_active_kb_incarnation(
                app.state.kb_registry,
                tenant_id,
                kb_id,
                expected_kb_epoch,
            )
            if authorization_guard is not None:
                authorization_guard()
            connection = app.state.connection_store.get(
                connection_id, include_secret_refs=True
            )
            if (
                connection is None
                or connection["tenant_id"] != tenant_id
                or connection["kb_id"] != kb_id
            ):
                raise KeyError(connection_id)
            if connection["enabled"] or not connection.get("deleting"):
                raise RuntimeError("connection deletion fence is not active")

        with kb_write_lock(kb_id):
            require_cleanup_authority()
            if app.state.resource_access_store is not None:
                retiring_document_ids = getattr(
                    app.state.resource_access_store,
                    "retiring_document_ids",
                    None,
                )
                if not callable(retiring_document_ids):
                    raise RuntimeError(
                        "resource access store does not expose retirement state"
                    )
                if retiring_document_ids(
                    tenant_id,
                    kb_id,
                    f"connector:{connection_id}",
                ):
                    raise RuntimeError(
                        "connection document retirement is still in progress"
                    )
            retired_sync_state = app.state.connector_sync_store.retire_connection(
                tenant_id,
                kb_id,
                connection_id,
            )
            if not app.state.connection_store.delete(connection_id):
                raise RuntimeError("connection definition disappeared during cleanup")
        return retired_sync_state

    app.state.connector_connection_cleanup = cleanup_connector_connection
    app.state.connector_connection_delete_finalizer = (
        finalize_connector_connection_delete
    )

    def reconcile_connector_oauth_bindings(*, credential_id: str | None = None) -> int:
        """Rollback crash-interrupted pending OAuth connection bindings."""

        vault = app.state.connector_credential_vault
        if vault is None:
            return 0
        reconciled = 0
        for binding in vault.pending_bindings(limit=10_000):
            if credential_id is not None and binding["credential_id"] != credential_id:
                continue
            connection = app.state.connection_store.get(
                str(binding["connection_id"]),
                include_secret_refs=True,
            )
            lifecycle = binding.get("lifecycle")
            if lifecycle == "active":
                # Activation and journal deletion share one vault transaction;
                # this branch only repairs legacy/manual partial state.
                vault.clear_pending_binding(
                    str(binding["credential_id"]),
                    tenant_id=str(binding["tenant_id"]),
                    kb_id=str(binding["kb_id"]),
                )
                reconciled += 1
                continue
            if (
                connection is not None
                and connection.get("tenant_id") == binding["tenant_id"]
                and connection.get("kb_id") == binding["kb_id"]
                and connection.get("credential_id") == binding["credential_id"]
            ):
                # Preserve unrelated enable/config changes made after the
                # crash, but replace the exact still-pending secret reference
                # under a fresh CAS snapshot.
                app.state.connection_store.restore_secret_reference(
                    str(binding["connection_id"]),
                    credential_id=binding.get("previous_credential_id"),
                    credential_fields=binding.get("previous_credential_fields", ()),
                    secret_env=binding.get("previous_secret_env", {}),
                    expected_revision=int(connection["revision"]),
                )
            vault.clear_pending_binding(
                str(binding["credential_id"]),
                tenant_id=str(binding["tenant_id"]),
                kb_id=str(binding["kb_id"]),
            )
            try:
                vault.delete(
                    str(binding["credential_id"]),
                    tenant_id=str(binding["tenant_id"]),
                    kb_id=str(binding["kb_id"]),
                    connection_id=str(binding["connection_id"]),
                    actor_id="oauth-binding-recovery",
                    expected_revision=int(binding["credential_revision"]),
                )
            except Exception:
                try:
                    vault.quarantine(
                        str(binding["credential_id"]),
                        tenant_id=str(binding["tenant_id"]),
                        kb_id=str(binding["kb_id"]),
                        connection_id=str(binding["connection_id"]),
                        actor_id="oauth-binding-recovery",
                        expected_revision=int(binding["credential_revision"]),
                    )
                except Exception:
                    # The row was pending before any cross-store mutation and
                    # remains unusable even while durable cleanup is retried.
                    pass
            reconciled += 1
        return reconciled

    app.state.reconcile_connector_oauth_bindings = reconcile_connector_oauth_bindings

    def maintain_connector_credentials() -> None:
        vault = app.state.connector_credential_vault
        if vault is None:
            return
        # OAuth callbacks create fail-closed pending envelopes and publish
        # them only after binding. Process-loss remnants and explicitly
        # quarantined rollback failures are safe but must not accumulate or
        # pin obsolete master-key versions forever.
        inactive_cutoff = max(0.0, time.time() - 3_600)
        inactive_remaining = 10_000
        while inactive_remaining:
            batch = min(1000, inactive_remaining)
            deleted = vault.prune_inactive_credentials(
                older_than=inactive_cutoff,
                limit=batch,
            )
            inactive_remaining -= deleted
            if deleted < batch:
                break
        cutoff = max(
            0.0,
            time.time()
            - connector_settings.cogdoc_connector_use_audit_retention_days * 86_400,
        )
        remaining = 10_000
        while remaining:
            batch = min(1000, remaining)
            deleted = vault.prune_use_audit_events(older_than=cutoff, limit=batch)
            remaining -= deleted
            if deleted < batch:
                break

    def resolve_connector_secret(
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        credential_id: str,
        *,
        expected_revision: int | None = None,
    ):
        vault = app.state.connector_credential_vault
        if vault is None:
            raise ValueError("connection credential vault is unavailable")
        metadata = vault.get_metadata(credential_id, tenant_id=tenant_id, kb_id=kb_id)
        if metadata is None:
            raise ValueError("connection credential is unavailable")
        bound_connection = metadata.get("connection_id")
        if bound_connection is not None and bound_connection != connection_id:
            raise ValueError("connection credential scope does not match")
        values = vault.get_for_use(
            credential_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            connection_id=bound_connection,
            actor_id=f"sync:{connection_id}",
            expected_revision=expected_revision,
        )
        raw_expiry = values.get("access_token_expires_at")
        if raw_expiry is None:
            return values
        try:
            expires_at = float(raw_expiry)
        except (TypeError, ValueError) as exc:
            raise ValueError("OAuth credential expiry is invalid") from exc
        if expires_at > time.time() + 60:
            return values
        if not values.get("refresh_token"):
            raise ValueError("OAuth credential has expired and cannot be refreshed")
        coordinator = app.state.connector_oauth
        if coordinator is None:
            raise ValueError("OAuth credential refresh is unavailable")
        from cogdoc.connectors.oauth import OAuthProviderError
        from cogdoc.connectors.base import RetryableConnectorError

        try:
            coordinator.refresh_credential(
                credential_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                connection_id=bound_connection,
                user_id=f"sync:{connection_id}",
                expected_revision=int(metadata["revision"]),
            )
        except CredentialRevisionConflict:
            # Another worker refreshed the shared credential after our metadata
            # read. Resolve the winning encrypted revision below.
            pass
        except OAuthProviderError as exc:
            raise RetryableConnectorError("OAuth credential refresh failed") from exc
        return vault.get_for_use(
            credential_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            connection_id=bound_connection,
            actor_id=f"sync:{connection_id}",
        )

    def submit_connector_webhook(event: str, payload: dict) -> None:
        dispatcher = app.state.webhook_dispatcher
        executor = getattr(app.state, "offload_executor", None)
        if not getattr(dispatcher, "enabled", False) or executor is None:
            return
        try:
            executor.submit(dispatcher.emit, event, payload)
        except RuntimeError:
            return

    def connector_credential_snapshot(connection) -> tuple[str | None, int]:
        credential_id = connection.get("credential_id")
        if credential_id is None:
            return None, 0
        vault = app.state.connector_credential_vault
        if vault is None:
            raise ValueError("connection credential vault is unavailable")
        metadata = vault.get_metadata(
            str(credential_id),
            tenant_id=str(connection["tenant_id"]),
            kb_id=str(connection["kb_id"]),
        )
        if metadata is None:
            raise ValueError("connection credential is unavailable")
        bound_connection = metadata.get("connection_id")
        if bound_connection is not None and bound_connection != connection.get(
            "connection_id"
        ):
            raise ValueError("connection credential scope does not match")
        return str(credential_id), int(metadata["revision"])

    def connector_job_admitted(job, connection) -> bool:
        registry_record = app.state.kb_registry.get_by_storage_id(str(job["kb_id"]))
        if not (
            registry_record is not None
            and str(registry_record.get("tenant_id") or "default") == job["tenant_id"]
            and shared_lifecycle_store().status(str(job["kb_id"])) == LIFECYCLE_ACTIVE
            and connection.get("enabled")
            and connection.get("tenant_id") == job.get("tenant_id")
            and connection.get("kb_id") == job.get("kb_id")
            and connection.get("connector_type") == job.get("connector_type")
            and connection.get("revision") == job.get("connection_revision")
            and job.get("status") in {"pending", "retry_wait", "running"}
            and not job.get("cancel_requested")
        ):
            return False
        if app.state.ha_connector_multiwriter_mode and job.get("connector_type") in {
            "local-directory",
            "git",
        }:
            return False
        try:
            credential_id, revision = connector_credential_snapshot(connection)
        except Exception:
            return False
        return bool(
            (job.get("credential_id") or None) == credential_id
            and int(job.get("credential_revision") or 0) == revision
        )

    app.state.connector_observer = ConnectorOperationsObserver(
        app.state.metrics,
        app.state.source_catalog,
        webhook_submitter=submit_connector_webhook,
        current_job_checker=lambda observation: (
            app.state.connector_sync_store.health_snapshot(
                observation.tenant_id,
                observation.kb_id,
                observation.connection_id,
            ).get("last_job_id")
            == observation.job_id
        ),
    )

    def connector_sync_is_active(job) -> bool:
        connection = app.state.connection_store.get(
            str(job["connection_id"]), include_secret_refs=True
        )
        return bool(connection is not None and connector_job_admitted(job, connection))

    def build_sync_connector(connection):
        expected_revision = int(connection.get("sync_credential_revision") or 0)
        frozen_job_id = str(connection.get("sync_job_id") or "")

        def resolve_frozen_secret(
            tenant_id: str,
            kb_id: str,
            connection_id: str,
            credential_id: str,
        ):
            with kb_write_lock(kb_id):
                current_job = app.state.connector_sync_store.get(frozen_job_id)
                current_connection = app.state.connection_store.get(
                    connection_id, include_secret_refs=True
                )
                if (
                    current_job is None
                    or current_connection is None
                    or not connector_job_admitted(current_job, current_connection)
                ):
                    raise SyncCancelled("connector sync authority has been revoked")
                try:
                    return resolve_connector_secret(
                        tenant_id,
                        kb_id,
                        connection_id,
                        credential_id,
                        expected_revision=(expected_revision or None),
                    )
                except CredentialRevisionConflict as exc:
                    raise SyncCancelled(
                        "connector credential authority has been revoked"
                    ) from exc

        with kb_write_lock(str(connection["kb_id"])):
            current_job = app.state.connector_sync_store.get(frozen_job_id)
            current_connection = app.state.connection_store.get(
                str(connection["connection_id"]), include_secret_refs=True
            )
            if (
                current_job is None
                or current_connection is None
                or not connector_job_admitted(current_job, current_connection)
            ):
                raise SyncCancelled("connector sync authority has been revoked")
            return build_connector(
                connection,
                secret_resolver=resolve_frozen_secret,
                allow_environment_secrets=not app.state.auth_enabled,
                confluence_allowed_hosts=(app.state.connector_confluence_allowed_hosts),
                s3_endpoint_allowed_hosts=(
                    app.state.connector_s3_endpoint_allowed_hosts
                ),
                enforce_local_access_policy=app.state.auth_enabled,
                local_allowed_roots=app.state.connector_local_allowed_roots,
                git_allowed_roots=app.state.connector_git_allowed_roots,
                enforce_url_host_policy=bool(
                    app.state.auth_enabled or app.state.connector_url_allowed_hosts
                ),
                url_allowed_hosts=app.state.connector_url_allowed_hosts,
            )

    if sync_manager is not None:
        if (
            sync_manager.connection_store is not app.state.connection_store
            or sync_manager.sync_store is not app.state.connector_sync_store
            or sync_manager.runtime.store is not app.state.connector_sync_store
        ):
            raise ValueError(
                "injected sync_manager must share the app connector stores"
            )
        app.state.sync_manager = sync_manager
    else:
        app.state.sync_manager = SyncManager(
            app.state.connection_store,
            app.state.connector_sync_store,
            ConnectorSyncRuntime(
                app.state.connector_sync_store,
                observer=app.state.connector_observer,
                continuation_checker=connector_sync_is_active,
            ),
            build_sync_sink,
            connector_builder=build_sync_connector,
            execution_context=connector_execution_context,
        )
    if app.state.ha_connector_multiwriter_mode:
        bind_execution_context = getattr(
            app.state.sync_manager, "bind_execution_context", None
        )
        if not callable(bind_execution_context):
            raise ValueError("HA sync_manager cannot bind distributed execution")
        try:
            bind_execution_context(connector_execution_context)
        except (RuntimeError, TypeError) as exc:
            raise ValueError("HA sync_manager execution binding failed") from exc
    bind_sync_control_plane = getattr(
        app.state.sync_manager, "bind_control_plane", None
    )
    if not callable(bind_sync_control_plane):
        raise ValueError(
            "sync_manager must support trusted control-plane dependency binding"
        )
    try:
        bind_sync_control_plane(
            observer=app.state.connector_observer,
            continuation_checker=connector_sync_is_active,
            credential_snapshotter=connector_credential_snapshot,
            job_admission_checker=connector_job_admitted,
            cleanup_callback=cleanup_sync_terminal,
            maintenance_callback=maintain_connector_credentials,
            terminal_retention_seconds=(
                connector_settings.cogdoc_connector_job_retention_days * 86_400
            ),
        )
    except (RuntimeError, TypeError) as exc:
        raise ValueError("sync_manager control-plane binding failed") from exc
    # 有界线程池限制本地算力并发，缓解高并发下精排/嵌入的坏邻居效应。
    app.state.offload_worker_count = (
        offload_workers or get_settings().cogdoc_offload_workers
    )
    app.state.offload_executor = ThreadPoolExecutor(
        max_workers=app.state.offload_worker_count,
        thread_name_prefix="cogdoc-offload",
    )
    app.state.offload_executor_shutdown = False
    selected_chat_stream_workers = (
        get_settings().cogdoc_chat_stream_workers
        if chat_stream_workers is None
        else chat_stream_workers
    )
    if (
        isinstance(selected_chat_stream_workers, bool)
        or not isinstance(selected_chat_stream_workers, int)
        or not 1 <= selected_chat_stream_workers <= 256
    ):
        raise ValueError("chat_stream_workers must be between 1 and 256")
    app.state.chat_stream_worker_count = selected_chat_stream_workers
    app.state.chat_stream_executor = ThreadPoolExecutor(
        max_workers=selected_chat_stream_workers,
        thread_name_prefix="cogdoc-chat-stream",
    )
    app.state.chat_stream_executor_shutdown = False
    selected_artifact_workers = (
        2 if artifact_io_workers is None else artifact_io_workers
    )
    if (
        isinstance(selected_artifact_workers, bool)
        or not isinstance(selected_artifact_workers, int)
        or selected_artifact_workers <= 0
    ):
        raise ValueError("artifact_io_workers must be a positive integer")
    app.state.source_artifact_worker_count = selected_artifact_workers
    app.state.source_artifact_executor = ThreadPoolExecutor(
        max_workers=selected_artifact_workers,
        thread_name_prefix="cogdoc-artifact-io",
    )
    app.state.source_artifact_executor_shutdown = False
    selected_cleanup_workers = (
        2 if connector_cleanup_workers is None else connector_cleanup_workers
    )
    if (
        isinstance(selected_cleanup_workers, bool)
        or not isinstance(selected_cleanup_workers, int)
        or selected_cleanup_workers <= 0
    ):
        raise ValueError("connector_cleanup_workers must be a positive integer")
    app.state.connector_cleanup_worker_count = selected_cleanup_workers
    app.state.connector_cleanup_executor = ThreadPoolExecutor(
        max_workers=selected_cleanup_workers,
        thread_name_prefix="cogdoc-connector-cleanup",
    )
    app.state.connector_cleanup_executor_shutdown = False
    app.state.derived_index_worker_count = (
        get_settings().cogdoc_derived_knowledge_index_workers
    )
    app.state.derived_index_executor = ThreadPoolExecutor(
        max_workers=app.state.derived_index_worker_count,
        thread_name_prefix="cogdoc-derived-index",
    )
    app.state.derived_index_executor_shutdown = False
    app.state.derived_index_refresh_lock = threading.Lock()
    app.state.derived_index_refresh_pending = {}
    app.state.research_planning_executor = ResearchPlanningRuntime(
        max_workers=get_settings().cogdoc_research_planning_workers,
        max_pending=get_settings().cogdoc_research_planning_max_pending,
        thread_name_prefix="cogdoc-research-planning",
    )

    def ha_research_provenance(storage_id: str) -> dict[str, Any]:
        if not app.state.ha_research_multiwriter_mode:
            raise RuntimeError("HA research provenance is unavailable")
        record = app.state.kb_registry.get_by_storage_id(storage_id)
        if not isinstance(record, Mapping):
            return {}
        tenant_id = str(record.get("tenant_id") or "")
        generation_authority = getattr(ha_runtime, "index_generations", None)
        if generation_authority is None:
            return {}
        generation = generation_authority.current(tenant_id, storage_id)
        if generation is None:
            return {}
        manifest = generation.get("manifest")
        contract = manifest.get("contract") if isinstance(manifest, Mapping) else None
        if not isinstance(contract, Mapping):
            return {}
        sources = app.state.source_catalog.list_sources(tenant_id, storage_id)
        acl_epoch_reader = getattr(app.state.resource_access_store, "acl_epoch", None)
        return {
            "index_generation": str(generation.get("generation_id") or ""),
            "index_build_version": str(generation.get("build_id") or ""),
            "chunk_identity_version": str(contract.get("chunk_version") or ""),
            "kb_epoch": int(record.get("epoch") or 0),
            "acl_epoch": (
                int(acl_epoch_reader(tenant_id, storage_id))
                if callable(acl_epoch_reader)
                else 0
            ),
            "source_versions": [
                {
                    "source": str(source.get("source_id") or ""),
                    "sha256": str(source.get("content_sha256") or ""),
                }
                for source in sources
            ],
        }

    def research_commit_writes_output(
        current: Mapping[str, Any], updated: Mapping[str, Any]
    ) -> bool:
        if current.get("report") != updated.get("report"):
            return True
        if current.get("published_report") != updated.get("published_report"):
            return True
        if current.get("review_history") != updated.get("review_history"):
            return True
        old_sections = current.get("sections") or []
        new_sections = updated.get("sections") or []
        protected_fields = (
            "evidence",
            "content",
            "citation_ledger",
            "claim_audit",
            "coverage_audit",
        )
        for old, new in zip(old_sections, new_sections, strict=False):
            if not isinstance(old, Mapping) or not isinstance(new, Mapping):
                continue
            if old.get("status") != "completed" and new.get("status") == "completed":
                return True
            if any(old.get(field) != new.get(field) for field in protected_fields):
                return True
        return False

    def ha_research_commit_guard(connection, current, updated) -> None:
        if not research_commit_writes_output(current, updated):
            return
        provenance = current.get("evidence_provenance")
        if not isinstance(provenance, Mapping):
            raise ResearchJobStateConflictError(
                "research evidence provenance is unavailable"
            )
        storage_id = str(current.get("kb_id") or "")
        authorization = current.get("authorization")
        tenant_id = (
            str(authorization.get("tenant_id") or "")
            if isinstance(authorization, Mapping)
            else "default"
        )
        lock = " FOR SHARE" if app.state.ha_connector_backend.kind == "postgres" else ""
        kb = connection.execute(
            "SELECT tenant_id,lifecycle,epoch FROM ha_api_knowledge_bases "
            "WHERE storage_id=?" + lock,
            (storage_id,),
        ).fetchone()
        if (
            kb is None
            or str(kb["tenant_id"]) != tenant_id
            or str(kb["lifecycle"]) != "active"
            or int(kb["epoch"]) != int(provenance.get("kb_epoch") or -1)
        ):
            raise ResearchJobStateConflictError(
                "research knowledge-base incarnation is stale"
            )
        head = connection.execute(
            "SELECT current_generation_id FROM ha_index_heads "
            "WHERE tenant_id=? AND kb_id=?" + lock,
            (tenant_id, storage_id),
        ).fetchone()
        if head is None or str(head["current_generation_id"] or "") != str(
            provenance.get("index_generation") or ""
        ):
            raise ResearchJobStateConflictError(
                "research index generation changed before commit"
            )
        if isinstance(authorization, Mapping):
            connection.execute(
                "INSERT INTO resource_access_acl_epochs"
                "(tenant_id,kb_id,epoch,updated_at) VALUES(?,?,0,?) "
                "ON CONFLICT(tenant_id,kb_id) DO NOTHING",
                (tenant_id, storage_id, time.time()),
            )
            acl = connection.execute(
                "SELECT epoch FROM resource_access_acl_epochs "
                "WHERE tenant_id=? AND kb_id=?" + lock,
                (tenant_id, storage_id),
            ).fetchone()
            if acl is None or int(acl[0]) != int(provenance.get("acl_epoch") or 0):
                raise ResearchJobStateConflictError(
                    "research authorization generation changed before commit"
                )
            if authorization.get("auth_kind") == "user_session":
                subject_id = str(authorization.get("created_by") or "")
                session_id = str(authorization.get("session_id") or "")
                membership_id = str(authorization.get("membership_id") or "")
                session = connection.execute(
                    "SELECT active_workspace_id,created_at,last_seen_at,expires_at,"
                    "revoked_at FROM auth_sessions WHERE session_id=? AND user_id=?"
                    + lock,
                    (session_id, subject_id),
                ).fetchone()
                membership = connection.execute(
                    "SELECT member_id,role FROM auth_memberships "
                    "WHERE workspace_id=? AND user_id=?" + lock,
                    (tenant_id, subject_id),
                ).fetchone()
                revoked = connection.execute(
                    "SELECT 1 FROM resource_access_membership_tombstones "
                    "WHERE tenant_id=? AND subject_id=? AND membership_id=?" + lock,
                    (tenant_id, subject_id, membership_id),
                ).fetchone()
                scim_rows = connection.execute(
                    "SELECT active,deleted_at FROM auth_scim_users WHERE user_id=?"
                    + lock,
                    (subject_id,),
                ).fetchall()
                policy = connection.execute(
                    "SELECT idle_timeout_minutes,absolute_timeout_hours "
                    "FROM auth_workspace_session_policies WHERE workspace_id=?" + lock,
                    (tenant_id,),
                ).fetchone()
                now = time.time()
                live_role = str(membership[1]) if membership is not None else ""
                idle_minutes = (
                    None if policy is None or policy[0] is None else int(policy[0])
                )
                absolute_hours = (
                    None if policy is None or policy[1] is None else int(policy[1])
                )
                session_expired = bool(
                    session is None
                    or session[4] is not None
                    or float(session[3]) <= now
                    or str(session[0] or "") != tenant_id
                    or (
                        absolute_hours is not None
                        and float(session[1]) + absolute_hours * 3600 <= now
                    )
                    or (
                        idle_minutes is not None
                        and float(session[2]) + idle_minutes * 60 <= now
                    )
                )
                scim_disabled = bool(scim_rows) and not any(
                    bool(row[0]) and row[1] is None for row in scim_rows
                )
                try:
                    role = Role(live_role)
                except ValueError:
                    role = None
                if (
                    not session_id
                    or not membership_id
                    or session_expired
                    or membership is None
                    or str(membership[0]) != membership_id
                    or revoked is not None
                    or scim_disabled
                    or role is None
                    or Permission.QUERY not in ROLE_PERMISSIONS[role]
                ):
                    raise ResearchJobStateConflictError(
                        "research authorization changed before commit"
                    )

    if app.state.ha_research_multiwriter_mode:
        bind_research_guard = getattr(
            runtime.research_job_store, "bind_commit_authority_guard", None
        )
        if not callable(bind_research_guard):
            raise ValueError("HA research store lacks a commit authority guard")
        bind_research_guard(ha_research_commit_guard)
    ha_research_config = getattr(ha_runtime, "config", None)
    app.state.research_execution_manager = research_execution_manager or (
        ResearchExecutionManager.from_runtime(
            runtime.research_job_store,
            state_runtime=runtime,
            kb_exists=resolved_kb_registry.exists,
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
            dispatch_store=app.state.ha_research_dispatch_store,
            worker_id=(
                str(getattr(ha_research_config, "worker_id", ""))
                if app.state.ha_research_multiwriter_mode
                else ""
            ),
            dispatch_lease_seconds=(
                getattr(ha_research_config, "research_worker_lease_seconds", 120.0)
                if app.state.ha_research_multiwriter_mode
                else 60.0
            ),
            dispatch_poll_seconds=(
                getattr(ha_research_config, "research_worker_poll_seconds", 0.5)
                if app.state.ha_research_multiwriter_mode
                else 0.5
            ),
            index_provenance_reader=(
                ha_research_provenance
                if app.state.ha_research_multiwriter_mode
                else None
            ),
            include_auxiliary_state=not app.state.ha_research_multiwriter_mode,
        )
        if runtime.research_job_store is not None
        else None
    )
    if app.state.ha_research_multiwriter_mode:
        active_research_manager = app.state.research_execution_manager
        if (
            active_research_manager is None
            or getattr(active_research_manager, "store", None)
            is not runtime.research_job_store
            or getattr(active_research_manager, "dispatch_store", None)
            is not app.state.ha_research_dispatch_store
        ):
            raise ValueError(
                "HA research manager must use the shared store and dispatcher"
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
        and (
            not app.state.ha_document_multiwriter_mode
            or app.state.ha_derived_knowledge_index is not None
        )
    )
    app.state.derived_knowledge_index_refresher = derived_knowledge_index_refresher or (
        app.state.ha_derived_knowledge_index.refresh_pending
        if app.state.ha_derived_knowledge_index is not None
        else runtime.refresh_derived_knowledge_index
    )
    app.state.derived_knowledge_index_statuser = (
        derived_knowledge_index_statuser or runtime.derived_knowledge_index_status
    )
    resolved_derived_index_clearer = derived_knowledge_index_clearer
    if resolved_derived_index_clearer is None:
        if app.state.ha_derived_knowledge_index is not None:
            resolved_derived_index_clearer = (
                app.state.ha_derived_knowledge_index.clear_kb
            )
        elif app.state.ha_document_multiwriter_mode:

            def clear_absent_ha_derived_index(_storage_id: str) -> None:
                """No derived index exists in this HA deployment."""

            resolved_derived_index_clearer = clear_absent_ha_derived_index
        else:
            resolved_derived_index_clearer = runtime.clear_derived_knowledge_index

    app.state.derived_knowledge_index_clearer = resolved_derived_index_clearer
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
    # Independent review credentials authenticate as the least-privileged
    # built-in reviewer role. They can query evidence and review/publish eval
    # artifacts, but cannot mutate documents, delete KBs, or manage access.
    for raw_key in resolved_review_keys:
        fingerprint = fingerprint_api_key(raw_key)
        resolved_principals.setdefault(
            raw_key,
            Principal(
                tenant_id="default",
                subject_id=f"eval-review:{fingerprint.removeprefix('sha256:')[:16]}",
                role=Role.REVIEWER,
                key_fingerprint=fingerprint,
            ),
        )
    app.state.explicit_principal_fingerprints = {
        principal.key_fingerprint for principal in resolved_principals.values()
    }
    resolved_limiter = rate_limiter or build_rate_limiter(
        settings.rate_limit_per_minute, settings.rate_limit_burst
    )
    app.state.auth_enabled = bool(
        resolved_keys or resolved_principals or app.state.auth_store is not None
    )
    oauth_principals = {
        principal.key_fingerprint: principal
        for principal in resolved_principals.values()
    }
    for raw_key in resolved_keys:
        fingerprint = fingerprint_api_key(raw_key)
        oauth_principals.setdefault(
            fingerprint,
            Principal(
                tenant_id="default",
                subject_id=f"api-key:{fingerprint}",
                role=Role.ADMIN,
                key_fingerprint=fingerprint,
            ),
        )

    def oauth_authorization_is_current(session) -> bool:
        record = app.state.kb_registry.get_by_storage_id(str(session.kb_id))
        if (
            record is None
            or str(record.get("tenant_id") or "default") != session.tenant_id
            or shared_lifecycle_store().status(str(session.kb_id)) != LIFECYCLE_ACTIVE
        ):
            return False
        if session.connection_id is not None:
            connection = app.state.connection_store.get(str(session.connection_id))
            if (
                connection is None
                or connection["tenant_id"] != session.tenant_id
                or connection["kb_id"] != session.kb_id
                or not isinstance(session.connection_revision, int)
                or isinstance(session.connection_revision, bool)
                or int(connection["revision"]) != session.connection_revision
            ):
                return False
        if not app.state.auth_enabled:
            return True
        principal: Principal | None = None
        if session.membership_id is not None:
            if app.state.auth_store is None:
                return False
            membership = app.state.auth_store.membership(
                str(session.tenant_id), str(session.user_id)
            )
            if not isinstance(membership, Mapping):
                return False
            live_membership_id = str(
                membership.get("member_id") or membership.get("membership_id") or ""
            )
            if live_membership_id != session.membership_id:
                return False
            try:
                principal = Principal(
                    tenant_id=str(session.tenant_id),
                    subject_id=str(session.user_id),
                    role=Role(str(membership.get("role") or "")),
                    key_fingerprint=(
                        str(session.principal_fingerprint)
                        if session.principal_fingerprint is not None
                        else f"oauth-membership:{live_membership_id}"
                    ),
                    membership_id=live_membership_id,
                )
            except (TypeError, ValueError):
                return False
        elif session.principal_fingerprint is not None:
            principal = oauth_principals.get(str(session.principal_fingerprint))
            if (
                principal is None
                or principal.tenant_id != session.tenant_id
                or principal.subject_id != session.user_id
            ):
                return False
        if principal is None or not principal.allows(Permission.MANAGE_ACCESS):
            return False
        access_store = app.state.resource_access_store
        if access_store is None:
            return app.state.auth_store is None
        decision = access_store.allowed_sources(
            principal,
            str(session.kb_id),
            tenant_id=str(session.tenant_id),
            permission=Permission.MANAGE_ACCESS,
        )
        return bool(getattr(decision, "is_allowed", False))

    if app.state.connector_oauth is not None:
        bind_oauth_authorization = getattr(
            app.state.connector_oauth, "bind_authorization_checker", None
        )
        if not callable(bind_oauth_authorization):
            raise ValueError(
                "connector_oauth must support live authorization revalidation"
            )
        bind_oauth_authorization(oauth_authorization_is_current)
        app.state.connector_oauth_authorization_checker = oauth_authorization_is_current
    else:
        app.state.connector_oauth_authorization_checker = None
    tenant_quota_policy = TenantQuotaPolicy(
        max_knowledge_bases=settings.cogdoc_tenant_max_knowledge_bases,
        max_documents=settings.cogdoc_tenant_max_documents,
        max_storage_bytes=settings.cogdoc_tenant_max_storage_mb * 1024 * 1024,
    )
    distributed_quota = getattr(ha_runtime, "tenant_quota_manager", None)
    if app.state.ha_document_multiwriter_mode and distributed_quota is not None:
        if getattr(distributed_quota, "policy", None) != tenant_quota_policy:
            raise ValueError(
                "HA tenant quota policy does not match application settings"
            )
        app.state.tenant_quota = distributed_quota
    elif app.state.ha_document_multiwriter_mode and any(
        tenant_quota_policy.public_limits().values()
    ):
        raise ValueError(
            "HA document multi-writer mode requires the distributed tenant quota plane"
        )
    else:
        app.state.tenant_quota = TenantQuotaManager(
            app.state.kb_registry,
            tenant_quota_policy,
        )

    def cleanup_ha_knowledge_base_control_plane(
        tenant_id: str, storage_id: str
    ) -> None:
        """Idempotently erase every shared capability behind a fenced HA KB."""

        # The distributed KB lifecycle/epoch fence is committed before this
        # function is entered.  Draining first prevents a connector with an
        # already-decrypted secret from publishing after credentials are gone.
        app.state.sync_manager.prepare_knowledge_base_delete(
            tenant_id,
            storage_id,
            timeout_seconds=30.0,
        )
        oauth_sessions = app.state.connector_oauth_session_store
        if oauth_sessions is not None:
            oauth_sessions.delete_scope(tenant_id, storage_id)
        app.state.sync_manager.purge_knowledge_base(tenant_id, storage_id)

        # Research dispatch rows are deleted by the shared research store's
        # clear_kb transaction. Shared auxiliary ledgers are cleared behind the
        # committed lifecycle/epoch fence so a same-slug recreation cannot
        # inherit feedback, tuning, review drafts, or approved knowledge.
        for store in (
            app.state.knowledge_store,
            app.state.feedback_store,
            app.state.feedback_analysis_store,
            app.state.retrieval_feedback_store,
            app.state.retrieval_eval_draft_store,
            app.state.research_job_store,
        ):
            clear_kb = getattr(store, "clear_kb", None)
            if callable(clear_kb):
                clear_kb(storage_id)

        app.state.derived_knowledge_index_clearer(storage_id)

        claim_reviews = app.state.claim_verification_review_store
        clear_claim_reviews = getattr(claim_reviews, "clear_kb", None)
        if not callable(clear_claim_reviews):
            raise RuntimeError(
                "claim verification review store does not support KB cleanup"
            )
        clear_claim_reviews(tenant_id, storage_id)
        app.state.index_jobs.clear_kb(storage_id)

        app.state.external_acl_sync_store.delete_scope(tenant_id, storage_id)
        vault = app.state.connector_credential_vault
        if vault is not None:
            vault.delete_scope(tenant_id, storage_id)
        access = app.state.resource_access_store
        if access is not None:
            # ACL is deliberately last: until every upstream capability is
            # gone, the deletion remains operable and fail-closed.
            access.clear_kb(tenant_id, storage_id)

    if app.state.ha_document_multiwriter_mode:
        deletion_coordinator = getattr(ha_runtime, "api_kb_deletion", None)
        bind_kb_cleanup = getattr(
            deletion_coordinator, "bind_control_plane_cleanup", None
        )
        if callable(bind_kb_cleanup):
            bind_kb_cleanup(cleanup_ha_knowledge_base_control_plane)

    # ``create_app`` is also the test/application-factory seam.  Audit is
    # opt-in there so independent in-process apps never append into one shared
    # production file; the module-level production app injects the durable
    # store explicitly below.
    app.state.audit_store = audit_store
    app.state.audit_export_manager = audit_export_manager
    app.state.audit_export_store = (
        audit_export_manager.store if audit_export_manager is not None else None
    )
    app.state.close_audit_export_store_on_shutdown = False

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
        oidc = getattr(app.state, "oidc_manager", None)
        return {
            "schema_version": "v1",
            "account_auth_enabled": app.state.account_auth_enabled,
            "self_registration_enabled": app.state.self_registration_enabled,
            "ha_enabled": getattr(app.state, "ha_runtime", None) is not None,
            "oidc_enabled": oidc is not None,
            "oidc_display_name": (
                str(getattr(oidc, "display_name", "Enterprise SSO"))
                if oidc is not None
                else ""
            ),
            "scim_enabled": bool(app.state.scim_access_registry),
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
        trusted_proxy_cidrs=get_settings().cogdoc_trusted_proxy_cidrs,
    )
    # 指标中间件在访问控制外层（后加=最外层），故 401/429 也被计入请求统计。
    app.add_middleware(MetricsMiddleware, metrics=app.state.metrics)

    # 处理未预期异常。
    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return _unhandled_error_response(exc)

    app.include_router(chat_router)
    app.include_router(claim_verification_router)
    app.include_router(connector_credentials_router)
    app.include_router(connector_oauth_router)
    app.include_router(connections_router)
    app.include_router(auth_router)
    app.include_router(access_router)
    app.include_router(audit_exports_router)
    app.include_router(agent_router)
    app.include_router(health_router)
    app.include_router(ha_operations_router)
    app.include_router(index_migrations_router)
    app.include_router(documents_router)
    app.include_router(feedback_router)
    app.include_router(knowledge_router)
    app.include_router(oidc_router)
    app.include_router(scim_router)
    app.include_router(service_accounts_router)
    app.include_router(service_account_policy_router)
    app.include_router(retrieval_eval_drafts_router)
    app.include_router(retrieval_diagnostics_router)
    app.include_router(research_router)
    app.include_router(source_operations_router)
    app.include_router(tasks_router)
    app.include_router(traces_router)
    return app


# 生产入口会话与入库任务落盘，进程重启不丢，默认创建仍便于测试隔离。
_settings = get_settings()
_db_path = _settings.state_db_path
_kb_registry: KnowledgeBaseRegistry | DistributedKnowledgeBaseRegistry = (
    KnowledgeBaseRegistry()
)
_kb_epoch_reader: Callable[[str], int] | None = None
_kb_lifecycle_reader: Callable[[str], str] | None = None
_state_runtime = StateRuntime.from_settings(_settings)
_ha_runtime = None
_distributed_mutation_coordinator = None
if _settings.cogdoc_ha_enabled:
    from cogdoc.ha.outbox import WebhookOutboxHandler
    from cogdoc.ha.runtime import HAConfig, HARuntime

    _ha_outbox_handler = None
    if _settings.cogdoc_webhook_url.strip():
        try:
            validate_webhook_url(_settings.cogdoc_webhook_url)
            _ha_outbox_handler = WebhookOutboxHandler(
                _settings.cogdoc_webhook_url,
                secret=_settings.cogdoc_webhook_secret,
                timeout_seconds=_settings.cogdoc_webhook_timeout_seconds,
                allow_private_hosts=_settings.cogdoc_webhook_allow_private_hosts,
                max_response_bytes=_settings.cogdoc_webhook_max_response_bytes,
                max_redirects=_settings.cogdoc_webhook_max_redirects,
            )
        except ValueError as exc:
            log_event(
                "startup",
                "ha_webhook_disabled_invalid_configuration",
                {},
                level=logging.ERROR,
                error_class=type(exc).__name__,
            )
    _ha_runtime = HARuntime(
        HAConfig.from_settings(_settings), outbox_handler=_ha_outbox_handler
    )
_ha_identity_backend = (
    _ha_runtime.backend
    if _ha_runtime is not None and _ha_runtime.config.api_multi_writer_enabled
    else None
)
_auth_store = (
    AuthStore(
        None if _ha_identity_backend is not None else _db_path,
        session_ttl_seconds=_settings.cogdoc_auth_session_ttl_seconds,
        invite_ttl_seconds=_settings.cogdoc_auth_invite_ttl_seconds,
        max_failed_logins=_settings.cogdoc_auth_max_failed_logins,
        lockout_seconds=_settings.cogdoc_auth_lockout_seconds,
        backend=_ha_identity_backend,
    )
    if _settings.cogdoc_account_auth_enabled
    else None
)
_oidc_flow_store = None
_oidc_manager = None
if _settings.cogdoc_oidc_enabled:
    if _auth_store is None:
        raise RuntimeError("COGDOC_OIDC_ENABLED requires COGDOC_ACCOUNT_AUTH_ENABLED")
    if not _settings.cogdoc_oidc_flow_key.strip():
        raise RuntimeError(
            "COGDOC_OIDC_ENABLED requires COGDOC_OIDC_FLOW_KEY to contain "
            "one base64url-encoded 32-byte key"
        )
    _oidc_config = OIDCProviderConfig(
        issuer=_settings.cogdoc_oidc_issuer,
        client_id=_settings.cogdoc_oidc_client_id,
        client_secret=_settings.cogdoc_oidc_client_secret or None,
        redirect_uri=_settings.cogdoc_oidc_redirect_uri,
        display_name=_settings.cogdoc_oidc_display_name,
        scopes=tuple(
            value.strip()
            for value in _settings.cogdoc_oidc_scopes.split(",")
            if value.strip()
        ),
        allowed_endpoint_hosts=tuple(
            value.strip()
            for value in _settings.cogdoc_oidc_allowed_endpoint_hosts.split(",")
            if value.strip()
        ),
        allowed_return_urls=tuple(
            value.strip()
            for value in _settings.cogdoc_oidc_allowed_return_urls.split(",")
            if value.strip()
        ),
        timeout_seconds=_settings.cogdoc_oidc_timeout_seconds,
        clock_skew_seconds=_settings.cogdoc_oidc_clock_skew_seconds,
    ).validated()
    _oidc_flow_store = OIDCFlowStore(
        None if _ha_identity_backend is not None else _db_path,
        _settings.cogdoc_oidc_flow_key,
        flow_ttl_seconds=_settings.cogdoc_oidc_flow_ttl_seconds,
        result_ttl_seconds=_settings.cogdoc_oidc_handoff_ttl_seconds,
        backend=_ha_identity_backend,
    )
    _oidc_manager = OIDCManager(
        OIDCClient(_oidc_config),
        _oidc_flow_store,
        _auth_store,
        jit_provisioning_enabled=(_settings.cogdoc_oidc_jit_provisioning_enabled),
        allow_verified_email_link=(_settings.cogdoc_oidc_allow_verified_email_link),
    )
_scim_access_registry = {}
if _settings.cogdoc_scim_enabled:
    if _oidc_manager is None or _auth_store is None:
        raise RuntimeError("COGDOC_SCIM_ENABLED requires account auth and OIDC")
    _scim_access_registry = parse_scim_access_registry(
        _settings.cogdoc_scim_bearer_tokens,
        issuer=_settings.cogdoc_oidc_issuer,
        default_role=_settings.cogdoc_scim_default_role,
        group_role_map=_settings.cogdoc_scim_group_role_map,
    )
_resource_access_store = (
    ResourceAccessStore(
        None if _ha_identity_backend is not None else _db_path,
        legacy_workspace_default=False,
        backend=_ha_identity_backend,
    )
    if _auth_store is not None
    else None
)
_claim_verification_observation_store = SqliteClaimVerificationObservationStore(
    _db_path,
    retention_days=_settings.claim_verification_observation_retention_days,
    max_per_tenant=_settings.claim_verification_observation_max_per_tenant,
)
_claim_verification_review_store = SqliteClaimVerificationReviewStore(
    _db_path,
    retention_days=_settings.claim_verification_review_retention_days,
    max_per_tenant=_settings.claim_verification_review_max_per_tenant,
)
_audit_store = AuditStore(_settings.audit_log_path)
_audit_export_store = AuditExportStore(
    _db_path,
    _settings.audit_export_dir,
)
_audit_export_manager = AuditExportManager(_audit_export_store, _audit_store)
_index_job_store: Any = SqliteJobStore(_db_path, reconcile_on_init=False)
if _ha_runtime is not None:
    if _ha_runtime.config.api_multi_writer_enabled:
        from cogdoc.ha.api_state import (
            DistributedIndexJobStore,
            DistributedMutationCoordinator,
        )
        from cogdoc.service.kb_epoch import bind_shared_epoch_store
        from cogdoc.service.kb_lifecycle import bind_shared_lifecycle_store

        _kb_registry = DistributedKnowledgeBaseRegistry(
            _ha_runtime.backend,
            _ha_runtime.config.source_cache_root,
        )
        _kb_epoch_reader = _kb_registry.current
        _kb_lifecycle_reader = _kb_registry.status
        _distributed_mutation_coordinator = DistributedMutationCoordinator(
            _ha_runtime.backend,
            _kb_registry,
            owner_id=_ha_runtime.config.worker_id,
            lease_seconds=_ha_runtime.config.mutation_lease_seconds,
        )
        _index_job_store = DistributedIndexJobStore(
            _ha_runtime.backend,
            owner_id=_ha_runtime.config.worker_id,
            lease_seconds=_ha_runtime.config.mutation_lease_seconds,
        )
        bind_shared_epoch_store(_kb_registry, replace_local=True)
        bind_shared_lifecycle_store(_kb_registry, replace_local=True)
        _ha_runtime.api_mutation_coordinator = _distributed_mutation_coordinator
        _ha_runtime.bind_api_cache_invalidation(_kb_registry)
        _ha_runtime.bind_api_kb_deletion(_kb_registry)
        _ha_runtime.bind_api_tenant_quota(
            TenantQuotaPolicy(
                max_knowledge_bases=_settings.cogdoc_tenant_max_knowledge_bases,
                max_documents=_settings.cogdoc_tenant_max_documents,
                max_storage_bytes=(
                    _settings.cogdoc_tenant_max_storage_mb * 1024 * 1024
                ),
            )
        )
_ha_index_provider = None
if _ha_runtime is not None and _ha_runtime.index_replica is not None:
    from cogdoc.ha.index_replica import RegistryIndexProvider

    _ha_index_provider = RegistryIndexProvider(
        _ha_runtime.index_replica,
        _kb_registry,
        worker_id=_ha_runtime.config.worker_id,
        reader_lease_seconds=_ha_runtime.config.chat_index_reader_lease_seconds,
    )
app = create_app(
    state_runtime=_state_runtime,
    # The ASGI application object may be entered more than once by embedded
    # servers and lifecycle probes. StateRuntime has no safe reopen operation,
    # so the process-owned production singleton stays alive across lifespan
    # cycles and is released by normal process teardown.
    close_state_runtime_on_shutdown=False,
    session_store=(
        None
        if _distributed_mutation_coordinator is not None
        else SqliteSessionStore(_db_path, memory_policy=_settings.memory_policy)
    ),
    ha_index_provider=_ha_index_provider,
    kb_registry=_kb_registry,
    index_jobs=IndexJobManager(
        job_store=_index_job_store,
        kb_exists=_kb_registry.exists,
        knowledge_store=_state_runtime.knowledge_store,
        epoch_reader=_kb_epoch_reader,
        lifecycle_reader=_kb_lifecycle_reader,
        mutation_coordinator=_distributed_mutation_coordinator,
        source_generation_store=(
            _ha_runtime.source_generations
            if _distributed_mutation_coordinator is not None and _ha_runtime is not None
            else None
        ),
    ),
    source_catalog=(
        _ha_runtime.source_catalog
        if _distributed_mutation_coordinator is not None and _ha_runtime is not None
        else None
    ),
    source_artifact_store=(
        _ha_runtime.source_artifact_store
        if _distributed_mutation_coordinator is not None and _ha_runtime is not None
        else None
    ),
    audit_store=_audit_store,
    audit_export_manager=_audit_export_manager,
    auth_store=_auth_store,
    oidc_manager=_oidc_manager,
    scim_access_registry=_scim_access_registry,
    resource_access_store=_resource_access_store,
    claim_verification_observation_store=_claim_verification_observation_store,
    claim_verification_review_store=_claim_verification_review_store,
    self_registration_enabled=_settings.cogdoc_self_registration_enabled,
    ha_runtime=_ha_runtime,
)
if _ha_index_provider is not None:
    from cogdoc.service.retriever_factory import RetrieverFactory

    RetrieverFactory.bind_external_provider(_ha_index_provider)
app.state.close_auth_store_on_shutdown = _auth_store is not None
app.state.close_oidc_manager_on_shutdown = _oidc_manager is not None
app.state.close_oidc_flow_store_on_shutdown = _oidc_flow_store is not None
app.state.close_resource_access_store_on_shutdown = _resource_access_store is not None
app.state.close_claim_verification_observation_store_on_shutdown = True
app.state.close_claim_verification_review_store_on_shutdown = True
app.state.close_audit_export_store_on_shutdown = True
