from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET

from cogdoc.connectors.base import (
    ConnectorError,
    ConnectorPage,
    ConnectorSourceRef,
    FetchedSource,
    RetryableConnectorError,
)
from cogdoc.connectors.http_transport import HttpResponse, HttpTransport
from cogdoc.tools.source_parser import MAX_SOURCE_BYTES, SUPPORTED_EXTENSIONS


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not str(cursor).isdigit():
        raise ConnectorError("connector cursor is invalid")
    return int(cursor)


def _paged(
    items: list[ConnectorSourceRef], cursor: str | None, limit: int
) -> ConnectorPage:
    start = _cursor_offset(cursor)
    page = tuple(items[start : start + limit])
    end = start + len(page)
    return ConnectorPage(
        page,
        next_cursor=None if end >= len(items) else str(end),
        complete=end >= len(items),
        snapshot=True,
    )


def _media(name: str, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(name)[0] or fallback


class LocalDirectoryConnector:
    connector_type = "local-directory"

    def __init__(self, root: str, *, follow_symlinks: bool = False) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("local connector root must be a directory")
        self.follow_symlinks = follow_symlinks

    def _paths(self) -> list[Path]:
        paths = []
        for path in self.root.rglob("*"):
            if path.is_symlink() and not self.follow_symlinks:
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if self.root not in resolved.parents or not resolved.is_file():
                continue
            if resolved.suffix.casefold() in SUPPORTED_EXTENSIONS:
                paths.append(resolved)
        return sorted(paths, key=lambda path: path.relative_to(self.root).as_posix())

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        refs = []
        for path in self._paths():
            relative = path.relative_to(self.root).as_posix()
            stat = path.stat()
            refs.append(
                ConnectorSourceRef(
                    relative,
                    path.name,
                    media_type=_media(path.name),
                    origin_uri="cogdoc-local:///" + quote(relative),
                    modified_at=datetime.fromtimestamp(
                        stat.st_mtime, timezone.utc
                    ).isoformat(),
                    byte_size=stat.st_size,
                )
            )
        return _paged(refs, cursor, limit)

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        relative = PurePosixPath(ref.external_id)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConnectorError("local source path is invalid")
        unresolved = self.root / relative
        if not self.follow_symlinks:
            cursor = self.root
            for part in relative.parts:
                cursor /= part
                if cursor.is_symlink():
                    raise ConnectorError("local source symlinks are disabled")
        candidate = unresolved.resolve(strict=True)
        if self.root not in candidate.parents or not candidate.is_file():
            raise ConnectorError("local source escaped the configured root")
        if candidate.is_symlink() and not self.follow_symlinks:
            raise ConnectorError("local source symlinks are disabled")
        with candidate.open("rb") as handle:
            content = handle.read(MAX_SOURCE_BYTES + 1)
        if len(content) > MAX_SOURCE_BYTES:
            raise ConnectorError("local source exceeds the byte limit")
        return FetchedSource(ref, content)


class GitConnector:
    connector_type = "git"

    def __init__(
        self,
        repository: str,
        *,
        ref: str = "HEAD",
        subpath: str = ".",
        timeout_seconds: float = 30.0,
        runner=subprocess.run,
    ) -> None:
        self.repository = str(Path(repository).resolve(strict=True))
        self.ref = str(ref).strip()
        self.subpath = str(PurePosixPath(subpath))
        if not self.ref or self.ref.startswith("-") or "\x00" in self.ref:
            raise ValueError("git ref is invalid")
        if ".." in PurePosixPath(self.subpath).parts:
            raise ValueError("git subpath cannot escape the repository")
        self.timeout_seconds = timeout_seconds
        self._runner = runner

    def _run(self, args: list[str], *, max_bytes: int) -> bytes:
        try:
            result = self._runner(
                ["git", "-C", self.repository, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RetryableConnectorError("git operation failed") from exc
        if result.returncode != 0:
            raise ConnectorError("git rejected the requested revision")
        if len(result.stdout) > max_bytes:
            raise ConnectorError("git output exceeds the byte limit")
        return bytes(result.stdout)

    def _refs(self) -> list[ConnectorSourceRef]:
        raw = self._run(
            ["ls-tree", "-r", "-z", "--long", self.ref, "--", self.subpath],
            max_bytes=32 * 1024 * 1024,
        )
        refs = []
        for record in raw.split(b"\x00"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode, kind, object_id, size = header.decode("ascii").split()
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise ConnectorError("git tree output is malformed") from exc
            if (
                kind != "blob"
                or Path(path).suffix.casefold() not in SUPPORTED_EXTENSIONS
            ):
                continue
            refs.append(
                ConnectorSourceRef(
                    path,
                    Path(path).name,
                    media_type=_media(path),
                    etag=object_id,
                    byte_size=int(size),
                    metadata={"git_mode": mode, "git_ref": self.ref},
                )
            )
        return refs

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        return _paged(self._refs(), cursor, limit)

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        if ".." in PurePosixPath(ref.external_id).parts or "\x00" in ref.external_id:
            raise ConnectorError("git source path is invalid")
        raw = self._run(
            ["show", f"{self.ref}:{ref.external_id}"], max_bytes=MAX_SOURCE_BYTES
        )
        return FetchedSource(ref, raw)


class UrlConnector:
    connector_type = "url"

    def __init__(self, urls: Iterable[str], transport: HttpTransport) -> None:
        self.urls = tuple(
            dict.fromkeys(str(url).strip() for url in urls if str(url).strip())
        )
        if not self.urls:
            raise ValueError("at least one URL is required")
        self.transport = transport

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        refs = [
            ConnectorSourceRef(
                url,
                Path(urlsplit(url).path).name or f"page-{index + 1}.html",
                origin_uri=url,
            )
            for index, url in enumerate(self.urls)
        ]
        return _paged(refs, cursor, limit)

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        response = self.transport.request(
            "GET", ref.external_id, headers={"Accept": "*/*"}
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[
            0
        ] or _media(ref.display_name)
        fetched_ref = ConnectorSourceRef(
            ref.external_id,
            ref.display_name,
            media_type=content_type,
            origin_uri=ref.origin_uri,
            etag=response.headers.get("etag"),
            modified_at=response.headers.get("last-modified"),
            byte_size=len(response.body),
        )
        return FetchedSource(fetched_ref, response.body)


class _JsonApiConnector:
    connector_type = "json-api"

    def __init__(
        self, base_url: str, transport: HttpTransport, headers: Mapping[str, str]
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.transport = transport
        self.headers = dict(headers)

    def _json(
        self, method: str, path_or_url: str, *, payload: dict | None = None
    ) -> dict:
        url = (
            path_or_url
            if urlsplit(path_or_url).scheme
            else urljoin(self.base_url, path_or_url)
        )
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {**self.headers, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.transport.request(method, url, headers=headers, body=body).json()


class ZoteroConnector(_JsonApiConnector):
    connector_type = "zotero"

    def __init__(
        self,
        library_type: str,
        library_id: str,
        api_key: str,
        transport: HttpTransport,
    ) -> None:
        if library_type not in {"users", "groups"}:
            raise ValueError("Zotero library_type must be users or groups")
        super().__init__(
            "https://api.zotero.org/", transport, {"Zotero-API-Key": api_key}
        )
        self.library_path = f"{library_type}/{quote(str(library_id), safe='')}"

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        start = _cursor_offset(cursor)
        response = self.transport.request(
            "GET",
            urljoin(
                self.base_url,
                f"{self.library_path}/items?itemType=attachment&start={start}&limit={limit}&format=json",
            ),
            headers=self.headers,
        )
        try:
            rows = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise ConnectorError("Zotero returned invalid JSON") from exc
        if not isinstance(rows, list):
            raise ConnectorError("Zotero item response must be a list")
        refs = []
        for row in rows:
            data = row.get("data", {}) if isinstance(row, dict) else {}
            links = row.get("links", {}) if isinstance(row, dict) else {}
            enclosure = links.get("enclosure", {}) if isinstance(links, dict) else {}
            href = enclosure.get("href") if isinstance(enclosure, dict) else None
            filename = str(data.get("filename") or "")
            key = str(data.get("key") or "")
            if (
                not href
                or not key
                or Path(filename).suffix.casefold() not in SUPPORTED_EXTENSIONS
            ):
                continue
            refs.append(
                ConnectorSourceRef(
                    key,
                    filename,
                    media_type=str(data.get("contentType") or _media(filename)),
                    origin_uri=str(href),
                    modified_at=str(data.get("dateModified") or "") or None,
                    metadata={"parent_item": data.get("parentItem")},
                )
            )
        complete = len(rows) < limit
        return ConnectorPage(
            tuple(refs),
            next_cursor=None if complete else str(start + len(rows)),
            complete=complete,
            snapshot=True,
        )

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        response = self.transport.request(
            "GET", str(ref.origin_uri), headers=self.headers
        )
        return FetchedSource(ref, response.body)


def _rich_text(block: Mapping[str, Any]) -> str:
    kind = str(block.get("type") or "")
    payload = block.get(kind, {}) if isinstance(block.get(kind), dict) else {}
    rich = payload.get("rich_text", []) if isinstance(payload, dict) else []
    text = "".join(
        str(item.get("plain_text") or "") for item in rich if isinstance(item, dict)
    )
    prefixes = {
        "heading_1": "# ",
        "heading_2": "## ",
        "heading_3": "### ",
        "bulleted_list_item": "- ",
        "numbered_list_item": "1. ",
    }
    return prefixes.get(kind, "") + text


class NotionConnector(_JsonApiConnector):
    connector_type = "notion"

    def __init__(self, token: str, transport: HttpTransport) -> None:
        super().__init__(
            "https://api.notion.com/v1/",
            transport,
            {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
        )

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        payload: dict[str, Any] = {
            "page_size": min(limit, 100),
            "filter": {"property": "object", "value": "page"},
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = self._json("POST", "search", payload=payload)
        refs = []
        for row in data.get("results", []):
            if not isinstance(row, dict) or not row.get("id"):
                continue
            properties = row.get("properties", {})
            title = ""
            if isinstance(properties, dict):
                for prop in properties.values():
                    if isinstance(prop, dict) and prop.get("type") == "title":
                        title = "".join(
                            str(item.get("plain_text") or "")
                            for item in prop.get("title", [])
                            if isinstance(item, dict)
                        )
                        break
            refs.append(
                ConnectorSourceRef(
                    str(row["id"]),
                    (title or str(row["id"])) + ".md",
                    media_type="text/markdown",
                    origin_uri=str(row.get("url") or "") or None,
                    modified_at=str(row.get("last_edited_time") or "") or None,
                )
            )
        complete = not bool(data.get("has_more"))
        return ConnectorPage(
            tuple(refs),
            next_cursor=None if complete else str(data.get("next_cursor") or ""),
            complete=complete,
            snapshot=True,
        )

    def _block_lines(
        self,
        block_id: str,
        *,
        depth: int,
        budget: list[int],
    ) -> list[str]:
        if depth > 16:
            raise ConnectorError("Notion block nesting exceeds the safety limit")
        cursor = None
        lines: list[str] = []
        for _ in range(100):
            suffix = (
                f"?page_size=100{f'&start_cursor={quote(cursor)}' if cursor else ''}"
            )
            data = self._json(
                "GET", f"blocks/{quote(block_id, safe='')}/children{suffix}"
            )
            for row in data.get("results", []):
                if not isinstance(row, dict):
                    continue
                budget[0] += 1
                if budget[0] > 10_000:
                    raise ConnectorError("Notion block count exceeds the safety limit")
                rendered = _rich_text(row)
                if rendered:
                    lines.append(rendered)
                if row.get("has_children") and row.get("id"):
                    lines.extend(
                        self._block_lines(
                            str(row["id"]), depth=depth + 1, budget=budget
                        )
                    )
            if not data.get("has_more"):
                break
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                raise ConnectorError("Notion pagination cursor is missing")
        else:
            raise ConnectorError("Notion block pagination exceeds the safety limit")
        return lines

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        lines = self._block_lines(ref.external_id, depth=0, budget=[0])
        return FetchedSource(ref, "\n\n".join(line for line in lines if line).encode())


class ConfluenceConnector(_JsonApiConnector):
    connector_type = "confluence"

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: HttpTransport,
        *,
        include_acl: bool = True,
    ) -> None:
        self.site_url = base_url.rstrip("/")
        self.include_acl = include_acl
        super().__init__(
            self.site_url + "/wiki/api/v2/",
            transport,
            {"Authorization": f"Bearer {token}"},
        )

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        path = f"pages?limit={min(limit, 250)}" + (
            f"&cursor={quote(cursor)}" if cursor else ""
        )
        data = self._json("GET", path)
        refs = []
        for row in data.get("results", []):
            if not isinstance(row, dict) or not row.get("id"):
                continue
            links = row.get("_links", {}) if isinstance(row.get("_links"), dict) else {}
            refs.append(
                ConnectorSourceRef(
                    str(row["id"]),
                    str(row.get("title") or row["id"]) + ".html",
                    media_type="text/html",
                    origin_uri=urljoin(self.base_url, str(links.get("webui") or "")),
                    modified_at=str(row.get("version", {}).get("createdAt") or "")
                    if isinstance(row.get("version"), dict)
                    else None,
                )
            )
        next_link = (
            data.get("_links", {}).get("next")
            if isinstance(data.get("_links"), dict)
            else None
        )
        next_cursor = None
        if next_link:
            next_cursor = dict(parse_qsl(urlsplit(str(next_link)).query)).get("cursor")
        return ConnectorPage(
            tuple(refs),
            next_cursor=next_cursor,
            complete=not bool(next_link),
            snapshot=True,
        )

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        data = self._json(
            "GET", f"pages/{quote(ref.external_id, safe='')}?body-format=storage"
        )
        body = (
            data.get("body", {}).get("storage", {}).get("value", "")
            if isinstance(data.get("body"), dict)
            else ""
        )
        acl = None
        if self.include_acl:
            restrictions = self._json(
                "GET",
                self.site_url
                + "/wiki/rest/api/content/"
                + quote(ref.external_id, safe="")
                + "/restriction/byOperation",
            )
            read = restrictions.get("read", {})
            if not isinstance(read, dict):
                read = restrictions.get("restrictions", {}).get("read", {})
            buckets = read.get("restrictions", {}) if isinstance(read, dict) else {}
            grants = []
            complete = True
            for subject_type in ("user", "group"):
                bucket = (
                    buckets.get(subject_type, {}) if isinstance(buckets, dict) else {}
                )
                rows = bucket.get("results", []) if isinstance(bucket, dict) else []
                if not isinstance(rows, list):
                    complete = False
                    continue
                size = bucket.get("size", len(rows))
                if isinstance(size, int) and size > len(rows):
                    complete = False
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    external_subject = (
                        row.get("email")
                        or row.get("accountId")
                        or row.get("id")
                        or row.get("name")
                    )
                    if external_subject:
                        grants.append(
                            {
                                "external_subject": str(external_subject),
                                "subject_type": subject_type,
                                "permission": "read",
                            }
                        )
            acl = {
                "complete": complete,
                "workspace_visible": complete and not grants,
                "provider_version": restrictions.get("restrictionsHash"),
                "grants": grants,
            }
        return FetchedSource(ref, str(body).encode(), acl=acl)


class SharePointConnector(_JsonApiConnector):
    connector_type = "sharepoint"

    def __init__(
        self,
        site_id: str,
        drive_id: str,
        token: str,
        transport: HttpTransport,
        *,
        include_acl: bool = True,
    ) -> None:
        super().__init__(
            "https://graph.microsoft.com/v1.0/",
            transport,
            {"Authorization": f"Bearer {token}"},
        )
        self.site_id = quote(site_id, safe="")
        self.drive_id = quote(drive_id, safe="")
        self.include_acl = include_acl

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        if cursor is None:
            path = f"sites/{self.site_id}/drives/{self.drive_id}/root/delta?$top={min(limit, 200)}"
            mode = "snapshot"
        elif cursor.startswith("snapshot:"):
            path = cursor.removeprefix("snapshot:")
            mode = "snapshot"
        elif cursor.startswith("delta:"):
            path = cursor.removeprefix("delta:")
            mode = "delta"
        else:
            raise ConnectorError("SharePoint delta cursor is invalid")
        data = self._json("GET", path)
        refs = []
        deleted = []
        for row in data.get("value", []):
            if not isinstance(row, dict) or not row.get("id"):
                continue
            if "deleted" in row:
                deleted.append(str(row["id"]))
                continue
            if "file" not in row:
                continue
            name = str(row.get("name") or row["id"])
            if Path(name).suffix.casefold() not in SUPPORTED_EXTENSIONS:
                continue
            refs.append(
                ConnectorSourceRef(
                    str(row["id"]),
                    name,
                    media_type=str(row.get("file", {}).get("mimeType") or _media(name)),
                    origin_uri=str(row.get("webUrl") or "") or None,
                    etag=str(row.get("eTag") or "") or None,
                    modified_at=str(row.get("lastModifiedDateTime") or "") or None,
                    byte_size=row.get("size"),
                    metadata={"parent_reference": row.get("parentReference", {})},
                )
            )
        next_link = data.get("@odata.nextLink")
        delta_link = data.get("@odata.deltaLink")
        if next_link:
            next_cursor = f"{mode}:{next_link}"
            complete = False
        else:
            if not delta_link:
                raise ConnectorError("SharePoint delta link is missing")
            next_cursor = f"delta:{delta_link}"
            complete = True
        return ConnectorPage(
            tuple(refs),
            tuple(deleted),
            next_cursor=next_cursor,
            complete=complete,
            snapshot=mode == "snapshot",
        )

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        url = urljoin(
            self.base_url,
            f"drives/{self.drive_id}/items/{quote(ref.external_id, safe='')}/content",
        )
        response = self.transport.request("GET", url, headers=self.headers)
        acl = None
        if self.include_acl:
            permissions = self._json(
                "GET",
                f"drives/{self.drive_id}/items/"
                f"{quote(ref.external_id, safe='')}/permissions",
            )
            rows = permissions.get("value", [])
            complete = isinstance(rows, list) and not permissions.get("@odata.nextLink")
            grants = []
            workspace_visible = False
            for permission in rows if isinstance(rows, list) else []:
                if not isinstance(permission, dict):
                    continue
                roles = permission.get("roles", [])
                permission_name = "write" if "write" in roles else "read"
                link = permission.get("link", {})
                if isinstance(link, dict) and link.get("scope") in {
                    "organization",
                    "anonymous",
                }:
                    workspace_visible = True
                identities = []
                for key in ("grantedToV2", "grantedTo"):
                    value = permission.get(key)
                    if isinstance(value, dict):
                        identities.append(value)
                for key in ("grantedToIdentitiesV2", "grantedToIdentities"):
                    value = permission.get(key)
                    if isinstance(value, list):
                        identities.extend(
                            item for item in value if isinstance(item, dict)
                        )
                for identity in identities:
                    for subject_type in ("user", "group"):
                        subject = identity.get(subject_type)
                        if not isinstance(subject, dict):
                            continue
                        external_subject = (
                            subject.get("email")
                            or subject.get("loginName")
                            or subject.get("id")
                            or subject.get("displayName")
                        )
                        if external_subject:
                            grants.append(
                                {
                                    "external_subject": str(external_subject),
                                    "subject_type": subject_type,
                                    "permission": permission_name,
                                }
                            )
            acl = {
                "complete": complete,
                "workspace_visible": complete and workspace_visible,
                "provider_version": ref.etag,
                "grants": grants,
            }
        return FetchedSource(ref, response.body, acl=acl)


class S3Connector:
    connector_type = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        transport: HttpTransport,
        prefix: str = "",
        endpoint: str | None = None,
        session_token: str | None = None,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.prefix = prefix
        self.endpoint = (
            endpoint or f"https://{bucket}.s3.{region}.amazonaws.com"
        ).rstrip("/")
        self.transport = transport
        self._clock = clock

    def _signed(self, method: str, url: str, payload: bytes = b"") -> HttpResponse:
        now = self._clock()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        parts = urlsplit(url)
        canonical_query = urlencode(
            sorted(parse_qsl(parts.query, keep_blank_values=True)), quote_via=quote
        )
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_headers = f"host:{parts.netloc}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        if self.session_token:
            canonical_headers += f"x-amz-security-token:{self.session_token}\n"
            signed_headers += ";x-amz-security-token"
        canonical = "\n".join(
            [
                method,
                parts.path or "/",
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        scope = f"{day}/{self.region}/s3/aws4_request"
        string_to_sign = (
            "AWS4-HMAC-SHA256\n"
            + amz_date
            + "\n"
            + scope
            + "\n"
            + hashlib.sha256(canonical.encode()).hexdigest()
        )
        key = ("AWS4" + self.secret_key).encode()
        for value in (day, self.region, "s3", "aws4_request"):
            key = hmac.new(key, value.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        }
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        return self.transport.request(
            method, url, headers=headers, body=payload or None
        )

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage:
        query = {
            "list-type": "2",
            "max-keys": str(min(limit, 1000)),
            "prefix": self.prefix,
        }
        if cursor:
            query["continuation-token"] = cursor
        response = self._signed("GET", self.endpoint + "/?" + urlencode(query))
        if (
            b"<!DOCTYPE" in response.body.upper()
            or b"<!ENTITY" in response.body.upper()
        ):
            raise ConnectorError("S3 returned unsafe XML")
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as exc:
            raise ConnectorError("S3 returned invalid XML") from exc
        if sum(1 for _ in root.iter()) > 250_000:
            raise ConnectorError("S3 XML contains too many nodes")

        def local(node):
            return node.tag.rsplit("}", 1)[-1]

        refs = []
        for content in (node for node in root if local(node) == "Contents"):
            values = {local(node): node.text or "" for node in content}
            key = values.get("Key", "")
            if not key or Path(key).suffix.casefold() not in SUPPORTED_EXTENSIONS:
                continue
            refs.append(
                ConnectorSourceRef(
                    key,
                    Path(key).name,
                    media_type=_media(key),
                    origin_uri=self.endpoint + "/" + quote(key, safe="/"),
                    etag=values.get("ETag", "").strip('"') or None,
                    modified_at=values.get("LastModified") or None,
                    byte_size=int(values.get("Size") or 0),
                )
            )
        truncated = next(
            (node.text == "true" for node in root if local(node) == "IsTruncated"),
            False,
        )
        next_cursor = next(
            (node.text for node in root if local(node) == "NextContinuationToken"), None
        )
        if truncated and not next_cursor:
            raise ConnectorError("S3 continuation token is missing")
        return ConnectorPage(
            tuple(refs), next_cursor=next_cursor, complete=not truncated, snapshot=True
        )

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource:
        response = self._signed(
            "GET", self.endpoint + "/" + quote(ref.external_id, safe="/")
        )
        return FetchedSource(ref, response.body)
