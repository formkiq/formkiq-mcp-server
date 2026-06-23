from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml
from mcp.server.fastmcp import FastMCP


DEFAULT_OPENAPI_URL = (
    "https://raw.githubusercontent.com/formkiq/formkiq-core/refs/heads/master/"
    "docs/openapi/openapi-jwt.yaml"
)

ALLOWED_OPERATION_IDS = {
    "AddAttribute",
    "AddDocumentActions",
    "AddDocumentAttributes",
    "AddDocumentTags",
    "AddDocumentUpload",
    "DeleteAttribute",
    "DeleteDocument",
    "DeleteDocumentAttribute",
    "DeleteDocumentAttributeAndValue",
    "DeleteDocumentTag",
    "DeleteDocumentTagAndValue",
    "DeleteDocumentVersion",
    "DocumentSearch",
    "GetAttribute",
    "GetAttributeAllowedValues",
    "GetAttributes",
    "GetDocument",
    "GetDocumentActions",
    "GetDocumentArtifacts",
    "GetDocumentAttribute",
    "GetDocumentAttributes",
    "GetDocumentContent",
    "GetDocuments",
    "GetDocumentTag",
    "GetDocumentTags",
    "GetDocumentUrl",
    "GetDocumentVersions",
    "GetResourceActivities",
    "GetVersion",
    "PurgeDocument",
    "SearchFulltext",
    "SetDocumentAttributeValue",
    "SetDocumentAttributes",
    "SetDocumentRestore",
    "SetDocumentTag",
    "SetDocumentTags",
    "SetDocumentVersion",
    "UpdateAttribute",
    "UpdateDocument",
    "UpdateDocumentTags",
}

mcp = FastMCP("FormKiQ OpenAPI Proxy", json_response=True)


class OpenApiProxyError(ValueError):
    pass


def openapi_url() -> str:
    return os.getenv("FORMKIQ_OPENAPI_URL", DEFAULT_OPENAPI_URL)


def api_endpoint_url() -> str:
    value = (
        os.getenv("FORMKIQ_API_ENDPOINT_URL", "").strip()
        or os.getenv("FORMKIQ_API_BASE_URL", "").strip()
    ).rstrip("/")
    if not value:
        raise OpenApiProxyError(
            "FORMKIQ_API_ENDPOINT_URL is required to call FormKiQ operations. "
            "Use the FormKiQ CloudFormation HttpApiUrl, IamApiUrl, or KeyApiUrl output."
        )
    return value


def bearer_token() -> str | None:
    token = os.getenv("FORMKIQ_JWT", "").strip()
    return token or None


@lru_cache(maxsize=1)
def load_openapi_text() -> str:
    local_file = os.getenv("FORMKIQ_OPENAPI_FILE", "").strip()
    if local_file:
        return Path(local_file).read_text(encoding="utf-8")

    response = httpx.get(openapi_url(), timeout=20)
    response.raise_for_status()
    return response.text


@lru_cache(maxsize=1)
def load_openapi_spec() -> dict[str, Any]:
    return yaml.safe_load(load_openapi_text())


@lru_cache(maxsize=1)
def operations_by_id() -> dict[str, dict[str, Any]]:
    spec = load_openapi_spec()
    operations: dict[str, dict[str, Any]] = {}

    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue

        path_parameters = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(operation, dict):
                continue

            operation_id = operation.get("operationId")
            if not operation_id:
                continue

            operations[operation_id] = {
                "operationId": operation_id,
                "method": method.upper(),
                "path": path,
                "summary": operation.get("summary", ""),
                "description": operation.get("description", ""),
                "parameters": [*path_parameters, *operation.get("parameters", [])],
                "requestBody": operation.get("requestBody"),
            }

    return operations


def allowed_operation(operation_id: str) -> dict[str, Any]:
    if operation_id not in ALLOWED_OPERATION_IDS:
        allowed = ", ".join(sorted(ALLOWED_OPERATION_IDS))
        raise OpenApiProxyError(f"Operation '{operation_id}' is not allowed. Allowed: {allowed}")

    operation = operations_by_id().get(operation_id)
    if operation is None:
        raise OpenApiProxyError(f"Operation '{operation_id}' was not found in the OpenAPI spec")

    return operation


def substitute_path_params(path_template: str, path_args: dict[str, Any]) -> str:
    path = path_template
    for name in re.findall(r"{([^}]+)}", path_template):
        if name not in path_args:
            raise OpenApiProxyError(f"Missing path parameter: {name}")
        path = path.replace("{" + name + "}", quote(str(path_args[name]), safe=""))
    return path


def operation_parameter_names(operation: dict[str, Any], location: str) -> set[str]:
    names: set[str] = set()
    for parameter in operation.get("parameters", []):
        if isinstance(parameter, dict) and parameter.get("in") == location:
            names.add(parameter["name"])
    return names


def split_args(operation: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    path_names = operation_parameter_names(operation, "path")
    query_names = operation_parameter_names(operation, "query")
    header_names = operation_parameter_names(operation, "header")

    path_args = dict(args.get("path", {}))
    query_args = dict(args.get("query", {}))
    header_args = dict(args.get("headers", {}))

    for name in path_names:
        if name in args:
            path_args[name] = args[name]

    for name in query_names:
        if name in args:
            query_args[name] = args[name]

    for name in header_names:
        if name in args:
            header_args[name] = args[name]

    return path_args, query_args, header_args


def invoke_operation(operation_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    operation = allowed_operation(operation_id)
    args = args or {}

    path_args, query_args, header_args = split_args(operation, args)
    path = substitute_path_params(operation["path"], path_args)

    headers = {
        "Accept": "application/json",
        **{k: str(v) for k, v in header_args.items() if v is not None},
    }
    token = bearer_token()
    if token and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {token}"

    request_body = args.get("body")
    url = f"{api_endpoint_url()}{path}"

    with httpx.Client(timeout=30) as client:
        response = client.request(
            operation["method"],
            url,
            params={k: v for k, v in query_args.items() if v is not None},
            json=request_body,
            headers=headers,
        )

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        body: Any = response.json()
    else:
        body = response.text

    return {
        "operationId": operation_id,
        "method": operation["method"],
        "url": str(response.request.url),
        "statusCode": response.status_code,
        "ok": response.is_success,
        "body": body,
    }


@mcp.resource("openapi://formkiq/jwt")
def formkiq_openapi_spec() -> str:
    """The FormKiQ JWT OpenAPI YAML document."""
    return load_openapi_text()


@mcp.tool()
def list_formkiq_operations() -> list[dict[str, str]]:
    """List the curated FormKiQ OpenAPI operations exposed by this MCP proxy."""
    operations = operations_by_id()
    result = []
    for operation_id in sorted(ALLOWED_OPERATION_IDS):
        operation = operations.get(operation_id, {})
        result.append(
            {
                "operationId": operation_id,
                "method": operation.get("method", ""),
                "path": operation.get("path", ""),
                "summary": operation.get("summary", ""),
            }
        )
    return result


@mcp.tool()
def call_formkiq_operation(operation_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call an allowlisted FormKiQ OpenAPI operation.

    Args can include top-level path/query/header names or nested objects:
    {"path": {"documentId": "..."}, "query": {"siteId": "default"}, "body": {...}}.
    """
    return invoke_operation(operation_id, args)


@mcp.tool()
def get_version() -> dict[str, Any]:
    """Call the FormKiQ GetVersion operation."""
    return invoke_operation("GetVersion")


@mcp.tool()
def get_documents(siteId: str | None = None, next: str | None = None, limit: str | None = None) -> dict[str, Any]:
    """Call the FormKiQ GetDocuments operation."""
    return invoke_operation(
        "GetDocuments",
        {"query": {"siteId": siteId, "next": next, "limit": limit}},
    )


@mcp.tool()
def get_document(documentId: str, siteId: str | None = None) -> dict[str, Any]:
    """Call the FormKiQ GetDocument operation."""
    return invoke_operation(
        "GetDocument",
        {"path": {"documentId": documentId}, "query": {"siteId": siteId}},
    )


@mcp.tool()
def search_documents(body: dict[str, Any], siteId: str | None = None) -> dict[str, Any]:
    """Call the FormKiQ DocumentSearch operation."""
    return invoke_operation(
        "DocumentSearch",
        {"query": {"siteId": siteId}, "body": body},
    )


def main() -> None:
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "streamable-http"))


if __name__ == "__main__":
    main()
