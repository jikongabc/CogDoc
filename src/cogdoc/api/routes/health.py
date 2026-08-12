from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from cogdoc.api.metrics import CONTENT_TYPE_LATEST
from cogdoc.config.settings import get_settings
from cogdoc.tools.ocr import OcrConfig, probe_ocr_dependency
from cogdoc.tools.rust_core_loader import REQUIRED_NATIVE_SYMBOLS, ensure_rust_core

router = APIRouter(tags=["health"])


def _component(status: str, *, required: bool = True) -> dict[str, Any]:
    return {"status": status, "required": required}


def _ocr_readiness_component() -> dict[str, Any]:
    config = OcrConfig.from_settings(get_settings())
    dependency = probe_ocr_dependency(config)
    if not config.enabled:
        return _component("disabled", required=False)
    if dependency.available:
        return _component("ready", required=config.required)
    return _component(
        "not_ready" if config.required else "degraded",
        required=config.required,
    )


def _security_store_component(store: Any, *, required: bool) -> dict[str, Any]:
    """Probe one security store without ever treating uncertainty as healthy."""

    check = getattr(store, "check", None) if store is not None else None
    healthy = False
    if callable(check):
        try:
            healthy = check() is True
        except Exception:
            healthy = False
    if healthy:
        return _component("ready", required=required)
    return _component("not_ready" if required else "degraded", required=required)


def _readiness_snapshot(request: Request) -> tuple[bool, dict[str, Any]]:
    app_state = request.app.state
    components: dict[str, dict[str, Any]] = {}

    lifecycle = getattr(app_state, "lifecycle_status", "unknown")
    components["lifecycle"] = _component(
        "ready" if lifecycle == "ready" else "not_ready"
    )

    required_state = (
        "session_store",
        "kb_registry",
        "index_jobs",
        "feedback_store",
        "knowledge_store",
        "offload_executor",
    )
    state_ready = all(hasattr(app_state, name) for name in required_state)
    executor = getattr(app_state, "offload_executor", None)
    if executor is not None and getattr(executor, "_shutdown", False):
        state_ready = False

    # SQLite-backed stores are probed with a read-only statement. In-memory stores
    # have no connection and are considered available once initialized.
    if state_ready:
        try:
            for name in ("session_store", "feedback_store"):
                connection = getattr(getattr(app_state, name), "_conn", None)
                if connection is not None:
                    connection.execute("SELECT 1").fetchone()
        except Exception:
            state_ready = False
    components["state"] = _component("ready" if state_ready else "not_ready")

    try:
        ensure_rust_core(*REQUIRED_NATIVE_SYMBOLS)
        native_ready = True
    except RuntimeError:
        native_ready = False
    components["rust_core"] = _component("ready" if native_ready else "not_ready")

    auth_enabled = bool(getattr(app_state, "auth_enabled", False))
    account_auth_enabled = bool(
        getattr(
            app_state,
            "account_auth_enabled",
            getattr(app_state, "auth_store", None) is not None,
        )
    )
    if account_auth_enabled:
        components["authentication"] = _security_store_component(
            getattr(app_state, "auth_store", None), required=True
        )
        components["resource_access"] = _security_store_component(
            getattr(app_state, "resource_access_store", None), required=True
        )
    else:
        # Preserve the legacy/static-key contract: authentication is advisory,
        # and an optional ACL store cannot make an auth-off deployment unready.
        components["authentication"] = _component(
            "ready" if auth_enabled else "degraded", required=False
        )
        resource_store = getattr(app_state, "resource_access_store", None)
        components["resource_access"] = (
            _security_store_component(resource_store, required=False)
            if resource_store is not None
            else _component("degraded", required=False)
        )
    components["ocr"] = _ocr_readiness_component()

    ready = all(
        item["status"] == "ready" for item in components.values() if item["required"]
    )
    payload = {
        "status": "ready" if ready else "not_ready",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }
    return ready, payload


# 返回结果。
@router.get("/healthz")
async def healthz():
    # 存活探针：进程在跑即可，不做依赖检查。
    return {"status": "ok"}


@router.get("/health/live")
async def health_live():
    return {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# 返回结果。
@router.get("/readyz")
async def readyz(request: Request):
    # 保留旧探针的响应字段，同时纳入生命周期和状态依赖检查。
    ready, snapshot = _readiness_snapshot(request)
    native_ready = snapshot["components"]["rust_core"]["status"] == "ready"
    payload = {
        "status": "ready" if ready else "not_ready",
        "rust_core": native_ready,
    }
    if not ready:
        payload["reason"] = "required service dependency unavailable"
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/health/ready")
async def health_ready(request: Request):
    ready, payload = _readiness_snapshot(request)
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


# 返回结果。
@router.get("/metrics")
async def metrics(request: Request):
    # Prometheus 抓取端点：返回每 app 注册表的文本快照，鉴权/限流已豁免。
    return Response(
        content=request.app.state.metrics.render(),
        media_type=CONTENT_TYPE_LATEST,
    )
