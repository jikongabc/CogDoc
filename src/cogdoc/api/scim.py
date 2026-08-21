"""Bounded SCIM 2.0 configuration and protocol helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping

from cogdoc.api.auth_store import AuthValidationError
from cogdoc.api.tenancy import Role, fingerprint_api_key


SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_MAX_BODY_BYTES = 1_000_000
SCIM_MAX_TOKENS = 100


class SCIMConfigurationError(ValueError):
    pass


class SCIMProtocolError(ValueError):
    def __init__(self, detail: str, *, status: int = 400, scim_type: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.scim_type = scim_type


@dataclass(frozen=True, slots=True)
class SCIMAccess:
    token_fingerprint: str
    workspace_id: str
    issuer: str
    label: str
    default_role: str
    group_role_map: Mapping[str, str]

    def mapped_role(self, display_name: str) -> str | None:
        return self.group_role_map.get(display_name.casefold())


def parse_scim_access_registry(
    raw: str,
    *,
    issuer: str,
    default_role: str = "viewer",
    group_role_map: str = "{}",
) -> dict[str, SCIMAccess]:
    try:
        records = json.loads(raw)
        raw_roles = json.loads(group_role_map or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SCIMConfigurationError("SCIM configuration must be valid JSON") from exc
    if not isinstance(records, list) or not 1 <= len(records) <= SCIM_MAX_TOKENS:
        raise SCIMConfigurationError(
            "SCIM token configuration must be a non-empty list"
        )
    if not isinstance(raw_roles, dict) or len(raw_roles) > 100:
        raise SCIMConfigurationError("SCIM group role map must be an object")
    try:
        clean_default = Role(str(default_role).strip().casefold())
    except ValueError as exc:
        raise SCIMConfigurationError("invalid SCIM default role") from exc
    if clean_default is Role.OWNER:
        raise SCIMConfigurationError("SCIM cannot assign the owner role")
    roles: dict[str, str] = {}
    for name, value in raw_roles.items():
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 256:
            raise SCIMConfigurationError("invalid SCIM role group name")
        try:
            role = Role(str(value).strip().casefold())
        except ValueError as exc:
            raise SCIMConfigurationError("invalid SCIM role group mapping") from exc
        if role is Role.OWNER:
            raise SCIMConfigurationError("SCIM cannot map a group to owner")
        roles[name.strip().casefold()] = role.value
    if not isinstance(issuer, str) or not issuer:
        raise SCIMConfigurationError("SCIM requires an OIDC issuer")
    result: dict[str, SCIMAccess] = {}
    for record in records:
        if not isinstance(record, dict):
            raise SCIMConfigurationError("SCIM token records must be objects")
        token = record.get("token")
        workspace_id = record.get("workspace_id")
        label = record.get("label", "directory")
        if (
            not isinstance(token, str)
            or token != token.strip()
            or not 32 <= len(token) <= 4096
        ):
            raise SCIMConfigurationError(
                "SCIM bearer tokens must contain 32-4096 characters"
            )
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or workspace_id != workspace_id.strip()
            or len(workspace_id) > 160
        ):
            raise SCIMConfigurationError("invalid SCIM workspace_id")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 120:
            raise SCIMConfigurationError("invalid SCIM token label")
        fingerprint = fingerprint_api_key(token)
        if fingerprint in result:
            raise SCIMConfigurationError("duplicate SCIM bearer token")
        result[fingerprint] = SCIMAccess(
            token_fingerprint=fingerprint,
            workspace_id=workspace_id,
            issuer=issuer,
            label=label.strip(),
            default_role=clean_default.value,
            group_role_map=dict(roles),
        )
    return result


_FILTER = re.compile(
    r'^\s*(id|externalId|userName|displayName)\s+eq\s+("(?:\\.|[^"\\])*")\s*$',
    re.IGNORECASE,
)


def parse_filter(value: str | None, *, resource: str) -> tuple[str | None, str | None]:
    if value is None or not value.strip():
        return None, None
    if len(value) > 2048:
        raise SCIMProtocolError(
            "filter exceeds the size limit", scim_type="invalidFilter"
        )
    match = _FILTER.fullmatch(value)
    if match is None:
        raise SCIMProtocolError("unsupported SCIM filter", scim_type="invalidFilter")
    field = match.group(1)
    canonical = {
        item.casefold(): item
        for item in ("id", "externalId", "userName", "displayName")
    }[field.casefold()]
    allowed = (
        {"id", "externalId", "userName"}
        if resource == "User"
        else {"id", "externalId", "displayName"}
    )
    if canonical not in allowed:
        raise SCIMProtocolError("unsupported SCIM filter", scim_type="invalidFilter")
    try:
        decoded = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise SCIMProtocolError(
            "invalid SCIM filter", scim_type="invalidFilter"
        ) from exc
    if not isinstance(decoded, str) or not decoded or len(decoded) > 512:
        raise SCIMProtocolError("invalid SCIM filter", scim_type="invalidFilter")
    return canonical, decoded


def parse_if_match(value: str | None) -> int | None:
    if value is None or not value.strip() or value.strip() == "*":
        return None
    match = re.fullmatch(r'W/"([1-9][0-9]*)"', value.strip())
    if match is None:
        raise SCIMProtocolError(
            "invalid If-Match version", status=412, scim_type="versionMismatch"
        )
    return int(match.group(1))


def scim_error(
    detail: str, *, status: int, scim_type: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "status": str(status),
        "detail": detail,
    }
    if scim_type:
        payload["scimType"] = scim_type
    return payload


def translate_store_error(exc: Exception) -> SCIMProtocolError:
    from cogdoc.api.auth_store import (
        AuthConflictError,
        AuthNotFoundError,
        AuthStoreError,
    )

    if isinstance(exc, AuthNotFoundError):
        return SCIMProtocolError("resource not found", status=404)
    if isinstance(exc, AuthConflictError):
        detail = str(exc)
        status = 412 if "version" in detail else 409
        return SCIMProtocolError(
            "resource version conflict"
            if status == 412
            else "resource conflicts with existing state",
            status=status,
            scim_type="versionMismatch" if status == 412 else "uniqueness",
        )
    if isinstance(exc, AuthValidationError):
        return SCIMProtocolError(
            "invalid SCIM resource", status=400, scim_type="invalidValue"
        )
    if isinstance(exc, AuthStoreError):
        return SCIMProtocolError("directory store unavailable", status=503)
    return SCIMProtocolError("directory service unavailable", status=503)
