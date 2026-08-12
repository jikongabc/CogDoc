"""Production-app smoke test for persistent accounts and workspace isolation.

With no URL this command starts ``cogdoc.api.app:app`` in a subprocess with an
isolated data directory and account authentication enabled.  ``--url`` checks
an already-running instance, which is used for release-equivalent Docker images.
The HTTP client intentionally uses only the Python standard library so Docker
jobs do not need a second dependency installation on the host runner.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class SmokeFailure(RuntimeError):
    """An account-mode production contract was not satisfied."""


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any] | list[Any] | None, str]:
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    parsed: dict[str, Any] | list[Any] | None = None
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, (dict, list)):
            parsed = value
    return status, parsed, raw


def _expect(
    base_url: str,
    method: str,
    path: str,
    expected_status: int,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any] | list[Any] | None:
    status, parsed, raw = _request(
        base_url,
        method,
        path,
        payload=payload,
        token=token,
        timeout=timeout,
    )
    if status != expected_status:
        preview = raw[:500].replace("\n", " ")
        raise SmokeFailure(
            f"{method} {path}: expected HTTP {expected_status}, got {status}: {preview}"
        )
    return parsed


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeFailure(f"{label} did not return a JSON object")
    return value


def _wait_until_ready(
    base_url: str, *, startup_timeout: float, request_timeout: float
) -> None:
    deadline = time.monotonic() + startup_timeout
    last_error = "service did not answer"
    while time.monotonic() < deadline:
        try:
            health_status, _, health_raw = _request(
                base_url, "GET", "/healthz", timeout=request_timeout
            )
            ready_status, _, ready_raw = _request(
                base_url, "GET", "/readyz", timeout=request_timeout
            )
            if health_status == 200 and ready_status == 200:
                return
            last_error = (
                f"healthz={health_status} {health_raw[:120]!r}; "
                f"readyz={ready_status} {ready_raw[:120]!r}"
            )
        except (OSError, URLError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.2)
    raise SmokeFailure(
        f"account-auth production app was not ready within {startup_timeout}s: "
        f"{last_error}"
    )


def _exercise_account_contract(
    base_url: str, *, startup_timeout: float, request_timeout: float
) -> dict[str, Any]:
    _wait_until_ready(
        base_url,
        startup_timeout=startup_timeout,
        request_timeout=request_timeout,
    )

    config = _mapping(
        _expect(
            base_url,
            "GET",
            "/v1/auth/config",
            200,
            timeout=request_timeout,
        ),
        "auth config",
    )
    if config.get("account_auth_enabled") is not True:
        raise SmokeFailure("production app did not enable persistent account auth")
    if config.get("self_registration_enabled") is not True:
        raise SmokeFailure("production app did not enable bootstrap registration")

    _expect(
        base_url,
        "GET",
        "/v1/knowledge-bases",
        401,
        timeout=request_timeout,
    )

    nonce = secrets.token_hex(8)
    email = f"account-smoke-{nonce}@example.test"
    password = f"CogDoc account smoke {nonce}!"
    kb_id = f"account-smoke-{nonce}"
    registered = _mapping(
        _expect(
            base_url,
            "POST",
            "/v1/auth/register",
            201,
            payload={
                "email": email,
                "password": password,
                "display_name": "Account Smoke",
                "workspace_name": "Account Smoke Workspace",
            },
            timeout=request_timeout,
        ),
        "register",
    )
    registration_token = registered.get("access_token")
    if not isinstance(registration_token, str) or not registration_token:
        raise SmokeFailure("registration did not return a Bearer token")

    created = _mapping(
        _expect(
            base_url,
            "POST",
            "/v1/knowledge-bases",
            201,
            payload={"kb_id": kb_id, "access_policy": "private"},
            token=registration_token,
            timeout=request_timeout,
        ),
        "authenticated KB create",
    )
    if created.get("kb_id") != kb_id:
        raise SmokeFailure("authenticated KB create returned the wrong public ID")

    viewer_email = f"account-smoke-viewer-{nonce}@example.test"
    viewer_password = f"CogDoc viewer smoke {nonce}!"
    invitation = _mapping(
        _expect(
            base_url,
            "POST",
            f"/v1/workspaces/{registered['workspace']['workspace_id']}/invites",
            201,
            payload={"email": viewer_email, "role": "viewer"},
            token=registration_token,
            timeout=request_timeout,
        ),
        "viewer invitation",
    )
    invite_token = invitation.get("invite_token")
    if not isinstance(invite_token, str) or not invite_token:
        raise SmokeFailure("workspace invitation did not return a one-time token")
    viewer = _mapping(
        _expect(
            base_url,
            "POST",
            "/v1/auth/invitations/accept",
            200,
            payload={
                "token": invite_token,
                "email": viewer_email,
                "password": viewer_password,
                "display_name": "Account Smoke Viewer",
            },
            timeout=request_timeout,
        ),
        "viewer invitation acceptance",
    )
    viewer_token = viewer.get("access_token")
    viewer_user = _mapping(viewer.get("user"), "invited viewer")
    viewer_user_id = viewer_user.get("user_id")
    if not isinstance(viewer_token, str) or not viewer_token:
        raise SmokeFailure("invitation acceptance did not return a Bearer token")
    if not isinstance(viewer_user_id, str) or not viewer_user_id:
        raise SmokeFailure("invitation acceptance did not return a stable user ID")

    viewer_before_grant = _expect(
        base_url,
        "GET",
        "/v1/knowledge-bases",
        200,
        token=viewer_token,
        timeout=request_timeout,
    )
    if not isinstance(viewer_before_grant, list) or any(
        isinstance(item, dict) and item.get("kb_id") == kb_id
        for item in viewer_before_grant
    ):
        raise SmokeFailure("private KB was visible to an ungranted workspace viewer")
    _expect(
        base_url,
        "POST",
        "/v1/knowledge-bases",
        403,
        payload={"kb_id": f"viewer-write-{nonce}"},
        token=viewer_token,
        timeout=request_timeout,
    )

    _expect(
        base_url,
        "POST",
        f"/v1/knowledge-bases/{kb_id}/access/grants",
        200,
        payload={"subject_id": viewer_user_id, "role": "viewer"},
        token=registration_token,
        timeout=request_timeout,
    )
    viewer_after_grant = _expect(
        base_url,
        "GET",
        "/v1/knowledge-bases",
        200,
        token=viewer_token,
        timeout=request_timeout,
    )
    if not isinstance(viewer_after_grant, list) or not any(
        isinstance(item, dict) and item.get("kb_id") == kb_id
        for item in viewer_after_grant
    ):
        raise SmokeFailure("explicit KB grant did not authorize the workspace viewer")
    _expect(
        base_url,
        "DELETE",
        f"/v1/knowledge-bases/{kb_id}/access/grants/{viewer_user_id}",
        204,
        token=registration_token,
        timeout=request_timeout,
    )
    viewer_after_revoke = _expect(
        base_url,
        "GET",
        "/v1/knowledge-bases",
        200,
        token=viewer_token,
        timeout=request_timeout,
    )
    if not isinstance(viewer_after_revoke, list) or any(
        isinstance(item, dict) and item.get("kb_id") == kb_id
        for item in viewer_after_revoke
    ):
        raise SmokeFailure("revoked KB grant remained visible to the viewer")

    logged_in = _mapping(
        _expect(
            base_url,
            "POST",
            "/v1/auth/login",
            200,
            payload={"email": email, "password": password},
            timeout=request_timeout,
        ),
        "login",
    )
    login_token = logged_in.get("access_token")
    if not isinstance(login_token, str) or not login_token:
        raise SmokeFailure("login did not return a Bearer token")

    knowledge_bases = _expect(
        base_url,
        "GET",
        "/v1/knowledge-bases",
        200,
        token=login_token,
        timeout=request_timeout,
    )
    if not isinstance(knowledge_bases, list) or not any(
        isinstance(item, dict) and item.get("kb_id") == kb_id
        for item in knowledge_bases
    ):
        raise SmokeFailure("fresh login could not access the workspace knowledge base")

    identity = _mapping(
        _expect(
            base_url,
            "GET",
            "/v1/auth/me",
            200,
            token=login_token,
            timeout=request_timeout,
        ),
        "current identity",
    )
    user = _mapping(identity.get("user"), "current user")
    if user.get("email") != email:
        raise SmokeFailure("Bearer session resolved to the wrong user")

    _expect(
        base_url,
        "GET",
        "/v1/knowledge-bases",
        401,
        timeout=request_timeout,
    )
    ready = _mapping(
        _expect(base_url, "GET", "/readyz", 200, timeout=request_timeout),
        "readyz",
    )
    if ready.get("status") != "ready":
        raise SmokeFailure("readyz did not report ready after account operations")

    workspace = _mapping(registered.get("workspace"), "registered workspace")
    return {
        "ok": True,
        "account_auth_enabled": True,
        "workspace_id": workspace.get("workspace_id"),
        "kb_id": kb_id,
        "checks": [
            "auth_config",
            "anonymous_401",
            "register",
            "bearer_kb_create",
            "workspace_invitation",
            "viewer_rbac",
            "private_kb_grant_revoke",
            "login",
            "bearer_kb_access",
            "identity",
            "readyz",
        ],
    }


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _isolated_environment(data_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        f"{SRC}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(SRC)
    )
    environment.update(
        {
            "COGDOC_DATA_DIR": str(data_dir),
            "COGDOC_DOC_DIR": str(data_dir / "inbox"),
            "COGDOC_ACCOUNT_AUTH_ENABLED": "true",
            "COGDOC_SELF_REGISTRATION_ENABLED": "true",
            "COGDOC_API_KEYS": "",
            "COGDOC_API_PRINCIPALS": "",
            "COGDOC_EVAL_REVIEW_API_KEYS": "",
            "COGDOC_OCR_ENABLED": "false",
            "COGDOC_OCR_REQUIRED": "false",
            "COGDOC_TRACE_ENABLED": "false",
            "COGDOC_TRACE_DIR": str(data_dir / "traces"),
            "COGDOC_LOG_TO_CONSOLE": "false",
            "COGDOC_LOG_FILE": str(data_dir / "logs" / "account-smoke.jsonl"),
            "COGDOC_STATE_BACKEND": "sqlite",
            "RATE_LIMIT_PER_MINUTE": "600",
            "RATE_LIMIT_BURST": "100",
        }
    )
    return environment


def _local_smoke(*, startup_timeout: float, request_timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cogdoc-account-smoke-") as temporary:
        data_dir = Path(temporary) / "data"
        data_dir.mkdir(parents=True)
        port = _available_port()
        base_url = f"http://127.0.0.1:{port}"
        log_path = Path(temporary) / "uvicorn.log"
        with log_path.open("w+", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "cogdoc.api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--timeout-graceful-shutdown",
                    "5",
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=_isolated_environment(data_dir),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                result = _exercise_account_contract(
                    base_url,
                    startup_timeout=startup_timeout,
                    request_timeout=request_timeout,
                )
                if process.poll() is not None:
                    raise SmokeFailure(
                        f"production app exited unexpectedly with {process.returncode}"
                    )
                return result
            except Exception as exc:
                log_file.flush()
                log_file.seek(0)
                log_tail = log_file.read()[-4000:]
                raise SmokeFailure(f"{exc}\nproduction app log:\n{log_tail}") from exc
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test persistent accounts against the production CogDoc app."
    )
    parser.add_argument(
        "--url",
        help="Check an existing temporary instance instead of starting local Uvicorn.",
    )
    parser.add_argument("--startup-timeout", type=float, default=90.0)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args()
    if arguments.startup_timeout <= 0 or arguments.request_timeout <= 0:
        parser.error("timeouts must be positive")

    try:
        result = (
            _exercise_account_contract(
                arguments.url,
                startup_timeout=arguments.startup_timeout,
                request_timeout=arguments.request_timeout,
            )
            if arguments.url
            else _local_smoke(
                startup_timeout=arguments.startup_timeout,
                request_timeout=arguments.request_timeout,
            )
        )
    except Exception as exc:
        print(f"Account-auth smoke failed: {exc}", file=sys.stderr)
        return 1
    if not arguments.quiet:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
