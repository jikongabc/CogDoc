from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
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


SecretResolver = Callable[[str, str, str, str], Mapping[str, str]]
_S3_BUCKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_S3_REGION = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


def _resolved_secrets(
    connection: Mapping[str, Any],
    resolver: SecretResolver | None,
    *,
    allow_environment_secrets: bool,
) -> dict[str, str]:
    credential_id = str(connection.get("credential_id") or "")
    if credential_id:
        if resolver is None:
            raise ValueError("connection credential vault is unavailable")
        resolved = resolver(
            str(connection.get("tenant_id") or ""),
            str(connection.get("kb_id") or ""),
            str(connection.get("connection_id") or ""),
            credential_id,
        )
        if not isinstance(resolved, Mapping):
            raise ValueError("connection credential resolution failed")
        return {str(key): str(value) for key, value in resolved.items()}
    references = connection.get("secret_env")
    if not isinstance(references, Mapping) or not references:
        return {}
    if not allow_environment_secrets:
        raise ValueError(
            "secret_env connections are disabled when authentication is enabled"
        )
    values: dict[str, str] = {}
    for field, raw_env_name in references.items():
        env_name = str(raw_env_name or "")
        value = os.environ.get(env_name, "") if env_name else ""
        if not value:
            raise ValueError(f"connection secret environment is missing: {field}")
        values[str(field)] = value
    return values


def _secret(secrets: Mapping[str, str], field: str) -> str:
    if not isinstance(secrets, Mapping):
        raise ValueError("connection secret references are unavailable")
    value = str(
        secrets.get(field)
        or (secrets.get("access_token") if field == "token" else "")
        or ""
    )
    if not value:
        raise ValueError(f"connection credential is missing: {field}")
    return value


def _transport(*urls: str) -> HttpTransport:
    hosts = {
        str(urlsplit(url).hostname or "").casefold() for url in urls if str(url).strip()
    }
    hosts.discard("")
    if not hosts:
        raise ValueError("connector has no valid HTTPS host")
    return HttpTransport(allowed_hosts=hosts)


def _normalized_hosts(values: Iterable[str]) -> frozenset[str]:
    return frozenset(
        str(value).strip().casefold() for value in values if str(value).strip()
    )


def _trusted_credential_endpoint(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    allow_atlassian_cloud: bool = False,
) -> None:
    parts = urlsplit(url)
    host = str(parts.hostname or "").casefold()
    allowed = _normalized_hosts(allowed_hosts)
    atlassian_cloud = allow_atlassian_cloud and host == "api.atlassian.com"
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.port not in {None, 443}
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or (host not in allowed and not atlassian_cloud)
    ):
        raise ValueError("connector credential endpoint host is not allowed")


def validate_connector_endpoint_policy(
    connector_type: str,
    config: Mapping[str, Any],
    *,
    confluence_allowed_hosts: Iterable[str] = (),
    s3_endpoint_allowed_hosts: Iterable[str] = (),
) -> None:
    kind = str(connector_type or "").casefold()
    if kind == "confluence":
        _trusted_credential_endpoint(
            str(config.get("base_url") or ""),
            confluence_allowed_hosts,
            allow_atlassian_cloud=True,
        )
    elif kind == "s3":
        bucket = str(config.get("bucket") or "")
        region = str(config.get("region") or "")
        if not _S3_BUCKET.fullmatch(bucket) or not _S3_REGION.fullmatch(region):
            raise ValueError("S3 bucket or region is invalid")
        endpoint = str(config.get("endpoint") or "")
        if endpoint:
            _trusted_credential_endpoint(endpoint, s3_endpoint_allowed_hosts)


def _trusted_local_path(
    value: object,
    allowed_roots: Iterable[str],
    *,
    field: str,
) -> Path:
    try:
        candidate = Path(str(value or "")).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{field} is unavailable") from exc
    if not candidate.is_dir():
        raise ValueError(f"{field} must be a directory")
    roots: list[Path] = []
    for raw_root in allowed_roots:
        try:
            root = Path(str(raw_root)).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"configured {field} allowlist root is unavailable"
            ) from exc
        if not root.is_dir():
            raise ValueError(f"configured {field} allowlist root is not a directory")
        roots.append(root)
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise ValueError(f"{field} is outside the server-owned allowlist")
    return candidate


def validate_connector_local_access_policy(
    connector_type: str,
    config: Mapping[str, Any],
    *,
    enforce: bool,
    local_allowed_roots: Iterable[str] = (),
    git_allowed_roots: Iterable[str] = (),
) -> None:
    """Constrain host-filesystem connectors in authenticated deployments."""

    if not enforce:
        return
    kind = str(connector_type or "").casefold()
    if kind == "local-directory":
        if config.get("follow_symlinks") is True:
            raise ValueError(
                "local-directory follow_symlinks is disabled with authentication"
            )
        _trusted_local_path(
            config.get("root"),
            local_allowed_roots,
            field="local connector root",
        )
    elif kind == "git":
        _trusted_local_path(
            config.get("repository"),
            git_allowed_roots,
            field="git connector repository",
        )


def validate_url_connector_host_policy(
    connector_type: str,
    config: Mapping[str, Any],
    *,
    enforce: bool,
    allowed_hosts: Iterable[str] = (),
) -> None:
    if not enforce or str(connector_type or "").casefold() != "url":
        return
    trusted = _normalized_hosts(allowed_hosts)
    urls = config.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("url connector requires a bounded urls list")
    for url in urls:
        parts = urlsplit(str(url))
        host = str(parts.hostname or "").casefold()
        if parts.port not in {None, 443} or host not in trusted:
            raise ValueError("URL connector host is not server-allowed")


def build_connector(
    connection: Mapping[str, Any],
    *,
    secret_resolver: SecretResolver | None = None,
    allow_environment_secrets: bool = True,
    confluence_allowed_hosts: Iterable[str] = (),
    s3_endpoint_allowed_hosts: Iterable[str] = (),
    enforce_local_access_policy: bool = False,
    local_allowed_roots: Iterable[str] = (),
    git_allowed_roots: Iterable[str] = (),
    enforce_url_host_policy: bool = False,
    url_allowed_hosts: Iterable[str] = (),
):
    connector_type = str(connection.get("connector_type") or "").casefold()
    config = connection.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("connection config is unavailable")
    credential_id = str(connection.get("credential_id") or "")
    secret_env = connection.get("secret_env")
    has_secret_env = isinstance(secret_env, Mapping) and bool(secret_env)
    validate_connector_local_access_policy(
        connector_type,
        config,
        enforce=enforce_local_access_policy,
        local_allowed_roots=local_allowed_roots,
        git_allowed_roots=git_allowed_roots,
    )
    validate_url_connector_host_policy(
        connector_type,
        config,
        enforce=enforce_url_host_policy,
        allowed_hosts=url_allowed_hosts,
    )
    if connector_type == "local-directory":
        if credential_id or has_secret_env:
            raise ValueError("local-directory connector does not accept credentials")
        return LocalDirectoryConnector(
            str(config["root"]), follow_symlinks=config.get("follow_symlinks") is True
        )
    if connector_type == "git":
        if credential_id or has_secret_env:
            raise ValueError("git connector does not accept credentials")
        return GitConnector(
            str(config["repository"]),
            ref=str(config.get("ref") or "HEAD"),
            subpath=str(config.get("subpath") or "."),
        )
    if connector_type == "url":
        if credential_id or has_secret_env:
            raise ValueError("url connector does not accept credentials")
        urls = [str(item) for item in config["urls"]]
        return UrlConnector(urls, _transport(*urls))
    if connector_type not in {
        "zotero",
        "notion",
        "confluence",
        "sharepoint",
        "s3",
    }:
        raise ValueError(f"unsupported connector type: {connector_type}")
    if connector_type == "zotero":
        secrets = _resolved_secrets(
            connection,
            secret_resolver,
            allow_environment_secrets=allow_environment_secrets,
        )
        return ZoteroConnector(
            str(config["library_type"]),
            str(config["library_id"]),
            _secret(secrets, "api_key"),
            _transport("https://api.zotero.org", "https://files.zotero.net"),
        )
    if connector_type == "notion":
        secrets = _resolved_secrets(
            connection,
            secret_resolver,
            allow_environment_secrets=allow_environment_secrets,
        )
        return NotionConnector(
            _secret(secrets, "token"), _transport("https://api.notion.com")
        )
    if connector_type == "confluence":
        base_url = str(config["base_url"])
        validate_connector_endpoint_policy(
            connector_type,
            config,
            confluence_allowed_hosts=confluence_allowed_hosts,
        )
        secrets = _resolved_secrets(
            connection,
            secret_resolver,
            allow_environment_secrets=allow_environment_secrets,
        )
        cloud_id = str(secrets.get("cloud_id") or "") or None
        credential_site = str(secrets.get("site_url") or "").rstrip("/") or None
        if (cloud_id is None) != (credential_site is None):
            raise ValueError("Atlassian credential resource binding is incomplete")
        if credential_site is not None and credential_site != base_url.rstrip("/"):
            raise ValueError("Atlassian credential is bound to another site")
        transport_url = (
            "https://api.atlassian.com" if cloud_id is not None else base_url
        )
        return ConfluenceConnector(
            base_url,
            _secret(secrets, "token"),
            _transport(transport_url),
            cloud_id=cloud_id,
            include_acl=config.get("include_acl", True) is True,
        )
    if connector_type == "sharepoint":
        secrets = _resolved_secrets(
            connection,
            secret_resolver,
            allow_environment_secrets=allow_environment_secrets,
        )
        return SharePointConnector(
            str(config["site_id"]),
            str(config["drive_id"]),
            _secret(secrets, "token"),
            _transport("https://graph.microsoft.com"),
            include_acl=config.get("include_acl", True) is True,
        )
    if connector_type == "s3":
        validate_connector_endpoint_policy(
            connector_type,
            config,
            s3_endpoint_allowed_hosts=s3_endpoint_allowed_hosts,
        )
        endpoint = str(config.get("endpoint") or "") or None
        default_endpoint = (
            f"https://{config['bucket']}.s3.{config['region']}.amazonaws.com"
        )
        secrets = _resolved_secrets(
            connection,
            secret_resolver,
            allow_environment_secrets=allow_environment_secrets,
        )
        return S3Connector(
            bucket=str(config["bucket"]),
            region=str(config["region"]),
            access_key=_secret(secrets, "access_key"),
            secret_key=_secret(secrets, "secret_key"),
            session_token=(
                _secret(secrets, "session_token")
                if "session_token" in secrets
                else None
            ),
            prefix=str(config.get("prefix") or ""),
            endpoint=endpoint,
            transport=_transport(endpoint or default_endpoint),
        )
    raise AssertionError("authenticated connector dispatch is incomplete")
