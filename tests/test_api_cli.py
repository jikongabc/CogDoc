import json
import stat
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from cogdoc import api_cli
from cogdoc.frontend.api_client import CogDocClient


def _response(status: int = 200, payload=None) -> httpx.Response:
    return httpx.Response(status, json={} if payload is None else payload)


def test_default_config_path_ignores_empty_environment_values(monkeypatch):
    monkeypatch.setenv("COGDOC_CLI_CONFIG", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")

    assert api_cli.default_config_path() == (
        Path.home() / ".config" / "cogdoc" / "cli.json"
    )


def test_cli_login_persists_server_workspace_and_reuses_it_for_kb_list(
    tmp_path: Path, monkeypatch, capsys
):
    config = tmp_path / "config" / "cli.json"
    seen: list[tuple[str, str | None]] = []

    monkeypatch.setattr(api_cli, "_password", lambda confirm=False: "password-1234")
    monkeypatch.setattr(
        CogDocClient,
        "login",
        lambda self, email, password, workspace_id=None: _response(
            payload={
                "access_token": "session-token",
                "expires_at": "2026-09-01T00:00:00Z",
                "user": {"email": email},
                "workspace": {"workspace_id": "wsp-current", "name": "Current"},
            }
        ),
    )
    login = api_cli.build_parser().parse_args(
        ["--config-path", str(config), "login", "owner@example.com"]
    )
    assert api_cli.APICommandRunner(login).run(login) == 0

    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["access_token"] == "session-token"
    assert stored["workspace_id"] == "wsp-current"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    def list_kbs(self):
        seen.append((self._headers["Authorization"], self.workspace_id))
        return [{"kb_id": "111"}]

    monkeypatch.setattr(CogDocClient, "list_knowledge_bases", list_kbs)
    listing = api_cli.build_parser().parse_args(
        ["--config-path", str(config), "kb", "list"]
    )
    assert api_cli.APICommandRunner(listing).run(listing) == 0
    assert seen == [("Bearer session-token", "wsp-current")]
    assert '"kb_id": "111"' in capsys.readouterr().out


def test_cli_login_clears_previous_kb_selection(tmp_path: Path, monkeypatch):
    config = tmp_path / "cli.json"
    config.write_text(
        json.dumps(
            {"access_token": "old", "workspace_id": "old-ws", "kb_id": "old-kb"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api_cli, "_password", lambda confirm=False: "password-1234")
    monkeypatch.setattr(
        CogDocClient,
        "login",
        lambda self, email, password, workspace_id=None: _response(
            payload={
                "access_token": "new",
                "user": {"email": email},
                "workspace": {"workspace_id": "new-ws"},
            }
        ),
    )
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(config), "login", "new@example.com"]
    )

    assert api_cli.APICommandRunner(args).run(args) == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["workspace_id"] == "new-ws"
    assert "kb_id" not in stored


def test_workspace_switch_clears_kb_and_chat_session(tmp_path: Path, monkeypatch):
    config = tmp_path / "cli.json"
    config.write_text(
        json.dumps(
            {
                "access_token": "old-token",
                "workspace_id": "old-workspace",
                "kb_id": "old-kb",
                "session_id": "old-chat",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        CogDocClient,
        "switch_workspace",
        lambda self, workspace_id: _response(
            payload={
                "access_token": "new-token",
                "workspace": {"workspace_id": workspace_id},
            }
        ),
    )
    args = api_cli.build_parser().parse_args(
        [
            "--config-path",
            str(config),
            "workspace",
            "use",
            "new-workspace",
        ]
    )

    assert api_cli.APICommandRunner(args).run(args) == 0
    stored = json.loads(config.read_text(encoding="utf-8"))
    assert stored["workspace_id"] == "new-workspace"
    assert "kb_id" not in stored
    assert "session_id" not in stored


def test_local_api_mode_uses_builtin_roles_without_workspace_state(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        CogDocClient,
        "auth_config",
        lambda self: _response(payload={"account_auth_enabled": False}),
    )
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(tmp_path / "cli.json"), "kb", "list"]
    )

    assert api_cli.APICommandRunner(args).upload_role_ids(None) == list(
        api_cli.BUILT_IN_ROLE_IDS
    )


def test_account_api_mode_requires_workspace_before_defaulting_roles(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        CogDocClient,
        "auth_config",
        lambda self: _response(payload={"account_auth_enabled": True}),
    )
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(tmp_path / "cli.json"), "kb", "list"]
    )

    with pytest.raises(ValueError, match="workspace use"):
        api_cli.APICommandRunner(args).upload_role_ids(None)


def test_chat_rejects_stream_that_ends_before_final_event(
    tmp_path: Path, monkeypatch, capsys
):
    config = tmp_path / "cli.json"
    config.write_text(json.dumps({"kb_id": "handbook"}), encoding="utf-8")

    def truncated_stream(self, kb_id, query, **kwargs):
        yield "token", {"content": "partial answer"}

    monkeypatch.setattr(CogDocClient, "stream_chat", truncated_stream)
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(config), "chat", "question"]
    )

    with pytest.raises(api_cli.CogDocAPIError, match="完成事件前中断"):
        api_cli.APICommandRunner(args).run(args)
    assert capsys.readouterr().out == "partial answer\n"


def test_interactive_keyboard_interrupt_during_dispatch_exits_cleanly(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("builtins.input", lambda prompt="": "status")
    monkeypatch.setattr(
        api_cli.APICommandRunner,
        "run",
        lambda self, args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    base = api_cli.build_parser().parse_args(
        ["--config-path", str(tmp_path / "cli.json")]
    )

    assert api_cli._interactive(base, api_cli.build_parser()) == 0


def test_interactive_commands_inherit_global_context(tmp_path: Path, monkeypatch):
    config = tmp_path / "cli.json"
    inputs = iter(["status", "exit"])
    captured: list[Namespace] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(
        api_cli.APICommandRunner,
        "run",
        lambda self, args: captured.append(args) or 0,
    )
    base = api_cli.build_parser().parse_args(
        [
            "--api-url",
            "https://example.test",
            "--token",
            "secret",
            "--workspace",
            "ws-one",
            "--config-path",
            str(config),
            "--compact",
        ]
    )

    assert api_cli._interactive(base, api_cli.build_parser()) == 0
    assert len(captured) == 1
    assert captured[0].api_url == "https://example.test"
    assert captured[0].token == "secret"
    assert captured[0].workspace == "ws-one"
    assert captured[0].config_path == config
    assert captured[0].compact is True


def test_interactive_console_keeps_brand_and_original_command_aliases(
    tmp_path: Path, monkeypatch, capsys
):
    inputs = iter(["/kb", "/kb new handbook", "/docs", "/qa 谁可以访问", "exit"])
    captured: list[Namespace] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(
        api_cli.APICommandRunner,
        "run",
        lambda self, args: captured.append(args) or 0,
    )
    base = api_cli.build_parser().parse_args(
        ["--config-path", str(tmp_path / "cli.json")]
    )

    assert api_cli._interactive(base, api_cli.build_parser()) == 0
    output = capsys.readouterr().out
    assert "██████╗" in output
    assert "CogDoc 控制台" in output
    assert [
        (item.command, getattr(item, "kb_action", None)) for item in captured[:2]
    ] == [
        ("kb", "list"),
        ("kb", "create"),
    ]
    assert captured[1].kb_id == "handbook"
    assert captured[2].command == "document"
    assert captured[2].document_action == "list"
    assert captured[3].command == "chat"
    assert captured[3].query == ["谁可以访问"]
    assert captured[3].mode == "qa"


def test_original_review_and_tuning_aliases_use_the_api_commands():
    assert api_cli._normalize_interactive_values(["dk", "approve", "dk-1"]) == [
        "knowledge",
        "review",
        "approve",
        "dk-1",
    ]
    assert api_cli._normalize_interactive_values(["dk", "stale-scan"]) == [
        "knowledge",
        "scan",
    ]
    assert api_cli._normalize_interactive_values(["tuning", "list"]) == [
        "feedback",
        "tuning",
    ]
    assert api_cli._normalize_interactive_values(["review", "summary"]) == [
        "knowledge",
        "summary",
    ]


def test_original_add_command_resolves_files_from_the_inbox(
    tmp_path: Path, monkeypatch
):
    inbox = tmp_path / "documents"
    inbox.mkdir()
    document = inbox / "report.pdf"
    document.write_bytes(b"pdf")
    inputs = iter(["/add report.pdf", "exit"])
    captured: list[Namespace] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(
        api_cli,
        "get_settings",
        lambda: Namespace(cogdoc_doc_dir=str(inbox)),
    )
    monkeypatch.setattr(
        api_cli.APICommandRunner,
        "run",
        lambda self, args: captured.append(args) or 0,
    )
    base = api_cli.build_parser().parse_args(
        ["--config-path", str(tmp_path / "cli.json")]
    )

    assert api_cli._interactive(base, api_cli.build_parser()) == 0
    assert captured[0].command == "document"
    assert captured[0].document_action == "upload"
    assert captured[0].files == [str(document)]


def test_cloud_configuration_hides_api_key_and_uses_the_single_cli(
    tmp_path: Path, monkeypatch
):
    captured = {}
    monkeypatch.setattr(api_cli.getpass, "getpass", lambda _prompt: "new-secret")
    monkeypatch.setattr(
        api_cli,
        "get_settings",
        lambda: Namespace(
            llm_base_url="https://old.example.com/v1",
            llm_model_name="old-model",
        ),
    )
    monkeypatch.setattr(
        api_cli,
        "apply_llm_config",
        lambda **values: captured.update(values),
    )
    answers = iter(["https://api.example.com/v1", "model-v1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(tmp_path / "cli.json"), "config"]
    )

    assert api_cli.APICommandRunner(args).run(args) == 0
    assert captured == {
        "api_key": "new-secret",
        "base_url": "https://api.example.com/v1",
        "model": "model-v1",
    }

    parser = api_cli.build_parser()
    config_parser = next(
        action.choices["config"]
        for action in parser._actions
        if isinstance(getattr(action, "choices", None), dict)
        and "config" in action.choices
    )
    assert "--api-key" not in config_parser.format_help()


def test_cli_kb_use_rejects_inaccessible_kb(tmp_path: Path, monkeypatch):
    config = tmp_path / "cli.json"
    config.write_text(
        json.dumps(
            {
                "access_token": "session-token",
                "workspace_id": "wsp-current",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        CogDocClient,
        "list_knowledge_bases",
        lambda self: [{"kb_id": "allowed"}],
    )
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(config), "kb", "use", "hidden"]
    )

    runner = api_cli.APICommandRunner(args)
    try:
        runner.run(args)
    except ValueError as exc:
        assert "无权访问" in str(exc)
    else:
        raise AssertionError("inaccessible KB selection must fail closed")


def test_cli_kb_create_defaults_to_all_workspace_roles(tmp_path: Path, monkeypatch):
    config = tmp_path / "cli.json"
    config.write_text(
        json.dumps(
            {
                "access_token": "session-token",
                "workspace_id": "wsp-current",
            }
        ),
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(
        CogDocClient,
        "list_workspace_roles",
        lambda self, workspace_id: _response(
            payload={
                "roles": [
                    {"role_id": "owner"},
                    {"role_id": "viewer"},
                    {"role_id": "role-custom"},
                ]
            }
        ),
    )

    def create(self, kb_id, *, access_policy="workspace", role_ids=None):
        captured.update(kb_id=kb_id, access_policy=access_policy, role_ids=role_ids)
        return _response(201, {"kb_id": kb_id})

    monkeypatch.setattr(CogDocClient, "create_knowledge_base", create)
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(config), "kb", "create", "new-kb"]
    )

    assert api_cli.APICommandRunner(args).run(args) == 0
    assert captured == {
        "kb_id": "new-kb",
        "access_policy": "workspace",
        "role_ids": ["owner", "viewer", "role-custom"],
    }


def test_client_batch_upload_matches_web_contract(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _response(202, {"job_id": "idx-1"})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)
    response = CogDocClient(
        "http://api", api_key="session", workspace_id="workspace"
    ).upload_documents(
        "kb/one",
        [("a.pdf", b"pdf"), ("b.md", b"markdown")],
        allowed_role_ids=["viewer", "role-custom"],
        embedding_profile_id="cloud",
    )

    assert response.status_code == 202
    assert calls[0][0] == "http://api/v1/knowledge-bases/kb%2Fone/documents/batch"
    assert [item[0] for item in calls[0][1]["files"]] == ["files", "files"]
    assert calls[0][1]["data"] == [
        ("allowed_role_ids", "viewer"),
        ("allowed_role_ids", "role-custom"),
        ("embedding_profile_id", "cloud"),
    ]
    assert calls[0][1]["headers"] == {
        "Authorization": "Bearer session",
        "X-CogDoc-Workspace": "workspace",
    }


def test_api_escape_hatch_is_version_scoped(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.request",
        lambda method, url, **kwargs: (
            calls.append((method, url, kwargs)) or _response(payload={"ok": True})
        ),
    )
    client = CogDocClient("http://api", api_key="token")

    assert client.request(
        "PATCH", "/v1/workspaces/ws", json_body={"name": "B"}
    ).json() == {"ok": True}
    assert calls[0][0:2] == ("PATCH", "http://api/v1/workspaces/ws")
    try:
        client.request("GET", "/metrics")
    except ValueError as exc:
        assert "/v1/" in str(exc)
    else:
        raise AssertionError("raw CLI requests must remain inside the versioned API")

    for path in (
        "/v1/../metrics",
        "/v1/%2e%2e/metrics",
        "/v1/%252e%252e/metrics",
        "/v1/jobs%3Fadmin=true",
        "/v1/jobs%23fragment",
        "/v1/jobs%00hidden",
        "/v1/%25252525252525252e%25252525252525252e/metrics",
    ):
        try:
            client.request("GET", path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"path traversal must be rejected: {path}")


def test_jobs_preserve_available_sources_when_ha_is_disabled(
    tmp_path: Path, monkeypatch, capsys
):
    config = tmp_path / "cli.json"
    config.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")
    monkeypatch.setattr(
        CogDocClient,
        "list_index_jobs",
        lambda self, kb_id=None, limit=200: _response(
            payload={"jobs": [{"id": "idx"}]}
        ),
    )
    monkeypatch.setattr(
        CogDocClient,
        "list_workspace_sync_jobs",
        lambda self, limit=200: _response(payload={"jobs": [{"id": "sync"}]}),
    )
    monkeypatch.setattr(
        CogDocClient,
        "list_ha_jobs",
        lambda self, limit=200: _response(503, {"detail": "HA 控制面未启用"}),
    )
    args = api_cli.build_parser().parse_args(["--config-path", str(config), "jobs"])

    assert api_cli.APICommandRunner(args).run(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["index_jobs"]["jobs"][0]["id"] == "idx"
    assert output["sync_jobs"]["jobs"][0]["id"] == "sync"
    assert output["unavailable"]["system_jobs"]["status"] == 503


def test_claim_summary_uses_selected_kb(tmp_path: Path, monkeypatch):
    config = tmp_path / "cli.json"
    config.write_text(json.dumps({"kb_id": "kb-current"}), encoding="utf-8")
    seen: list[str | None] = []
    monkeypatch.setattr(
        CogDocClient,
        "claim_verification_review_summary",
        lambda self, kb_id=None: seen.append(kb_id) or _response(payload={"total": 0}),
    )
    args = api_cli.build_parser().parse_args(
        ["--config-path", str(config), "evaluation", "claim-summary"]
    )

    assert api_cli.APICommandRunner(args).run(args) == 0
    assert seen == ["kb-current"]


def test_feedback_submit_and_knowledge_binding_are_exposed(tmp_path: Path, monkeypatch):
    config = tmp_path / "cli.json"
    config.write_text(json.dumps({"kb_id": "kb-current"}), encoding="utf-8")
    feedback_seen = {}
    knowledge_seen = {}

    def submit(self, trace_id, feedback, **kwargs):
        feedback_seen.update(trace_id=trace_id, feedback=feedback, **kwargs)
        return _response(payload={"feedback_id": "fb-1"})

    def create(self, **kwargs):
        knowledge_seen.update(kwargs)
        return _response(201, {"knowledge_id": "dk-1"})

    monkeypatch.setattr(CogDocClient, "submit_feedback", submit)
    monkeypatch.setattr(CogDocClient, "create_knowledge", create)
    feedback = api_cli.build_parser().parse_args(
        [
            "--config-path",
            str(config),
            "feedback",
            "submit",
            "--trace-id",
            "trace-1",
            "--value",
            "thumbs_down",
            "--issue",
            "bad_retrieval",
            "--chunks",
            "chunk-1,chunk-2",
        ]
    )
    knowledge = api_cli.build_parser().parse_args(
        [
            "--config-path",
            str(config),
            "knowledge",
            "create",
            "correct answer",
            "--document-id",
            "doc-1",
            "--source",
            "manual.pdf",
            "--source-sha256",
            "abc",
            "--chunks",
            "chunk-1,chunk-2",
            "--trace-id",
            "trace-1",
        ]
    )

    assert api_cli.APICommandRunner(feedback).run(feedback) == 0
    assert api_cli.APICommandRunner(knowledge).run(knowledge) == 0
    assert feedback_seen["related_chunk_ids"] == ["chunk-1", "chunk-2"]
    assert knowledge_seen["related_document_id"] == "doc-1"
    assert knowledge_seen["related_source_sha256"] == "abc"
    assert knowledge_seen["created_from_trace_id"] == "trace-1"


def test_local_storage_compatibility_entrypoint_is_preserved(monkeypatch):
    import cogdoc.cli as local_cli

    calls: list[bool] = []
    monkeypatch.setattr(local_cli, "local_main", lambda: calls.append(True) or 7)

    assert api_cli.main(["--local-storage"]) == 7
    assert calls == [True]
