import json
import os
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cogdoc.connectors.base import ConnectorError, ConnectorSourceRef
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
    assert confluence.fetch(confluence_ref).content == b"<p>Body</p>"

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


def test_http_transport_blocks_private_dns_before_opening():
    opener = SimpleNamespace(open=lambda request, timeout: pytest.fail("must not open"))
    transport = HttpTransport(
        allowed_hosts={"internal.example"},
        opener=opener,
        resolver=lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    with pytest.raises(ConnectorError, match="non-public"):
        transport.request("GET", "https://internal.example/data")
