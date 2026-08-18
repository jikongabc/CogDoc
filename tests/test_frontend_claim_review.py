from __future__ import annotations

from contextlib import nullcontext

import httpx

from cogdoc.frontend import app as frontend_app


class _State(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


class _StreamlitStub:
    def __init__(
        self, *, submit: bool = False, selected_verdict: str | None = None
    ):
        self.session_state = _State(
            claim_review_pages={},
            claim_review_export_jsonl="",
        )
        self.submit = submit
        self.selected_verdict = selected_verdict
        self.submit_disabled: bool | None = None
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.markdowns: list[str] = []

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [self for _ in range(count)]

    def metric(self, *args, **kwargs):
        return None

    def caption(self, *args, **kwargs):
        return None

    def selectbox(self, label, options, **kwargs):
        key = str(kwargs.get("key") or "")
        if key == "claim-review-status":
            return "pending"
        if key == "claim-review-page-size":
            return 25
        return list(options)[0]

    def button(self, *args, **kwargs):
        return False

    def markdown(self, body, **kwargs):
        self.markdowns.append(str(body))

    def error(self, message):
        self.errors.append(str(message))

    def warning(self, message):
        self.warnings.append(str(message))

    def success(self, message):
        self.successes.append(str(message))

    def info(self, *args, **kwargs):
        return None

    def divider(self):
        return None

    def form(self, *args, **kwargs):
        return nullcontext()

    def radio(self, label, options, **kwargs):
        if self.selected_verdict is not None:
            return self.selected_verdict
        index = kwargs.get("index", 0)
        return None if index is None else list(options)[index]

    def text_area(self, *args, **kwargs):
        return str(kwargs.get("value") or "")

    def form_submit_button(self, *args, **kwargs):
        self.submit_disabled = bool(kwargs.get("disabled"))
        return self.submit and not self.submit_disabled

    def spinner(self, *args, **kwargs):
        return nullcontext()

    def download_button(self, *args, **kwargs):
        return False

    def rerun(self):
        raise AssertionError("unexpected rerun")


class _Client:
    base_url = "http://api"
    auth_cache_identity = "identity"
    workspace_id = "workspace-a"

    def __init__(
        self, *, label_status: int = 200, expected_verdict: str | None = None
    ):
        self.label_status = label_status
        self.expected_verdict = expected_verdict
        self.calls: list[tuple] = []

    def claim_verification_review_summary(self):
        self.calls.append(("summary",))
        return httpx.Response(
            200,
            json={
                "pending_count": 1,
                "reviewed_count": 2,
                "agreement_rate": 0.5,
                "evidence_incomplete_count": 0,
                "shadow_count": 2,
                "enforce_count": 1,
                "total_count": 3,
                "oldest_pending_at": "2026-08-19T00:00:00+00:00",
            },
        )

    def list_claim_verification_reviews(self, **kwargs):
        self.calls.append(("list", kwargs))
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "review_id": "a" * 32,
                        "status": "pending",
                        "actual_verdict": "supported",
                        "claim": "<script>alert(1)</script>",
                    }
                ],
                "next_cursor": None,
            },
        )

    def get_claim_verification_review(self, review_id):
        self.calls.append(("detail", review_id))
        return httpx.Response(
            200,
            json={
                "review_id": review_id,
                "task_type": "qa",
                "policy_id": "1" * 16,
                "claim": "<script>alert(1)</script>",
                "actual_verdict": "supported",
                "expected_verdict": self.expected_verdict,
                "effective_mode": "shadow",
                "decision": "would_allow",
                "confidence": 0.9,
                "duration_ms": 12.0,
                "reason": "模型理由",
                "evidence_complete": True,
                "evidence": [
                    {
                        "chunk_id": "chunk-1",
                        "source": "guide.pdf",
                        "page": 2,
                        "text": "<img src=x onerror=alert(1)>",
                    }
                ],
                "review_note": "",
                "revision": 4,
            },
        )

    def label_claim_verification_review(self, review_id, **kwargs):
        self.calls.append(("label", review_id, kwargs))
        return httpx.Response(
            self.label_status,
            json={"error_code": "CLAIM_REVIEW_REVISION_CONFLICT"},
        )

    def export_all_claim_verification_reviews(self):
        raise AssertionError("export should only run after an explicit click")


def test_claim_review_desk_loads_detail_on_selection_and_escapes_content(monkeypatch):
    streamlit = _StreamlitStub()
    streamlit.session_state.claim_review_export_jsonl = "old-tenant-data"
    streamlit.session_state.claim_review_export_scope = "other-identity"
    client = _Client()
    monkeypatch.setattr(frontend_app, "st", streamlit)

    frontend_app._claim_verification_review_desk(client)

    assert client.calls == [
        ("summary",),
        ("list", {"status": "pending", "limit": 25, "cursor": None}),
        ("detail", "a" * 32),
    ]
    rendered = "\n".join(streamlit.markdowns)
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<img src=x onerror=alert(1)>" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert streamlit.errors == []
    assert streamlit.submit_disabled is True
    assert streamlit.session_state.claim_review_export_jsonl == ""
    assert streamlit.session_state.claim_review_export_scope.endswith(
        ":identity:workspace-a"
    )


def test_claim_review_desk_submits_revision_and_surfaces_conflict(monkeypatch):
    streamlit = _StreamlitStub(submit=True)
    client = _Client(label_status=409, expected_verdict="supported")
    monkeypatch.setattr(frontend_app, "st", streamlit)

    frontend_app._claim_verification_review_desk(client)

    assert client.calls[-1] == (
        "label",
        "a" * 32,
        {
            "expected_verdict": "supported",
            "expected_revision": 4,
            "review_note": "",
        },
    )
    assert streamlit.warnings == [
        "这条任务已被其他审核者更新，请刷新后基于最新 revision 重审。"
    ]
    assert streamlit.successes == []
