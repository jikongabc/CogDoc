import json
import os
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cogdoc.connectors.base import (
    MAX_CONNECTOR_ACL_BYTES,
    MAX_CONNECTOR_ACL_GRANTS,
    ConnectorError,
    ConnectorSourceRef,
)
from cogdoc.connectors.http_transport import HttpResponse, HttpTransport
from cogdoc.connectors.implementations import (
    ConfluenceConnector,
    GitConnector,
    LocalDirectoryConnector,
    NotionConnector,
    S3Connector,
    SharePointConnector,
    UrlConnector,
    ZoteroConnector,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url, dict(headers or {}), body))
        response = self.responses.pop(0)
        if callable(response):
            return response(method, url, headers, body)
        return response


def _response(body, *, content_type="application/json", url="https://provider.test/x"):
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    return HttpResponse(200, {"content-type": content_type}, body, url)


def test_local_directory_connector_is_recursive_and_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "guide.md").write_text("guide", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, root / "link.md")

    connector = LocalDirectoryConnector(str(root))
    page = connector.list_page(None, limit=10)
    assert [item.external_id for item in page.items] == ["nested/guide.md"]
    assert connector.fetch(page.items[0]).content == b"guide"
    with pytest.raises(ConnectorError, match="symlinks"):
        connector.fetch(
            type(page.items[0])("link.md", "link.md", media_type="text/markdown")
        )


def test_git_connector_lists_revision_and_fetches_blob(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "guide.md").write_text("# Guide", encoding="utf-8")
    (repo / "skip.bin").write_bytes(b"skip")
    subprocess.run(["git", "-C", str(repo), "add", "guide.md", "skip.bin"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )

    connector = GitConnector(str(repo))
    page = connector.list_page(None, limit=10)
    assert [item.external_id for item in page.items] == ["guide.md"]
    assert connector.fetch(page.items[0]).content == b"# Guide"


def test_url_connector_uses_bounded_transport_content_type():
    transport = FakeTransport(
        [
            _response(
                b"<h1>Hello</h1>",
                content_type="text/html",
                url="https://docs.example/a",
            )
        ]
    )
    connector = UrlConnector(["https://docs.example/a"], transport)
    ref = connector.list_page(None, limit=10).items[0]
    fetched = connector.fetch(ref)
    assert fetched.ref.media_type == "text/html" and fetched.content.startswith(b"<h1>")


def test_zotero_lists_supported_attachment_and_fetches_enclosure():
    rows = [
        {
            "data": {
                "key": "A1",
                "filename": "paper.pdf",
                "contentType": "application/pdf",
                "dateModified": "2026-01-01T00:00:00Z",
            },
            "links": {"enclosure": {"href": "https://files.zotero.net/paper.pdf"}},
        }
    ]
    transport = FakeTransport(
        [_response(rows), _response(b"%PDF", content_type="application/pdf")]
    )
    connector = ZoteroConnector("users", "42", "secret", transport)
    page = connector.list_page(None, limit=10)
    assert page.complete and page.items[0].external_id == "A1"
    assert connector.fetch(page.items[0]).content == b"%PDF"
    assert transport.calls[0][2]["Zotero-API-Key"] == "secret"


def test_notion_converts_blocks_to_markdown():
    search = {
        "results": [
            {
                "id": "page-id",
                "url": "https://notion.so/page-id",
                "last_edited_time": "2026-01-01T00:00:00Z",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [{"plain_text": "Roadmap"}],
                    }
                },
            }
        ],
        "has_more": False,
    }
    blocks = {
        "results": [
            {
                "type": "heading_1",
                "heading_1": {"rich_text": [{"plain_text": "Plan"}]},
            },
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "Details"}]},
            },
        ],
        "has_more": False,
    }
    transport = FakeTransport([_response(search), _response(blocks)])
    connector = NotionConnector("secret", transport)
    ref = connector.list_page(None, limit=10).items[0]
    assert ref.display_name == "Roadmap.md"
    assert connector.fetch(ref).content.decode() == "# Plan\n\nDetails"


def test_notion_fetches_nested_blocks_with_shared_budget():
    parent = {
        "results": [
            {
                "id": "child",
                "type": "toggle",
                "toggle": {"rich_text": [{"plain_text": "More"}]},
                "has_children": True,
            }
        ],
        "has_more": False,
    }
    child = {
        "results": [
            {
                "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "Nested"}]},
            }
        ],
        "has_more": False,
    }
    transport = FakeTransport([_response(parent), _response(child)])
    connector = NotionConnector("secret", transport)
    fetched = connector.fetch(ConnectorSourceRef("page", "page.md"))
    assert fetched.content.decode() == "More\n\nNested"


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        pytest.param("notion", {}, id="notion-missing-results"),
        pytest.param(
            "notion",
            {"results": "not-a-list", "has_more": False},
            id="notion-wrong-results",
        ),
        pytest.param("confluence", {}, id="confluence-missing-results"),
        pytest.param(
            "confluence",
            {"results": "not-a-list", "_links": {}},
            id="confluence-wrong-results",
        ),
        pytest.param("sharepoint", {}, id="sharepoint-missing-value"),
        pytest.param(
            "sharepoint",
            {"value": {}, "@odata.deltaLink": "https://graph.example/delta"},
            id="sharepoint-wrong-value",
        ),
    ],
)
def test_provider_list_schema_uncertainty_never_becomes_an_empty_snapshot(
    provider, payload
):
    transport = FakeTransport([_response(payload)])
    connector = {
        "notion": lambda: NotionConnector("token", transport),
        "confluence": lambda: ConfluenceConnector(
            "https://wiki.example", "token", transport
        ),
        "sharepoint": lambda: SharePointConnector("site", "drive", "token", transport),
    }[provider]()

    with pytest.raises(ConnectorError, match="schema"):
        connector.list_page(None, limit=10)


def test_confluence_missing_storage_body_is_not_materialized_as_empty_content():
    connector = ConfluenceConnector(
        "https://wiki.example",
        "token",
        FakeTransport([_response({"body": {}})]),
    )

    with pytest.raises(ConnectorError, match="storage body"):
        connector.fetch(ConnectorSourceRef("page", "page.html"))


@pytest.mark.parametrize(
    "xml",
    [
        b"<foo/>",
        b"<ListBucketResult/>",
        b"<ListBucketResult><IsTruncated>maybe</IsTruncated></ListBucketResult>",
        (
            b"<ListBucketResult><IsTruncated>false</IsTruncated>"
            b"<Contents><Key>docs/a.md</Key></Contents></ListBucketResult>"
        ),
    ],
)
def test_s3_malformed_list_never_becomes_an_empty_snapshot(xml):
    connector = S3Connector(
        bucket="bucket",
        region="us-east-1",
        access_key="AKID",
        secret_key="secret",
        transport=FakeTransport([_response(xml, content_type="application/xml")]),
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(ConnectorError):
        connector.list_page(None, limit=10)


def test_confluence_reads_storage_html_and_sharepoint_downloads_file():
    confluence_transport = FakeTransport(
        [
            _response({"results": [{"id": "1", "title": "Page"}], "_links": {}}),
            _response({"body": {"storage": {"value": "<p>Body</p>"}}}),
            _response(
                {
                    "read": {
                        "restrictions": {
                            "user": {"results": [], "size": 0},
                            "group": {"results": [], "size": 0},
                        }
                    }
                }
            ),
        ]
    )
    confluence = ConfluenceConnector(
        "https://wiki.example", "token", confluence_transport
    )
    confluence_ref = confluence.list_page(None, limit=10).items[0]
    confluence_fetched = confluence.fetch(confluence_ref)
    assert confluence_fetched.content == b"<p>Body</p>"
    assert confluence_fetched.acl == {
        "complete": True,
        "workspace_visible": True,
        "provider_version": None,
        "grants": [],
    }

    graph_transport = FakeTransport(
        [
            _response(
                {
                    "value": [
                        {
                            "id": "item",
                            "name": "report.docx",
                            "file": {
                                "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                            },
                            "size": 4,
                        }
                    ],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-token",
                }
            ),
            _response(b"docx", content_type="application/octet-stream"),
            _response(
                {
                    "value": [
                        {
                            "roles": ["read"],
                            "grantedToV2": {"user": {"email": "alice@example.com"}},
                        }
                    ]
                }
            ),
        ]
    )
    sharepoint = SharePointConnector("site", "drive", "token", graph_transport)
    sharepoint_page = sharepoint.list_page(None, limit=10)
    sharepoint_ref = sharepoint_page.items[0]
    assert sharepoint_page.next_cursor.startswith("delta:")
    fetched = sharepoint.fetch(sharepoint_ref)
    assert fetched.content == b"docx"
    assert fetched.acl["grants"][0]["external_subject"] == "alice@example.com"


def test_confluence_acl_stably_deduplicates_normalized_subjects():
    transport = FakeTransport(
        [
            _response({"body": {"storage": {"value": "body"}}}),
            _response(
                {
                    "restrictionsHash": "acl-v1",
                    "read": {
                        "restrictions": {
                            "user": {
                                "results": [
                                    {"email": "Alice@Example.COM"},
                                    {"email": " alice@example.com "},
                                ],
                                "size": 2,
                            },
                            "group": {
                                "results": [{"name": "ALICE@EXAMPLE.COM"}],
                                "size": 1,
                            },
                        }
                    },
                }
            ),
        ]
    )
    connector = ConfluenceConnector("https://wiki.example", "token", transport)

    fetched = connector.fetch(ConnectorSourceRef("1", "page.html"))

    assert fetched.acl == {
        "complete": True,
        "workspace_visible": False,
        "provider_version": "acl-v1",
        "grants": [
            {
                "external_subject": "alice@example.com",
                "subject_type": "user",
                "permission": "read",
            },
            {
                "external_subject": "alice@example.com",
                "subject_type": "group",
                "permission": "read",
            },
        ],
    }


def test_confluence_acl_grant_overflow_quarantines_without_losing_content(
    monkeypatch,
):
    # Isolate the grant-count boundary from the independent serialized-byte
    # boundary; production enforces both and either one must quarantine.
    monkeypatch.setattr(
        "cogdoc.connectors.base.MAX_CONNECTOR_ACL_BYTES",
        MAX_CONNECTOR_ACL_BYTES * 10,
    )
    rows = [
        {"accountId": f"account-{index}"}
        for index in range(MAX_CONNECTOR_ACL_GRANTS + 1)
    ]
    transport = FakeTransport(
        [
            _response({"body": {"storage": {"value": "body"}}}),
            _response(
                {
                    "read": {
                        "restrictions": {
                            "user": {"results": rows, "size": len(rows)},
                            "group": {"results": [], "size": 0},
                        }
                    }
                }
            ),
        ]
    )
    connector = ConfluenceConnector("https://wiki.example", "token", transport)

    fetched = connector.fetch(ConnectorSourceRef("1", "page.html"))

    assert fetched.content == b"body"
    assert fetched.acl == {
        "complete": False,
        "workspace_visible": False,
        "provider_version": None,
        "grants": [],
    }


@pytest.mark.parametrize(
    ("first_role", "second_role"),
    [("write", "read"), ("read", "write")],
)
def test_sharepoint_acl_merges_duplicate_roles_to_least_privilege_in_stable_order(
    first_role, second_role
):
    transport = FakeTransport(
        [
            _response(b"file", content_type="application/octet-stream"),
            _response(
                {
                    "value": [
                        {
                            "roles": ["owner"],
                            "grantedToV2": {"user": {"email": "bob@example.com"}},
                        },
                        {
                            "roles": [first_role],
                            "grantedToV2": {"user": {"email": "Alice@Example.COM"}},
                        },
                        {
                            "roles": [second_role],
                            "grantedToV2": {"user": {"email": " alice@example.com "}},
                        },
                    ]
                }
            ),
        ]
    )
    connector = SharePointConnector("site", "drive", "token", transport)

    fetched = connector.fetch(ConnectorSourceRef("item", "report.docx"))

    assert fetched.acl == {
        "complete": True,
        "workspace_visible": False,
        "provider_version": None,
        "grants": [
            {
                "external_subject": "bob@example.com",
                "subject_type": "user",
                "permission": "write",
            },
            {
                "external_subject": "alice@example.com",
                "subject_type": "user",
                "permission": "read",
            },
        ],
    }


def test_sharepoint_acl_byte_overflow_quarantines_without_losing_content():
    transport = FakeTransport(
        [
            _response(b"file", content_type="application/octet-stream"),
            _response(
                {
                    "value": [
                        {
                            "roles": ["read"],
                            "grantedToV2": {
                                "user": {"email": "x" * MAX_CONNECTOR_ACL_BYTES}
                            },
                        }
                    ]
                }
            ),
        ]
    )
    connector = SharePointConnector("site", "drive", "token", transport)

    fetched = connector.fetch(
        ConnectorSourceRef("item", "report.docx", etag="acl-etag")
    )

    assert fetched.content == b"file"
    assert fetched.acl == {
        "complete": False,
        "workspace_visible": False,
        "provider_version": "acl-etag",
        "grants": [],
    }


def test_sharepoint_unknown_acl_role_quarantines_without_losing_content():
    transport = FakeTransport(
        [
            _response(b"file", content_type="application/octet-stream"),
            _response(
                {
                    "value": [
                        {
                            "roles": ["future-role"],
                            "grantedToV2": {"user": {"email": "alice@example.com"}},
                        }
                    ]
                }
            ),
        ]
    )
    connector = SharePointConnector("site", "drive", "token", transport)

    fetched = connector.fetch(ConnectorSourceRef("item", "report.docx"))

    assert fetched.content == b"file"
    assert fetched.acl == {
        "complete": False,
        "workspace_visible": False,
        "provider_version": None,
        "grants": [],
    }


def test_sharepoint_display_name_only_identity_quarantines_content():
    transport = FakeTransport(
        [
            _response(b"file", content_type="application/octet-stream"),
            _response(
                {
                    "value": [
                        {
                            "roles": ["read"],
                            "grantedToV2": {
                                "user": {"displayName": "alice@example.com"}
                            },
                        }
                    ]
                }
            ),
        ]
    )
    connector = SharePointConnector("site", "drive", "token", transport)

    fetched = connector.fetch(ConnectorSourceRef("item", "report.docx"))

    assert fetched.content == b"file"
    assert fetched.acl["complete"] is False
    assert fetched.acl["workspace_visible"] is False
    assert fetched.acl["grants"] == []


@pytest.mark.parametrize(
    ("connector_type", "subject"),
    [
        pytest.param("confluence", 123, id="confluence-number"),
        pytest.param("sharepoint", {"unexpected": "object"}, id="sharepoint-object"),
    ],
)
def test_provider_acl_non_string_subject_quarantines_content(connector_type, subject):
    if connector_type == "confluence":
        connector = ConfluenceConnector(
            "https://wiki.example",
            "token",
            FakeTransport(
                [
                    _response({"body": {"storage": {"value": "body"}}}),
                    _response(
                        {
                            "read": {
                                "restrictions": {
                                    "user": {
                                        "results": [{"accountId": subject}],
                                        "size": 1,
                                    },
                                    "group": {"results": [], "size": 0},
                                }
                            }
                        }
                    ),
                ]
            ),
        )
        ref = ConnectorSourceRef("1", "page.html")
        expected_content = b"body"
    else:
        connector = SharePointConnector(
            "site",
            "drive",
            "token",
            FakeTransport(
                [
                    _response(b"file", content_type="application/octet-stream"),
                    _response(
                        {
                            "value": [
                                {
                                    "roles": ["read"],
                                    "grantedToV2": {"user": {"email": subject}},
                                }
                            ]
                        }
                    ),
                ]
            ),
        )
        ref = ConnectorSourceRef("item", "report.docx")
        expected_content = b"file"

    fetched = connector.fetch(ref)

    assert fetched.content == expected_content
    assert fetched.acl["complete"] is False
    assert fetched.acl["workspace_visible"] is False
    assert fetched.acl["grants"] == []


@pytest.mark.parametrize("scope", ["organization", "anonymous"])
def test_sharepoint_broad_links_are_parsed_as_provider_workspace_visibility(scope):
    transport = FakeTransport(
        [
            _response(b"file", content_type="application/octet-stream"),
            _response(
                {
                    "value": [
                        {
                            "roles": ["read"],
                            "link": {"scope": scope},
                        }
                    ]
                }
            ),
        ]
    )
    connector = SharePointConnector("site", "drive", "token", transport)

    fetched = connector.fetch(ConnectorSourceRef("item", "report.docx"))

    assert fetched.acl == {
        "complete": True,
        "workspace_visible": True,
        "provider_version": None,
        "grants": [],
    }


def test_atlassian_oauth_confluence_uses_cloud_gateway_for_content_and_acl():
    transport = FakeTransport(
        [
            _response({"results": [{"id": "1", "title": "Page"}], "_links": {}}),
            _response({"body": {"storage": {"value": "<p>Body</p>"}}}),
            _response(
                {
                    "read": {
                        "restrictions": {
                            "user": {"results": [], "size": 0},
                            "group": {"results": [], "size": 0},
                        }
                    }
                }
            ),
        ]
    )
    connector = ConfluenceConnector(
        "https://docs.atlassian.net",
        "oauth-token",
        transport,
        cloud_id="cloud-123",
    )
    ref = connector.list_page(None, limit=10).items[0]
    fetched = connector.fetch(ref)

    assert fetched.content == b"<p>Body</p>"
    assert [call[1] for call in transport.calls] == [
        "https://api.atlassian.com/ex/confluence/cloud-123/wiki/api/v2/pages?limit=10",
        "https://api.atlassian.com/ex/confluence/cloud-123/wiki/api/v2/pages/1?body-format=storage",
        "https://api.atlassian.com/ex/confluence/cloud-123/wiki/rest/api/content/1/restriction/byOperation",
    ]


@pytest.mark.parametrize(
    "acl_response",
    [
        {},
        {"read": {}},
        {
            "read": {
                "restrictions": {
                    "user": {"results": [{}], "size": 1},
                    "group": {"results": [], "size": 0},
                }
            }
        },
        pytest.param(
            {
                "read": {
                    "restrictions": {
                        "user": {"results": [], "size": 0, "totalSize": 1},
                        "group": {"results": [], "size": 0},
                    }
                }
            },
            id="total-size-shows-truncation",
        ),
        pytest.param(
            {
                "read": {
                    "restrictions": {
                        "user": {
                            "results": [{"accountId": "alice"}],
                            "size": 1,
                            "_links": {"next": "/next-page"},
                        },
                        "group": {"results": [], "size": 0},
                    }
                }
            },
            id="next-link-shows-truncation",
        ),
        pytest.param(
            {
                "read": {
                    "restrictions": {
                        "user": {
                            "results": [{"accountId": "alice"}],
                            "size": 1,
                            "start": 0,
                            "limit": 1,
                        },
                        "group": {"results": [], "size": 0},
                    }
                }
            },
            id="full-page-without-total-size",
        ),
    ],
)
def test_confluence_acl_uncertainty_quarantines_content(acl_response):
    transport = FakeTransport(
        [
            _response({"body": {"storage": {"value": "body"}}}),
            _response(acl_response),
        ]
    )
    connector = ConfluenceConnector("https://wiki.example", "token", transport)
    fetched = connector.fetch(ConnectorSourceRef("1", "page.html"))
    assert fetched.content == b"body"
    assert fetched.acl == {
        "complete": False,
        "workspace_visible": False,
        "provider_version": None,
        "grants": [],
    }


@pytest.mark.parametrize("connector_type", ["confluence", "sharepoint"])
def test_acl_transport_failure_quarantines_without_losing_content(connector_type):
    def fail_acl(*_args):
        raise ConnectorError("permission endpoint denied")

    if connector_type == "confluence":
        connector = ConfluenceConnector(
            "https://wiki.example",
            "token",
            FakeTransport(
                [_response({"body": {"storage": {"value": "body"}}}), fail_acl]
            ),
        )
        ref = ConnectorSourceRef("1", "page.html")
        expected = b"body"
    else:
        connector = SharePointConnector(
            "site",
            "drive",
            "token",
            FakeTransport([_response(b"file"), fail_acl]),
        )
        ref = ConnectorSourceRef("1", "file.docx", etag="etag")
        expected = b"file"
    fetched = connector.fetch(ref)
    assert fetched.content == expected
    assert fetched.acl["complete"] is False
    assert fetched.acl["workspace_visible"] is False
    assert fetched.acl["grants"] == []


def test_sharepoint_delta_propagates_nested_deletions():
    transport = FakeTransport(
        [
            _response(
                {
                    "value": [{"id": "gone", "deleted": {"state": "deleted"}}],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/next-delta",
                }
            )
        ]
    )
    connector = SharePointConnector("site", "drive", "token", transport)
    page = connector.list_page(
        "delta:https://graph.microsoft.com/v1.0/previous-delta", limit=10
    )
    assert page.snapshot is False
    assert page.deleted_external_ids == ("gone",)
    assert page.complete is True


def test_s3_signs_session_token_lists_and_fetches_object():
    xml = b"""<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>docs/a.md</Key><LastModified>2026-01-01T00:00:00Z</LastModified><ETag>&quot;etag&quot;</ETag><Size>3</Size></Contents></ListBucketResult>"""
    transport = FakeTransport(
        [_response(xml, content_type="application/xml"), _response(b"abc")]
    )
    connector = S3Connector(
        bucket="bucket",
        region="us-east-1",
        access_key="AKID",
        secret_key="secret",
        session_token="session",
        transport=transport,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    ref = connector.list_page(None, limit=10).items[0]
    assert ref.external_id == "docs/a.md"
    assert connector.fetch(ref).content == b"abc"
    assert "x-amz-security-token" in transport.calls[0][2]
    assert "x-amz-security-token" in transport.calls[0][2]["Authorization"]


def test_s3_signature_canonical_host_matches_lowercase_transport_host_with_port():
    upper_transport = FakeTransport([_response(b"")])
    lower_transport = FakeTransport([_response(b"")])
    options = {
        "bucket": "bucket",
        "region": "us-east-1",
        "access_key": "AKID",
        "secret_key": "secret",
        "clock": lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    upper = S3Connector(transport=upper_transport, **options)
    lower = S3Connector(transport=lower_transport, **options)

    upper._signed("GET", "https://BUCKET.S3.EXAMPLE:9443/docs/a.md")
    lower._signed("GET", "https://bucket.s3.example:9443/docs/a.md")

    assert upper_transport.calls[0][2]["Authorization"] == (
        lower_transport.calls[0][2]["Authorization"]
    )


def test_http_transport_blocks_private_dns_before_opening():
    opener = SimpleNamespace(open=lambda request, timeout: pytest.fail("must not open"))
    transport = HttpTransport(
        allowed_hosts={"internal.example"},
        opener=opener,
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    with pytest.raises(ConnectorError, match="non-public"):
        transport.request("GET", "https://internal.example/data")
