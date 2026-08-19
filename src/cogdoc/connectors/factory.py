from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from cogdoc.connectors.http_transport import HttpTransport
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


def _secret(connection: Mapping[str, Any], field: str) -> str:
    references = connection.get("secret_env")
    if not isinstance(references, Mapping):
        raise ValueError("connection secret references are unavailable")
    env_name = str(references.get(field) or "")
    value = os.environ.get(env_name, "") if env_name else ""
    if not value:
        raise ValueError(f"connection secret environment is missing: {field}")
    return value


def _transport(*urls: str) -> HttpTransport:
    hosts = {
        str(urlsplit(url).hostname or "").casefold() for url in urls if str(url).strip()
    }
    hosts.discard("")
    if not hosts:
        raise ValueError("connector has no valid HTTPS host")
    return HttpTransport(allowed_hosts=hosts)


def build_connector(connection: Mapping[str, Any]):
    connector_type = str(connection.get("connector_type") or "").casefold()
    config = connection.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("connection config is unavailable")
    if connector_type == "local-directory":
        return LocalDirectoryConnector(
            str(config["root"]), follow_symlinks=config.get("follow_symlinks") is True
        )
    if connector_type == "git":
        return GitConnector(
            str(config["repository"]),
            ref=str(config.get("ref") or "HEAD"),
            subpath=str(config.get("subpath") or "."),
        )
    if connector_type == "url":
        urls = [str(item) for item in config["urls"]]
        return UrlConnector(urls, _transport(*urls))
    if connector_type == "zotero":
        return ZoteroConnector(
            str(config["library_type"]),
            str(config["library_id"]),
            _secret(connection, "api_key"),
            _transport("https://api.zotero.org", "https://files.zotero.net"),
        )
    if connector_type == "notion":
        return NotionConnector(
            _secret(connection, "token"), _transport("https://api.notion.com")
        )
    if connector_type == "confluence":
        base_url = str(config["base_url"])
        return ConfluenceConnector(
            base_url,
            _secret(connection, "token"),
            _transport(base_url),
            include_acl=config.get("include_acl", True) is True,
        )
    if connector_type == "sharepoint":
        return SharePointConnector(
            str(config["site_id"]),
            str(config["drive_id"]),
            _secret(connection, "token"),
            _transport("https://graph.microsoft.com"),
            include_acl=config.get("include_acl", True) is True,
        )
    if connector_type == "s3":
        endpoint = str(config.get("endpoint") or "") or None
        default_endpoint = (
            f"https://{config['bucket']}.s3.{config['region']}.amazonaws.com"
        )
        return S3Connector(
            bucket=str(config["bucket"]),
            region=str(config["region"]),
            access_key=_secret(connection, "access_key"),
            secret_key=_secret(connection, "secret_key"),
            session_token=(
                _secret(connection, "session_token")
                if "session_token" in connection.get("secret_env", {})
                else None
            ),
            prefix=str(config.get("prefix") or ""),
            endpoint=endpoint,
            transport=_transport(endpoint or default_endpoint),
        )
    raise ValueError(f"unsupported connector type: {connector_type}")
