import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
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
from cogdoc.api.research_job_store import ResearchJobStore
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
    traces_router,
)
from cogdoc.api.schemas import ErrorCode, build_error_response
from cogdoc.api.session_store import SessionStore
from cogdoc.api.tenant_quota import TenantQuotaManager, TenantQuotaPolicy
from cogdoc.api.tenancy import Permission, Principal, Role, fingerprint_api_key
from cogdoc.api.webhooks import WebhookDispatcher
from cogdoc.agents.research_planner import propose_research_plan
import logging
from cogdoc.config.settings import get_settings
from cogdoc.observability.logger import configure_logging, log_event
from cogdoc.service.chat_service import ChatResult, run_chat, run_chat_sync
from cogdoc.service.ingest_service import cancel_all_timers, drain_purge_queue
from cogdoc.service.kb_locks import kb_write_lock
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
    audit_export_manager: AuditExportManager | None = None,
    auth_store: AuthStore | None = None,
    oidc_manager: Any | None = None,
    scim_access_registry: Mapping[str, Any] | None = None,
    resource_access_store: ResourceAccessStore | None = None,
    claim_verification_observation_store: Any | None = None,
    claim_verification_review_store: Any | None = None,
    self_registration_enabled: bool | None = None,
    offload_workers: int | None = None,
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
            app.state.sync_manager.recover()
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
    app.state.auth_store = auth_store
    app.state.oidc_manager = oidc_manager
    app.state.oidc_flow_store = (
        getattr(oidc_manager, "flow_store", None) if oidc_manager is not None else None
    )
    app.state.scim_access_registry = dict(scim_access_registry or {})
    app.state.scim_policies = dict(scim_policies)
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
    app.state.webhook_dispatcher = webhook_dispatcher or WebhookDispatcher()
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
    app.state.session_store = session_store or SessionStore()
    # 入库注册表/任务管理器可注入，便于测试用假入库函数。
    app.state.kb_registry = kb_registry or KnowledgeBaseRegistry()
    from cogdoc.service.index_migration import (
        IndexMigrationManager,
        IndexMigrationRunner,
    )

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
    app.state.connection_store = connection_store or ConnectionStore(
        connector_db_path,
        max_connections_global=(
            connector_settings.cogdoc_connector_max_connections_global
        ),
        max_connections_per_tenant=(
            connector_settings.cogdoc_connector_max_connections_per_tenant
        ),
        max_connections_per_kb=(
            connector_settings.cogdoc_connector_max_connections_per_kb
        ),
    )
    app.state.connector_sync_store = connector_sync_store or ConnectorSyncStore(
        connector_db_path
    )
    app.state.source_catalog = source_catalog or SourceCatalog(connector_db_path)
    app.state.source_artifact_store = source_artifact_store or SourceArtifactStore(
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
    if connector_credential_vault is not None:
        app.state.connector_credential_vault = connector_credential_vault
    elif connector_settings.cogdoc_connector_vault_keys.strip():
        app.state.connector_credential_vault = CredentialVault(
            connector_db_path,
            env={
                MASTER_KEYS_ENV: connector_settings.cogdoc_connector_vault_keys,
                ACTIVE_KEY_VERSION_ENV: (
                    connector_settings.cogdoc_connector_vault_active_key_id
                ),
            },
        )
    else:
        app.state.connector_credential_vault = None
    app.state.connector_oauth_session_store = None
    # Cross-store reference changes cannot share a SQLite transaction because
    # the vault and connection store own separate connections. Serialize their
    # route-level mutations without blocking the event loop.
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
                connector_db_path,
                app.state.connector_credential_vault,
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
    app.state.external_acl_sync_store = ExternalAclSyncStore(connector_db_path)
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

    def build_sync_sink(connection):
        return MaterializedSyncSink(
            source_dir=app.state.kb_registry.source_dir(connection["kb_id"]),
            catalog=app.state.source_catalog,
            index_submitter=app.state.index_jobs.submit,
            index_status_reader=app.state.index_jobs.get,
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
        )

    def cleanup_sync_terminal(job) -> None:
        MaterializedSyncSink.cleanup_work(
            source_dir=app.state.kb_registry.source_dir(str(job["kb_id"])),
            connection_id=str(job["connection_id"]),
            job_id=str(job["job_id"]),
        )

    def cleanup_connector_connection(
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
            index_submitter=app.state.index_jobs.submit,
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
                enforce_url_host_policy=app.state.auth_enabled,
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
        )
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
        _db_path,
        _settings.cogdoc_oidc_flow_key,
        flow_ttl_seconds=_settings.cogdoc_oidc_flow_ttl_seconds,
        result_ttl_seconds=_settings.cogdoc_oidc_handoff_ttl_seconds,
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
    ResourceAccessStore(_db_path, legacy_workspace_default=False)
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
app = create_app(
    state_runtime=_state_runtime,
    # The ASGI application object may be entered more than once by embedded
    # servers and lifecycle probes. StateRuntime has no safe reopen operation,
    # so the process-owned production singleton stays alive across lifespan
    # cycles and is released by normal process teardown.
    close_state_runtime_on_shutdown=False,
    session_store=SqliteSessionStore(_db_path, memory_policy=_settings.memory_policy),
    kb_registry=_kb_registry,
    index_jobs=IndexJobManager(
        job_store=SqliteJobStore(_db_path, reconcile_on_init=False),
        kb_exists=_kb_registry.exists,
        knowledge_store=_state_runtime.knowledge_store,
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
)
app.state.close_auth_store_on_shutdown = _auth_store is not None
app.state.close_oidc_manager_on_shutdown = _oidc_manager is not None
app.state.close_oidc_flow_store_on_shutdown = _oidc_flow_store is not None
app.state.close_resource_access_store_on_shutdown = _resource_access_store is not None
app.state.close_claim_verification_observation_store_on_shutdown = True
app.state.close_claim_verification_review_store_on_shutdown = True
app.state.close_audit_export_store_on_shutdown = True
