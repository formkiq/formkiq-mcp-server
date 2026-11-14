from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .openapi import (
    READ_OPERATION_IDS,
    WRITE_OPERATION_IDS,
    OpenApiCatalog,
    OpenApiProxyError,
    load_openapi_text,
    query_parameters,
    scalar,
    substitute_path_params,
)
from .security import CallerAuthMiddleware, Settings, caller_context, loopback

audit = logging.getLogger("formkiq.audit")
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


class ProxyRuntime:
    def __init__(self, settings: Settings, catalog: OpenApiCatalog, client_factory: Callable[..., httpx.AsyncClient]):
        self.settings = settings
        self.catalog = catalog
        self.client_factory = client_factory
        self.client: httpx.AsyncClient | None = None
        self.users = 0
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(self):
        # HTTP holds one reference for the app lifespan; stdio holds one per process.
        async with self.lock:
            if self.client is None:
                self.client = self.client_factory(
                    timeout=httpx.Timeout(30, connect=10),
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
                await self.client.__aenter__()
            self.users += 1
        try:
            yield
        finally:
            async with self.lock:
                self.users -= 1
                if self.users == 0 and self.client is not None:
                    await self.client.aclose()
                    self.client = None

    def enabled_ids(self) -> frozenset[str]:
        enabled = READ_OPERATION_IDS & self.catalog.operations.keys()
        if self.settings.enable_writes:
            enabled |= WRITE_OPERATION_IDS & self.catalog.operations.keys()
        if self.settings.enable_purge:
            enabled |= {"PurgeDocument"} & self.catalog.operations.keys()
        caller = caller_context.get()
        return frozenset(enabled & caller.allowed_operations if caller else enabled)

    def operation(self, operation_id: str) -> dict[str, Any]:
        if operation_id not in self.enabled_ids():
            raise OpenApiProxyError(
                f"Operation '{operation_id}' is not enabled for this caller; use list_formkiq_operations"
            )
        return self.catalog.operations[operation_id]

    def bounded(self, value: Any) -> Any:
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > self.settings.max_response_bytes:
            raise OpenApiProxyError(
                "Result exceeds FORMKIQ_MAX_RESPONSE_BYTES; request fewer results or use GetDocumentUrl"
            )
        return value

    async def invoke(
        self, operation_id: str, args: dict[str, Any] | None = None, *, mode: str = "read"
    ) -> dict[str, Any]:
        started = time.monotonic()
        caller = caller_context.get()
        status = None
        outcome = "error"
        try:
            operation = self.operation(operation_id)
            expected_ids = (
                READ_OPERATION_IDS if mode == "read" else WRITE_OPERATION_IDS if mode == "write" else {"PurgeDocument"}
            )
            if operation_id not in expected_ids:
                raise OpenApiProxyError(f"Use the dedicated write or purge tool for operation '{operation_id}'")
            args = args if args is not None else {}
            path_args, query_args, header_args = self.catalog.split_args(operation, args)
            if caller and any(p["name"] == "siteId" and p["in"] == "query" for p in operation["parameters"]):
                site = query_args.get("siteId", "default")
                if site not in caller.allowed_sites:
                    raise OpenApiProxyError(
                        "siteId is not authorized for this caller; supply an allowed siteId explicitly"
                    )
                query_args["siteId"] = site
            path = substitute_path_params(operation["path"], path_args)
            if not self.settings.api_endpoint:
                raise OpenApiProxyError("FORMKIQ_API_ENDPOINT_URL is required to call FormKiQ operations")
            token = os.getenv(caller.jwt_env, "").strip() if caller else self.settings.jwt
            if not token:
                raise OpenApiProxyError("A downstream FormKiQ JWT is required for this caller")
            headers = {name: scalar(value) for name, value in header_args.items()}
            headers.update({"Accept": "application/json", "Authorization": f"Bearer {token}"})
            url = self.settings.api_endpoint + path
            if httpx.URL(url).raw_path.decode("ascii") != urlsplit(url).path:
                raise OpenApiProxyError("Path parameters changed the target URL")
            if self.client is None:
                raise OpenApiProxyError("The proxy client lifespan has not started")
            async with self.client.stream(
                operation["method"],
                url,
                params=query_parameters(operation, query_args),
                json=args.get("body"),
                headers=headers,
            ) as response:
                status = response.status_code
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > self.settings.max_response_bytes:
                        raise OpenApiProxyError(
                            f"Upstream response (HTTP {status}) exceeds FORMKIQ_MAX_RESPONSE_BYTES; "
                            "request fewer results or use GetDocumentUrl"
                        )
                    content.extend(chunk)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content:
                    body = None
                elif content_type == "application/json" or content_type.endswith("+json"):
                    try:
                        body = json.loads(content)
                    except (ValueError, UnicodeError) as exc:
                        raise OpenApiProxyError(f"Upstream returned invalid JSON (HTTP {status})") from exc
                elif content_type.startswith("text/") or not content_type:
                    body = content.decode("utf-8", errors="replace")
                else:
                    raise OpenApiProxyError(f"Unsupported upstream content type (HTTP {status}); use GetDocumentUrl")
                if not response.is_success:
                    error = {"operationId": operation_id, "statusCode": status, "ok": False, "body": body}
                    if response.headers.get("retry-after"):
                        error["retryAfter"] = response.headers["retry-after"]
                    raise ToolError(json.dumps(self.bounded(error), ensure_ascii=False))
                result = self.bounded(
                    {
                        "operationId": operation_id,
                        "method": operation["method"],
                        "statusCode": status,
                        "ok": True,
                        "body": body,
                    }
                )
                outcome = "success"
                return result
        except httpx.TimeoutException as exc:
            raise ToolError(
                f"FormKiQ request timed out for {operation_id}; check the operation before retrying a write"
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"FormKiQ transport failed for {operation_id}; check endpoint connectivity") from exc
        except (OpenApiProxyError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        finally:
            audit.info(
                json.dumps(
                    {
                        "event": "formkiq.operation",
                        "caller": caller.id if caller else "local",
                        "operationId": operation_id,
                        "outcome": outcome,
                        "statusCode": status,
                        "durationMs": round((time.monotonic() - started) * 1000),
                    }
                )
            )


class FormKiQMCP(FastMCP):
    def __init__(self, runtime: ProxyRuntime):
        self.runtime = runtime

        @asynccontextmanager
        async def lifespan(_server):
            async with runtime.lifespan():
                yield {}

        super().__init__(
            "FormKiQ OpenAPI Proxy",
            json_response=True,
            stateless_http=True,
            host=runtime.settings.host,
            port=runtime.settings.port,
            lifespan=lifespan,
            instructions="Use describe_operation for argument schemas. Read tools cannot mutate data; writes and purge require separately enabled tools.",
        )
        # httpx INFO logs include complete request URLs and potentially share keys.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def streamable_http_app(self):
        settings = self.runtime.settings
        if not loopback(settings.host) and not settings.callers:
            raise OpenApiProxyError("Non-loopback HTTP requires FORMKIQ_MCP_AUTH_FILE with per-caller policies")
        app = super().streamable_http_app()
        if settings.callers:
            app.add_middleware(CallerAuthMiddleware, policies=settings.callers)
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with self.runtime.lifespan(), original_lifespan(app):
                yield

        app.router.lifespan_context = lifespan
        return app


def create_server(
    settings: Settings | None = None,
    catalog: OpenApiCatalog | None = None,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> FormKiQMCP:
    settings = settings or Settings.from_env()
    catalog = catalog or OpenApiCatalog(
        load_openapi_text(
            os.getenv("FORMKIQ_OPENAPI_FILE", "").strip() or None,
            os.getenv("FORMKIQ_OPENAPI_URL", "").strip() or None,
        )
    )
    runtime = ProxyRuntime(settings, catalog, client_factory)
    mcp = FormKiQMCP(runtime)

    @mcp.resource("openapi://formkiq/jwt")
    async def formkiq_openapi_spec() -> str:
        """The pinned FormKiQ JWT OpenAPI document; prefer describe_operation for focused schemas."""
        return catalog.text

    @mcp.tool(annotations=LOCAL)
    async def list_formkiq_operations() -> list[dict[str, str]]:
        """List operations enabled for this caller. Writes and purge are disabled by default."""
        return [
            {key: catalog.operations[name][key] for key in ("operationId", "method", "path", "summary")}
            for name in sorted(runtime.enabled_ids())
        ]

    @mcp.tool(annotations=LOCAL)
    async def describe_operation(operation_id: str) -> dict[str, Any]:
        """Get an enabled operation's parameters, JSON request schema, examples, and reachable local definitions."""
        runtime.operation(operation_id)
        return runtime.bounded(catalog.describe(operation_id))

    @mcp.tool(annotations=READ)
    async def call_formkiq_operation(operation_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a read/search operation. Use describe_operation first. Args accepts path, query, headers, body, or unambiguous top-level parameter names."""
        return await runtime.invoke(operation_id, args)

    @mcp.tool(annotations=READ)
    async def get_version() -> dict[str, Any]:
        """Get the FormKiQ API version."""
        return await runtime.invoke("GetVersion")

    @mcp.tool(annotations=READ)
    async def get_documents(
        siteId: str | None = None, next: str | None = None, limit: str | None = None
    ) -> dict[str, Any]:
        """List recent documents. Pass the returned next token to retrieve another page."""
        return await runtime.invoke("GetDocuments", {"query": {"siteId": siteId, "next": next, "limit": limit}})

    @mcp.tool(annotations=READ)
    async def get_document(documentId: str, siteId: str | None = None) -> dict[str, Any]:
        """Get document metadata by ID and optional site."""
        return await runtime.invoke("GetDocument", {"path": {"documentId": documentId}, "query": {"siteId": siteId}})

    @mcp.tool(annotations=READ)
    async def search_documents(body: dict[str, Any], siteId: str | None = None) -> dict[str, Any]:
        """Search documents. Use describe_operation('DocumentSearch') for the request schema and examples."""
        return await runtime.invoke("DocumentSearch", {"query": {"siteId": siteId}, "body": body})

    if settings.enable_writes:

        @mcp.tool(annotations=WRITE)
        async def call_formkiq_write_operation(operation_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            """Mutate FormKiQ data, including deletion. Requires explicit write enablement and caller authorization. Purge uses a separate tool."""
            return await runtime.invoke(operation_id, args, mode="write")

    if settings.enable_purge:

        @mcp.tool(annotations=WRITE)
        async def purge_document(documentId: str, siteId: str | None = None) -> dict[str, Any]:
            """Permanently purge a document and its versions. Requires explicit purge and write enablement and caller authorization."""
            return await runtime.invoke(
                "PurgeDocument", {"path": {"documentId": documentId}, "query": {"siteId": siteId}}, mode="purge"
            )

    return mcp


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport not in {"stdio", "streamable-http"}:
        raise OpenApiProxyError("MCP_TRANSPORT must be stdio or streamable-http")
    create_server().run(transport=transport)
