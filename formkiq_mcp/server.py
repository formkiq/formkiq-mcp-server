from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .openapi import (
    OpenApiCatalog,
    OpenApiProxyError,
    load_openapi_text,
    query_parameters,
    scalar,
    substitute_path_params,
)
from .operations import OPERATION_POLICIES, module_capabilities
from .security import CallerAuthMiddleware, Settings, caller_context, loopback
from .tools import operation_tool, tool_name

audit = logging.getLogger("formkiq.audit")
discovery = logging.getLogger("formkiq.discovery")
DISCOVERY_TIMEOUT_SECONDS = 5
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
        self.detected_modules: frozenset[str] = frozenset()
        self.discovery_message = "Module discovery has not run."

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
                try:
                    # Finish discovery once before any nested lifespan can serve tools.
                    await self.discover_modules()
                except BaseException:
                    await self.client.aclose()
                    self.client = None
                    raise
            self.users += 1
        try:
            yield
        finally:
            async with self.lock:
                self.users -= 1
                if self.users == 0 and self.client is not None:
                    await self.client.aclose()
                    self.client = None

    async def discover_modules(self) -> None:
        self.detected_modules = frozenset()
        policy = None
        if not self.settings.auto_discover:
            self.discovery_message = "Auto-discovery is disabled (FORMKIQ_AUTO_DISCOVER=false)."
        elif "GetVersion" not in self.catalog.operations:
            self.discovery_message = "Auto-discovery skipped: the custom spec does not include GetVersion."
        elif not self.settings.api_endpoint:
            self.discovery_message = "Auto-discovery skipped: configure FORMKIQ_API_ENDPOINT_URL."
        elif self.settings.callers and not (
            policy := next((p for p in self.settings.callers if "GetVersion" in p.allowed_operations), None)
        ):
            self.discovery_message = "Auto-discovery skipped: no configured caller is allowed GetVersion."
        elif not (os.getenv(policy.jwt_env, "").strip() if policy else self.settings.jwt):
            self.discovery_message = "Auto-discovery skipped: configure a downstream FormKiQ JWT."
        else:
            context = caller_context.set(policy)
            try:
                async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
                    await self.invoke("GetVersion")
            except TimeoutError:
                self.discovery_message = "Auto-discovery timed out; check endpoint connectivity and retry get_version."
            except ToolError:
                # Upstream error bodies can contain sensitive data; do not log the exception.
                self.discovery_message = "Auto-discovery failed; check the endpoint and JWT, then retry get_version."
            finally:
                caller_context.reset(context)

        if self.discovery_message == "Module discovery completed.":
            discovery.info(
                "%s Detected capabilities: %s",
                self.discovery_message,
                ", ".join(sorted(module_capabilities(self.detected_modules))) or "none",
            )
        else:
            discovery.warning(self.discovery_message)
        capabilities = module_capabilities(self.settings.modules | self.detected_modules)
        missing_groups: dict[str, set[str]] = {}
        for operation_policy in OPERATION_POLICIES.values():
            missing = operation_policy.required_modules - capabilities
            if operation_policy.api_group in self.settings.api_groups and missing:
                missing_groups.setdefault(operation_policy.api_group, set()).update(missing)
        for group, missing in sorted(missing_groups.items()):
            discovery.warning(
                "API group '%s' remains unavailable: required capabilities not discovered: %s. "
                "If installed but unadvertised, declare them in FORMKIQ_MODULES.",
                group,
                ", ".join(sorted(missing)),
            )

    def enabled_ids(self) -> frozenset[str]:
        capabilities = module_capabilities(self.settings.modules | self.detected_modules)
        enabled = {
            name
            for name, policy in OPERATION_POLICIES.items()
            if name in self.catalog.operations
            and policy.api_group in self.settings.api_groups
            and policy.required_modules <= capabilities
            and (policy.access == "read" or self.settings.enable_writes)
            and (policy.access != "purge" or self.settings.enable_purge)
        }
        caller = caller_context.get()
        return frozenset(enabled & caller.allowed_operations if caller else enabled)

    def operation(self, operation_id: str) -> dict[str, Any]:
        if operation_id not in self.enabled_ids():
            policy = OPERATION_POLICIES.get(operation_id)
            caller = caller_context.get()
            if policy and (not caller or operation_id in caller.allowed_operations):
                if policy.api_group not in self.settings.api_groups:
                    raise OpenApiProxyError(
                        f"Operation '{operation_id}' requires the '{policy.api_group}' API group in FORMKIQ_API_GROUPS"
                    )
                missing = policy.required_modules - module_capabilities(self.settings.modules | self.detected_modules)
                if missing:
                    raise OpenApiProxyError(
                        f"Operation '{operation_id}' requires installed capability: {', '.join(sorted(missing))}. "
                        f"{self.discovery_message} Call get_version to refresh discovery, or configure FORMKIQ_MODULES for unadvertised capabilities."
                    )
            raise OpenApiProxyError(
                f"Operation '{operation_id}' is not enabled for this caller; use list_formkiq_operations"
            )
        return self.catalog.operations[operation_id]

    def authorize_sites(self, operation: dict[str, Any], path_args: dict, query_args: dict) -> None:
        caller = caller_context.get()
        if caller is None:
            return
        for parameter in operation["parameters"]:
            if parameter["name"] != "siteId":
                continue
            values = path_args if parameter["in"] == "path" else query_args
            site = values.get("siteId", "default")
            if site not in caller.allowed_sites:
                raise OpenApiProxyError("siteId is not authorized for this caller; supply an allowed siteId explicitly")
            values["siteId"] = site
        if "indexName" in query_args:
            raise OpenApiProxyError(
                "Explicit indexName overrides are disabled for HTTP callers; use the authorized site's index"
            )

    def check_processing_modules(self, operation_id: str, args: dict, query_args: dict) -> None:
        if operation_id not in {"AddDocumentOcr", "GetDocumentOcr"}:
            return
        body = args.get("body") or {}
        requires_textract = (
            body.get("ocrEngine") == "TEXTRACT"
            or bool(body.get("textractQueries"))
            or bool({value.upper() for value in body.get("parseTypes", [])} & {"FORMS", "TABLES", "QUERIES"})
            or query_args.get("outputType") in {"KEY_VALUE", "TABLES"}
        )
        if requires_textract and "textract" not in module_capabilities(self.settings.modules | self.detected_modules):
            raise OpenApiProxyError(
                "These OCR options require the textract module; call get_version or configure FORMKIQ_MODULES"
            )

    def process_body(self, operation_id: str, body: Any) -> Any:
        if operation_id == "GetVersion":
            modules = body.get("modules") if isinstance(body, dict) else None
            self.detected_modules = (
                frozenset(modules)
                if isinstance(modules, list) and all(isinstance(m, str) for m in modules)
                else frozenset()
            )
            self.discovery_message = (
                "Module discovery completed."
                if isinstance(modules, list) and all(isinstance(m, str) for m in modules)
                else "GetVersion did not report a valid modules list."
            )
        caller = caller_context.get()
        if operation_id == "GetSites" and caller:
            if not isinstance(body, dict) or not isinstance(body.get("sites"), list):
                raise OpenApiProxyError("Upstream returned an invalid sites response")
            safe_fields = {"siteId", "title", "status", "roles", "permissions"}
            body = {
                "sites": [
                    {key: value for key, value in site.items() if key in safe_fields}
                    for site in body["sites"]
                    if isinstance(site, dict)
                    and isinstance(site.get("siteId"), str)
                    and site["siteId"] in caller.allowed_sites
                ]
            }
        return body

    def redirect_body(self, response: httpx.Response) -> dict[str, str]:
        location = response.headers.get("location", "")
        if not location or any(ord(char) < 32 or ord(char) == 127 for char in location):
            raise OpenApiProxyError("Upstream download redirect has no valid Location")
        try:
            url = httpx.URL(urljoin(str(response.request.url), location))
        except (ValueError, httpx.InvalidURL) as exc:
            raise OpenApiProxyError("Upstream download redirect contains an invalid URL") from exc
        original = response.request.url
        local_http = (
            url.scheme == "http"
            and loopback(url.host)
            and (url.scheme, url.host, url.port) == (original.scheme, original.host, original.port)
        )
        if not url.host or url.userinfo or (url.scheme != "https" and not local_http):
            raise OpenApiProxyError("Upstream download redirect must use HTTPS without embedded credentials")
        return {"contentUrl": str(url)}

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
            if OPERATION_POLICIES[operation_id].access != mode:
                raise OpenApiProxyError(f"Use the dedicated write or purge tool for operation '{operation_id}'")
            args = args if args is not None else {}
            path_args, query_args, header_args = self.catalog.split_args(operation, args)
            self.authorize_sites(operation, path_args, query_args)
            self.check_processing_modules(operation_id, args, query_args)
            if operation_id == "DeleteDocument" and "deleteType" not in query_args:
                if "softDelete" not in query_args:
                    raise OpenApiProxyError(
                        "DeleteDocument requires explicit deleteType: SOFT_DELETE, HARD_DELETE, or CONTENT_ONLY"
                    )
                query_args["deleteType"] = "SOFT_DELETE" if query_args["softDelete"] else "HARD_DELETE"
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
                if (
                    status in {301, 302, 303, 307, 308}
                    and mode == "read"
                    and str(status) in operation["responseStatusCodes"]
                ):
                    result = self.bounded(
                        {
                            "operationId": operation_id,
                            "method": operation["method"],
                            "statusCode": status,
                            "ok": True,
                            "body": self.redirect_body(response),
                        }
                    )
                    outcome = "success"
                    return result
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > self.settings.max_response_bytes:
                        raise OpenApiProxyError(
                            f"Upstream response (HTTP {status}) exceeds FORMKIQ_MAX_RESPONSE_BYTES; "
                            "request fewer results or use GetDocumentUrl"
                        )
                    content.extend(chunk)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
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
                        "body": self.process_body(operation_id, body),
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
        self.operation_tools = {}
        for operation_id in runtime.catalog.operations:
            tool = operation_tool(
                runtime.catalog, operation_id, READ if OPERATION_POLICIES[operation_id].access == "read" else WRITE
            )
            if tool.definition.name in self.operation_tools:
                raise OpenApiProxyError(f"Duplicate endpoint tool name: {tool.definition.name}")
            self.operation_tools[tool.definition.name] = tool

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
            instructions="Each enabled FormKiQ endpoint has a dedicated tool with its own input schema. Installed modules are discovered automatically at startup when credentials permit. Use list_formkiq_operations and describe_operation for additional permissions and examples. Call get_version to retry or refresh discovery, then refresh the tool list. Read tools cannot mutate data; writes and purge require separately enabled tools. Count results may be truncated lower bounds. Upload URLs and accepted processing requests require separate upload or status checks.",
        )
        # httpx INFO logs include complete request URLs and potentially share keys.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    async def list_tools(self):
        enabled = self.runtime.enabled_ids()
        access = {OPERATION_POLICIES[name].access for name in enabled}
        helpers = await super().list_tools()
        return sorted(
            [tool.definition for tool in self.operation_tools.values() if tool.operation_id in enabled]
            + [
                tool
                for tool in helpers
                if (tool.name != "count_documents" or "DocumentSearch" in enabled)
                and (tool.name != "call_formkiq_operation" or "read" in access)
                and (tool.name != "call_formkiq_write_operation" or "write" in access)
            ],
            key=lambda tool: tool.name,
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        if tool := self.operation_tools.get(name):
            # Discovery is filtered, but never rely on a client's cached tool list for authorization.
            self.runtime.operation(tool.operation_id)
            args = tool.request_args(self.runtime.catalog, arguments)
            return await self.runtime.invoke(tool.operation_id, args, mode=OPERATION_POLICIES[tool.operation_id].access)
        return await super().call_tool(name, arguments)

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
    async def list_formkiq_operations() -> list[dict[str, Any]]:
        """List operations enabled for this caller. Writes and purge are disabled by default."""
        return runtime.bounded(
            [
                {
                    **{
                        key: catalog.operations[name][key]
                        for key in ("operationId", "method", "path", "summary", "requiredPermission")
                    },
                    "apiGroup": OPERATION_POLICIES[name].api_group,
                    "toolName": tool_name(name),
                    "access": OPERATION_POLICIES[name].access,
                    "requiredModules": sorted(OPERATION_POLICIES[name].required_modules),
                }
                for name in sorted(runtime.enabled_ids())
            ]
        )

    @mcp.tool(annotations=LOCAL)
    async def describe_operation(operation_id: str) -> dict[str, Any]:
        """Get an enabled operation's parameters, JSON request schema, examples, and reachable local definitions."""
        runtime.operation(operation_id)
        policy = OPERATION_POLICIES[operation_id]
        return runtime.bounded(
            {
                **catalog.describe(operation_id),
                "apiGroup": policy.api_group,
                "toolName": tool_name(operation_id),
                "access": policy.access,
                "requiredModules": sorted(policy.required_modules),
            }
        )

    @mcp.tool(annotations=READ)
    async def call_formkiq_operation(operation_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a read/search operation. Use describe_operation first. Args accepts path, query, headers, body, or unambiguous top-level parameter names."""
        return await runtime.invoke(operation_id, args)

    @mcp.tool(annotations=READ)
    async def count_documents(body: dict[str, Any], siteId: str | None = None) -> dict[str, Any]:
        """Count matching documents using DocumentSearch. Counts stop at 10,000 or a processing budget. countIsExact=false means the count is a lower bound; do not interpret it as the total or aggregate partial counts."""
        result = await runtime.invoke(
            "DocumentSearch", {"query": {"siteId": siteId, "projection": "COUNT"}, "body": body}
        )
        data = result["body"]
        if (
            not isinstance(data, dict)
            or type(data.get("count")) is not int
            or not 0 <= data["count"] <= 10000
            or type(data.get("truncated")) is not bool
        ):
            raise ToolError("Upstream did not return a valid COUNT response; count projection requires FormKiQ v1.19.2")
        return runtime.bounded({**result, "countIsExact": not data["truncated"]})

    if settings.enable_writes:

        @mcp.tool(annotations=WRITE)
        async def call_formkiq_write_operation(operation_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            """Mutate FormKiQ data, including deletion. Requires explicit write enablement and caller authorization. Purge uses a separate tool."""
            return await runtime.invoke(operation_id, args, mode="write")

    return mcp


def main() -> None:
    # Limit automatic discovery to the launch directory and preserve client/host settings.
    load_dotenv(Path.cwd() / ".env", override=False, interpolate=False)
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport not in {"stdio", "streamable-http"}:
        raise OpenApiProxyError("MCP_TRANSPORT must be stdio or streamable-http")
    create_server().run(transport=transport)
