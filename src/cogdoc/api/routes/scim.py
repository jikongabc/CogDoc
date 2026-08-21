"""SCIM 2.0 Users and Groups provisioning endpoints."""

from __future__ import annotations

from functools import wraps
import json
import re
from typing import Any, Mapping

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from cogdoc.api.offload import run_sync
from cogdoc.api.scim import (
    SCIMAccess,
    SCIM_GROUP_SCHEMA,
    SCIM_LIST_SCHEMA,
    SCIM_MAX_BODY_BYTES,
    SCIM_PATCH_SCHEMA,
    SCIM_USER_SCHEMA,
    SCIMProtocolError,
    parse_filter,
    parse_if_match,
    scim_error,
    translate_store_error,
)


router = APIRouter(prefix="/scim/v2", tags=["scim"])
_MEMBER_FILTER_PATH = re.compile(
    r'^members\s*\[\s*value\s+eq\s+"([^"\\]{1,160})"\s*\]$',
    re.IGNORECASE,
)


class _SCIMRouteError(RuntimeError):
    def __init__(self, error: SCIMProtocolError):
        self.error = error

    def response(self) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if self.error.status == 401 else None
        return JSONResponse(
            status_code=self.error.status,
            content=scim_error(
                self.error.detail,
                status=self.error.status,
                scim_type=self.error.scim_type,
            ),
            headers=headers,
            media_type="application/scim+json",
        )


def _guarded(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except _SCIMRouteError as exc:
            return exc.response()
        except SCIMProtocolError as exc:
            return _SCIMRouteError(exc).response()
        except Exception as exc:
            return _SCIMRouteError(translate_store_error(exc)).response()

    return wrapped


def _access(request: Request) -> SCIMAccess:
    value = getattr(request.state, "scim_access", None)
    if not isinstance(value, SCIMAccess):
        raise SCIMProtocolError("invalid SCIM bearer token", status=401)
    return value


async def _store_call(request: Request, operation: str, **kwargs: Any) -> Any:
    store = getattr(request.app.state, "auth_store", None)
    executor = getattr(request.app.state, "offload_executor", None)
    function = getattr(store, operation, None)
    if (
        not callable(function)
        or executor is None
        or getattr(executor, "_shutdown", False)
    ):
        raise SCIMProtocolError("directory service unavailable", status=503)
    return await run_sync(executor, function, **kwargs)


async def _body(request: Request) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            content_length = int(length)
        except ValueError as exc:
            raise SCIMProtocolError("invalid Content-Length") from exc
        if content_length > SCIM_MAX_BODY_BYTES:
            raise SCIMProtocolError("SCIM request body is too large", status=413)
    raw = await request.body()
    if len(raw) > SCIM_MAX_BODY_BYTES:
        raise SCIMProtocolError("SCIM request body is too large", status=413)
    try:
        payload = json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SCIMProtocolError(
            "SCIM request body must be JSON", scim_type="invalidSyntax"
        ) from exc
    if not isinstance(payload, dict):
        raise SCIMProtocolError(
            "SCIM resource must be an object", scim_type="invalidSyntax"
        )
    return payload


def _schemas(payload: Mapping[str, Any], expected: str) -> None:
    schemas = payload.get("schemas")
    if not isinstance(schemas, list) or expected not in schemas:
        raise SCIMProtocolError(
            "required SCIM schema is missing", scim_type="invalidValue"
        )


def _string(
    payload: Mapping[str, Any], name: str, *, required: bool = False
) -> str | None:
    value = payload.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise SCIMProtocolError(f"invalid {name}", scim_type="invalidValue")
    return value.strip()


def _active(payload: Mapping[str, Any], *, default: bool = True) -> bool:
    value = payload.get("active", default)
    if type(value) is not bool:
        raise SCIMProtocolError("active must be boolean", scim_type="invalidValue")
    return value


def _members(payload: Mapping[str, Any]) -> list[str]:
    values = payload.get("members", [])
    if not isinstance(values, list) or len(values) > 10_000:
        raise SCIMProtocolError("invalid group members", scim_type="invalidValue")
    result: list[str] = []
    for item in values:
        if not isinstance(item, Mapping):
            raise SCIMProtocolError("invalid group member", scim_type="invalidValue")
        value = item.get("value")
        if not isinstance(value, str) or not value or len(value) > 160:
            raise SCIMProtocolError(
                "invalid group member value", scim_type="invalidValue"
            )
        if value not in result:
            result.append(value)
    return result


def _location(request: Request, resource: str, identifier: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/scim/v2/{resource}/{identifier}"


def _pagination(start_index: str | None, count: str | None) -> tuple[int, int]:
    try:
        start = 1 if start_index is None else int(start_index)
        size = 100 if count is None else int(count)
    except (TypeError, ValueError) as exc:
        raise SCIMProtocolError(
            "invalid SCIM pagination", scim_type="invalidValue"
        ) from exc
    if start < 1 or not 0 <= size <= 200:
        raise SCIMProtocolError("invalid SCIM pagination", scim_type="invalidValue")
    return start, size


def _resource_type_payload(request: Request, name: str, schema: str) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": name,
        "name": name,
        "endpoint": f"/{name}s",
        "schema": schema,
        "meta": {
            "resourceType": "ResourceType",
            "location": f"{base}/scim/v2/ResourceTypes/{name}",
        },
    }


def _schema_payload(request: Request, name: str, schema: str) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    common = [
        {
            "name": "externalId",
            "type": "string",
            "multiValued": False,
            "required": False,
            "mutability": "readWrite",
            "returned": "default",
            "uniqueness": "server",
        }
    ]
    if name == "User":
        attributes = [
            *common,
            {
                "name": "userName",
                "type": "string",
                "multiValued": False,
                "required": True,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "server",
            },
            {
                "name": "displayName",
                "type": "string",
                "multiValued": False,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "none",
            },
            {
                "name": "active",
                "type": "boolean",
                "multiValued": False,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "none",
            },
        ]
    else:
        attributes = [
            *common,
            {
                "name": "displayName",
                "type": "string",
                "multiValued": False,
                "required": True,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "server",
            },
            {
                "name": "members",
                "type": "complex",
                "multiValued": True,
                "required": False,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": "none",
                "subAttributes": [
                    {"name": "value", "type": "string", "required": True},
                    {"name": "$ref", "type": "reference", "required": False},
                    {"name": "type", "type": "string", "required": False},
                ],
            },
        ]
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
        "id": schema,
        "name": name,
        "description": f"CogDoc SCIM {name}",
        "attributes": attributes,
        "meta": {
            "resourceType": "Schema",
            "location": f"{base}/scim/v2/Schemas/{schema}",
        },
    }


def _user_resource(request: Request, row: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(row["id"])
    user_name = str(row["user_name"])
    revision = int(row["revision"])
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": identifier,
        **({"externalId": row["external_id"]} if row.get("external_id") else {}),
        "userName": user_name,
        "displayName": str(row["display_name"]),
        "active": bool(row["active"]),
        "emails": [{"value": user_name, "type": "work", "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": str(row["created_at"]),
            "lastModified": str(row["updated_at"]),
            "version": f'W/"{revision}"',
            "location": _location(request, "Users", identifier),
        },
    }


def _group_resource(request: Request, row: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(row["id"])
    revision = int(row["revision"])
    return {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": identifier,
        **({"externalId": row["external_id"]} if row.get("external_id") else {}),
        "displayName": str(row["display_name"]),
        "members": [
            {
                "value": str(member),
                "$ref": _location(request, "Users", str(member)),
                "type": "User",
            }
            for member in row.get("members", [])
        ],
        "meta": {
            "resourceType": "Group",
            "created": str(row["created_at"]),
            "lastModified": str(row["updated_at"]),
            "version": f'W/"{revision}"',
            "location": _location(request, "Groups", identifier),
        },
    }


def _resource_response(payload: dict[str, Any], *, status: int = 200) -> JSONResponse:
    meta = payload.get("meta", {})
    headers = {
        "ETag": str(meta.get("version", "")),
        "Location": str(meta.get("location", "")),
    }
    return JSONResponse(
        status_code=status,
        content=payload,
        headers=headers,
        media_type="application/scim+json",
    )


@router.get("/ServiceProviderConfig")
@_guarded
async def service_provider_config(request: Request):
    _access(request)
    return JSONResponse(
        content={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": True},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer Token",
                    "description": "Dedicated server-managed SCIM bearer token",
                    "specUri": "https://www.rfc-editor.org/rfc/rfc6750",
                    "primary": True,
                }
            ],
        },
        media_type="application/scim+json",
    )


@router.get("/ResourceTypes")
@_guarded
async def resource_types(request: Request):
    _access(request)
    resources = [
        _resource_type_payload(request, name, schema)
        for name, schema in (("User", SCIM_USER_SCHEMA), ("Group", SCIM_GROUP_SCHEMA))
    ]
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": 2,
            "Resources": resources,
        },
        media_type="application/scim+json",
    )


@router.get("/ResourceTypes/{resource_name}")
@_guarded
async def resource_type(resource_name: str, request: Request):
    _access(request)
    schemas_by_name = {"User": SCIM_USER_SCHEMA, "Group": SCIM_GROUP_SCHEMA}
    schema = schemas_by_name.get(resource_name)
    if schema is None:
        raise SCIMProtocolError("resource type not found", status=404)
    return JSONResponse(
        content=_resource_type_payload(request, resource_name, schema),
        media_type="application/scim+json",
    )


@router.get("/Schemas")
@_guarded
async def schemas(request: Request):
    _access(request)
    resources = [
        _schema_payload(request, name, schema)
        for name, schema in (("User", SCIM_USER_SCHEMA), ("Group", SCIM_GROUP_SCHEMA))
    ]
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": 2,
            "Resources": resources,
        },
        media_type="application/scim+json",
    )


@router.get("/Schemas/{schema_id:path}")
@_guarded
async def schema(schema_id: str, request: Request):
    _access(request)
    names_by_schema = {SCIM_USER_SCHEMA: "User", SCIM_GROUP_SCHEMA: "Group"}
    name = names_by_schema.get(schema_id)
    if name is None:
        raise SCIMProtocolError("schema not found", status=404)
    return JSONResponse(
        content=_schema_payload(request, name, schema_id),
        media_type="application/scim+json",
    )


@router.get("/Users")
@_guarded
async def list_users(
    request: Request,
    filter: str | None = None,
    startIndex: str | None = None,
    count: str | None = None,
):
    access = _access(request)
    start_index, page_size = _pagination(startIndex, count)
    field, value = parse_filter(filter, resource="User")
    total, rows = await _store_call(
        request,
        "list_scim_users",
        workspace_id=access.workspace_id,
        filter_field=field,
        filter_value=value,
        start_index=start_index,
        count=page_size,
    )
    resources = [_user_resource(request, row) for row in rows]
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": resources,
        },
        media_type="application/scim+json",
    )


@router.post("/Users")
@_guarded
async def create_user(request: Request):
    access, payload = _access(request), await _body(request)
    _schemas(payload, SCIM_USER_SCHEMA)
    row = await _store_call(
        request,
        "create_scim_user",
        workspace_id=access.workspace_id,
        issuer=access.issuer,
        external_id=_string(payload, "externalId"),
        user_name=_string(payload, "userName", required=True),
        display_name=_string(payload, "displayName")
        or _string(payload, "userName", required=True),
        active=_active(payload),
        base_role=access.default_role,
    )
    return _resource_response(_user_resource(request, row), status=201)


@router.get("/Users/{user_id}")
@_guarded
async def get_user(user_id: str, request: Request):
    access = _access(request)
    row = await _store_call(
        request, "get_scim_user", workspace_id=access.workspace_id, scim_user_id=user_id
    )
    return _resource_response(_user_resource(request, row))


async def _replace_user(request: Request, user_id: str, payload: Mapping[str, Any]):
    access = _access(request)
    current = await _store_call(
        request, "get_scim_user", workspace_id=access.workspace_id, scim_user_id=user_id
    )
    expected = parse_if_match(request.headers.get("if-match"))
    row = await _store_call(
        request,
        "update_scim_user",
        workspace_id=access.workspace_id,
        scim_user_id=user_id,
        external_id=payload.get("externalId", current["external_id"]),
        user_name=payload.get("userName", current["user_name"]),
        display_name=payload.get("displayName", current["display_name"]),
        active=payload.get("active", current["active"]),
        base_role=current["base_role"],
        expected_revision=expected if expected is not None else current["revision"],
    )
    return _resource_response(_user_resource(request, row))


@router.put("/Users/{user_id}")
@_guarded
async def replace_user(user_id: str, request: Request):
    payload = await _body(request)
    _schemas(payload, SCIM_USER_SCHEMA)
    return await _replace_user(request, user_id, payload)


@router.patch("/Users/{user_id}")
@_guarded
async def patch_user(user_id: str, request: Request):
    payload = await _body(request)
    _schemas(payload, SCIM_PATCH_SCHEMA)
    operations = payload.get("Operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise SCIMProtocolError("invalid PATCH operations", scim_type="invalidSyntax")
    access = _access(request)
    current = dict(
        await _store_call(
            request,
            "get_scim_user",
            workspace_id=access.workspace_id,
            scim_user_id=user_id,
        )
    )
    for operation in operations:
        if not isinstance(operation, Mapping) or str(
            operation.get("op", "")
        ).casefold() not in {"add", "replace"}:
            raise SCIMProtocolError(
                "unsupported user PATCH operation", scim_type="invalidValue"
            )
        path = operation.get("path")
        value = operation.get("value")
        if path is None and isinstance(value, Mapping):
            for key in ("externalId", "userName", "displayName", "active"):
                if key in value:
                    current[
                        {
                            "externalId": "external_id",
                            "userName": "user_name",
                            "displayName": "display_name",
                            "active": "active",
                        }[key]
                    ] = value[key]
        elif isinstance(path, str) and path.casefold() in {
            "externalid",
            "username",
            "displayname",
            "active",
        }:
            key = {
                "externalid": "external_id",
                "username": "user_name",
                "displayname": "display_name",
                "active": "active",
            }[path.casefold()]
            current[key] = value
        else:
            raise SCIMProtocolError(
                "unsupported user PATCH path", scim_type="invalidPath"
            )
    return await _replace_user(
        request,
        user_id,
        {
            "externalId": current["external_id"],
            "userName": current["user_name"],
            "displayName": current["display_name"],
            "active": current["active"],
        },
    )


@router.delete("/Users/{user_id}", status_code=204)
@_guarded
async def delete_user(user_id: str, request: Request):
    access = _access(request)
    current = await _store_call(
        request, "get_scim_user", workspace_id=access.workspace_id, scim_user_id=user_id
    )
    expected = parse_if_match(request.headers.get("if-match"))
    await _store_call(
        request,
        "delete_scim_user",
        workspace_id=access.workspace_id,
        scim_user_id=user_id,
        expected_revision=expected if expected is not None else current["revision"],
    )
    return Response(status_code=204)


@router.get("/Groups")
@_guarded
async def list_groups(
    request: Request,
    filter: str | None = None,
    startIndex: str | None = None,
    count: str | None = None,
):
    access = _access(request)
    start_index, page_size = _pagination(startIndex, count)
    field, value = parse_filter(filter, resource="Group")
    total, rows = await _store_call(
        request,
        "list_scim_groups",
        workspace_id=access.workspace_id,
        filter_field=field,
        filter_value=value,
        start_index=start_index,
        count=page_size,
    )
    resources = [_group_resource(request, row) for row in rows]
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": resources,
        },
        media_type="application/scim+json",
    )


@router.post("/Groups")
@_guarded
async def create_group(request: Request):
    access, payload = _access(request), await _body(request)
    _schemas(payload, SCIM_GROUP_SCHEMA)
    display_name = _string(payload, "displayName", required=True)
    row = await _store_call(
        request,
        "create_scim_group",
        workspace_id=access.workspace_id,
        external_id=_string(payload, "externalId"),
        display_name=display_name,
        mapped_role=access.mapped_role(str(display_name)),
        member_ids=_members(payload),
    )
    return _resource_response(_group_resource(request, row), status=201)


@router.get("/Groups/{group_id}")
@_guarded
async def get_group(group_id: str, request: Request):
    access = _access(request)
    row = await _store_call(
        request,
        "get_scim_group",
        workspace_id=access.workspace_id,
        scim_group_id=group_id,
    )
    return _resource_response(_group_resource(request, row))


async def _replace_group(request: Request, group_id: str, payload: Mapping[str, Any]):
    access = _access(request)
    current = await _store_call(
        request,
        "get_scim_group",
        workspace_id=access.workspace_id,
        scim_group_id=group_id,
    )
    display_name = payload.get("displayName", current["display_name"])
    members = payload.get("members")
    member_ids = (
        current["members"] if members is None else _members({"members": members})
    )
    expected = parse_if_match(request.headers.get("if-match"))
    row = await _store_call(
        request,
        "update_scim_group",
        workspace_id=access.workspace_id,
        scim_group_id=group_id,
        external_id=payload.get("externalId", current["external_id"]),
        display_name=display_name,
        mapped_role=access.mapped_role(str(display_name)),
        member_ids=member_ids,
        expected_revision=expected if expected is not None else current["revision"],
    )
    return _resource_response(_group_resource(request, row))


@router.put("/Groups/{group_id}")
@_guarded
async def replace_group(group_id: str, request: Request):
    payload = await _body(request)
    _schemas(payload, SCIM_GROUP_SCHEMA)
    return await _replace_group(request, group_id, payload)


@router.patch("/Groups/{group_id}")
@_guarded
async def patch_group(group_id: str, request: Request):
    payload = await _body(request)
    _schemas(payload, SCIM_PATCH_SCHEMA)
    operations = payload.get("Operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise SCIMProtocolError("invalid PATCH operations", scim_type="invalidSyntax")
    access = _access(request)
    current = dict(
        await _store_call(
            request,
            "get_scim_group",
            workspace_id=access.workspace_id,
            scim_group_id=group_id,
        )
    )
    members = list(current["members"])
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise SCIMProtocolError(
                "invalid group PATCH operation", scim_type="invalidSyntax"
            )
        op = str(operation.get("op", "")).casefold()
        path, value = operation.get("path"), operation.get("value")
        member_filter = (
            _MEMBER_FILTER_PATH.fullmatch(path) if isinstance(path, str) else None
        )
        if member_filter is not None and op == "remove":
            removed = member_filter.group(1)
            members = [member for member in members if member != removed]
        elif isinstance(path, str) and path.casefold() == "members":
            incoming = _members({"members": value})
            if op == "add":
                members = list(dict.fromkeys([*members, *incoming]))
            elif op == "replace":
                members = incoming
            elif op == "remove":
                removed = set(incoming)
                members = [member for member in members if member not in removed]
            else:
                raise SCIMProtocolError(
                    "unsupported group PATCH operation", scim_type="invalidValue"
                )
        elif (
            isinstance(path, str)
            and path.casefold() in {"displayname", "externalid"}
            and op in {"add", "replace"}
        ):
            current[
                "display_name" if path.casefold() == "displayname" else "external_id"
            ] = value
        else:
            raise SCIMProtocolError(
                "unsupported group PATCH path", scim_type="invalidPath"
            )
    return await _replace_group(
        request,
        group_id,
        {
            "displayName": current["display_name"],
            "externalId": current["external_id"],
            "members": [{"value": member} for member in members],
        },
    )


@router.delete("/Groups/{group_id}", status_code=204)
@_guarded
async def delete_group(group_id: str, request: Request):
    access = _access(request)
    current = await _store_call(
        request,
        "get_scim_group",
        workspace_id=access.workspace_id,
        scim_group_id=group_id,
    )
    expected = parse_if_match(request.headers.get("if-match"))
    await _store_call(
        request,
        "delete_scim_group",
        workspace_id=access.workspace_id,
        scim_group_id=group_id,
        expected_revision=expected if expected is not None else current["revision"],
    )
    return Response(status_code=204)
