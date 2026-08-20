from cogdoc.frontend import app as frontend_app


class _MarkdownStub:
    def __init__(self):
        self.blocks = []

    def markdown(self, body, **kwargs):
        self.blocks.append((body, kwargs))


def test_source_navigation_hero_renders_one_accessible_sync_pulse(monkeypatch):
    streamlit = _MarkdownStub()
    monkeypatch.setattr(frontend_app, "st", streamlit)

    frontend_app._source_navigation_hero(
        [{"connection_id": "c1"}],
        [{"connection_id": "c1", "health_status": "syncing"}],
        [{"status": "running"}],
        {"active_versions": 2},
    )

    rendered = streamlit.blocks[0][0]
    assert "来源航海台" in rendered
    assert 'role="list"' in rendered
    assert rendered.count('role="listitem"') == 4
    assert rendered.count("is-moving") == 2
    assert "连接健康" in rendered
    assert "ACL / 引用" in rendered


def test_source_provider_connection_filter_is_provider_and_binding_scoped():
    connections = [
        {
            "connection_id": "notion-ready",
            "connector_type": "notion",
            "credential_source": "none",
            "credential_id": None,
        },
        {
            "connection_id": "notion-env",
            "connector_type": "notion",
            "credential_source": "environment",
            "credential_id": None,
        },
        {
            "connection_id": "sharepoint-ready",
            "connector_type": "sharepoint",
            "credential_source": "none",
            "credential_id": None,
        },
    ]

    notion = frontend_app._source_provider_connections(connections, "notion")
    microsoft = frontend_app._source_provider_connections(connections, "microsoft")

    assert [row["connection_id"] for row in notion] == ["notion-ready"]
    assert [row["connection_id"] for row in microsoft] == ["sharepoint-ready"]


def test_source_console_formatters_are_bounded_and_human_readable():
    assert frontend_app._source_format_bytes(0) == "0 B"
    assert frontend_app._source_format_bytes(1536) == "1.5 KiB"
    assert frontend_app._source_format_bytes("invalid") == "—"
    assert frontend_app._source_status_label("dead_letter") == "死信"
    assert frontend_app._source_status_class("dead_letter") == "is-fault"
    assert frontend_app._source_format_time(None) == "—"
