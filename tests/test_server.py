from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from formkiq_mcp.openapi import (
    ALLOWED_OPERATION_IDS,
    READ_OPERATION_IDS,
    OpenApiCatalog,
    OpenApiProxyError,
    load_openapi_text,
    query_parameters,
    substitute_path_params,
)
from formkiq_mcp.security import CallerPolicy, Settings, caller_context, load_policies
from formkiq_mcp.server import create_server

CATALOG = OpenApiCatalog(load_openapi_text())


async def call(server, name, arguments=None):
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments or {}))
    return (await handler(request)).root


class CatalogTests(unittest.TestCase):
    def test_bundled_spec_has_all_operations_and_matches_checksum(self):
        self.assertEqual(CATALOG.operations.keys(), ALLOWED_OPERATION_IDS)
        provenance = json.loads(Path("formkiq_mcp/data/provenance.json").read_text())
        self.assertEqual(hashlib.sha256(CATALOG.text.encode()).hexdigest(), provenance["sha256"])
        self.assertNotIn("/master/", provenance["source"])

    def test_referenced_top_level_parameters(self):
        path, query, _ = CATALOG.split_args(
            CATALOG.operations["GetDocument"],
            {"documentId": "doc-123", "siteId": "tenant-a"},
        )
        self.assertEqual(path, {"documentId": "doc-123"})
        self.assertEqual(query, {"siteId": "tenant-a"})

    def test_operation_parameters_override_path_parameters(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/documents": {
                    "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                    "get": {
                        "operationId": "GetDocuments",
                        "parameters": [
                            {"name": "limit", "in": "query", "schema": {"type": "string"}},
                        ],
                    },
                }
            },
        }
        catalog = OpenApiCatalog(yaml.safe_dump(spec), frozenset({"GetDocuments"}))
        operation = catalog.operations["GetDocuments"]
        self.assertEqual(len(operation["parameters"]), 1)
        self.assertEqual(catalog.split_args(operation, {"limit": "5"})[1], {"limit": "5"})

    def test_path_values_are_encoded_without_changing_route(self):
        self.assertEqual(
            substitute_path_params("/documents/{id}", {"id": "a/b?#雪"}), "/documents/a%2Fb%3F%23%E9%9B%AA"
        )
        for value in (".", "..", "", None, {}, []):
            with self.subTest(value=value), self.assertRaises(OpenApiProxyError):
                substitute_path_params("/documents/{id}", {"id": value})

    def test_rejects_unknown_conflicting_and_malformed_arguments(self):
        operation = CATALOG.operations["GetDocument"]
        for args in (
            {"documentId": "x", "sietId": "a"},
            {"path": {"documentId": "x", "extra": "y"}},
            {"path": {"documentId": "x"}, "documentId": "y"},
            {"path": None},
            {"path": {"documentId": None}},
            {"documentId": "x", "headers": {"Authorization": "secret"}},
            {"documentId": "x", "body": {}},
        ):
            with self.subTest(args=args), self.assertRaises(OpenApiProxyError):
                CATALOG.split_args(operation, args)

    def test_required_body_nested_schema_and_parameter_type_validation(self):
        operation = CATALOG.operations["DocumentSearch"]
        for args in ({}, {"body": None}, {"body": {"query": "wrong"}}, {"limit": 5, "body": {}}):
            with self.subTest(args=args), self.assertRaises(OpenApiProxyError):
                CATALOG.split_args(operation, args)
        CATALOG.split_args(operation, {"body": {"query": {"text": "invoice"}}})

    def test_description_contains_reachable_definitions_and_examples(self):
        description = CATALOG.describe("DocumentSearch")
        self.assertIn("DocumentSearchRequest", description["components"]["schemas"])
        self.assertIn("examples", description["requestBody"]["content"]["application/json"])
        self.assertNotIn("paths", description)
        self.assertLess(len(json.dumps(description)), 65_536)

    def test_missing_duplicate_external_and_unresolved_references_fail(self):
        base = {"openapi": "3.0.3", "paths": {"/version": {"get": {"operationId": "GetVersion"}}}}
        with self.assertRaisesRegex(OpenApiProxyError, "missing allowlisted"):
            OpenApiCatalog(yaml.safe_dump(base))
        duplicate = copy.deepcopy(base)
        duplicate["paths"]["/other"] = {"get": {"operationId": "GetVersion"}}
        with self.assertRaisesRegex(OpenApiProxyError, "Duplicate"):
            OpenApiCatalog(yaml.safe_dump(duplicate), frozenset({"GetVersion"}))
        for ref in ("https://example.test/schema", "#/missing"):
            spec = copy.deepcopy(base)
            spec["paths"]["/version"]["get"]["parameters"] = [{"$ref": ref}]
            with self.subTest(ref=ref), self.assertRaises(OpenApiProxyError):
                OpenApiCatalog(yaml.safe_dump(spec), frozenset({"GetVersion"}))

    def test_parameter_serialization(self):
        operation = {
            "parameters": [
                {"name": "keys", "in": "query", "style": "form", "explode": False},
                {"name": "flags", "in": "query"},
            ]
        }
        self.assertEqual(
            query_parameters(operation, {"keys": ["a", "b"], "flags": [True, False]}),
            [("keys", "a,b"), ("flags", "true"), ("flags", "false")],
        )

    def test_nullable_and_recursive_schemas_remain_usable(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {},
            "components": {
                "schemas": {
                    "Node": {
                        "type": "object",
                        "properties": {
                            "nullable": {"type": "integer"},
                            "label": {"type": "string", "nullable": True},
                            "child": {"$ref": "#/components/schemas/Node"},
                        },
                    }
                }
            },
        }
        catalog = OpenApiCatalog(yaml.safe_dump(spec), frozenset())
        schema = {"$ref": "#/components/schemas/Node"}
        catalog.validate(schema, {"label": None, "nullable": 3, "child": {"label": "ok"}}, "body")
        with self.assertRaises(OpenApiProxyError):
            catalog.validate(schema, {"nullable": "wrong"}, "body")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.body = {"version": "test"}
        self.response_headers = {}
        self.clients = []

        async def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.body, headers=self.response_headers)

        self.handler = handler
        self.server = await self.make_server()

    async def make_server(self, **settings):
        settings.setdefault("auto_discover", False)

        def factory(**kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kwargs)
            self.clients.append(client)
            return client

        server = create_server(
            Settings(api_endpoint="https://formkiq.example.test", jwt="downstream-secret", **settings),
            CATALOG,
            factory,
        )
        lifespan = server.runtime.lifespan()
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        return server

    async def test_top_level_arguments_reach_backend(self):
        result = await call(
            self.server,
            "call_formkiq_operation",
            {
                "operation_id": "GetDocument",
                "args": {"documentId": "doc-123", "siteId": "tenant-a"},
            },
        )
        self.assertFalse(result.isError)
        self.assertEqual(self.requests[-1].url.path, "/documents/doc-123")
        self.assertEqual(self.requests[-1].url.params["siteId"], "tenant-a")
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer downstream-secret")
        self.assertNotIn("url", result.structuredContent)

    async def test_default_tools_and_operations_are_read_only(self):
        tools = await self.server.list_tools()
        self.assertEqual(len(tools), 29)
        self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
        result = await call(self.server, "list_formkiq_operations")
        enabled = {op["operationId"] for op in result.structuredContent["result"]}
        self.assertEqual(len(enabled), 25)
        self.assertTrue({"GetSites", "GetPermissionCatalog", "GetSitesSchema", "IndexSearch"} <= enabled)
        self.assertFalse({"SearchFulltext", "GetResourceActivities", "GetDocumentIdUpload", "GetWorkflow"} & enabled)
        self.assertTrue(enabled <= READ_OPERATION_IDS)
        for operation_id in ("DeleteDocument", "PurgeDocument"):
            result = await call(self.server, "call_formkiq_operation", {"operation_id": operation_id})
            self.assertTrue(result.isError)
        self.assertEqual(self.requests, [])

    async def test_delete_tag_traversal_never_sends_request(self):
        server = await self.make_server(enable_writes=True)
        for key in ("..", ".", "", None):
            result = await call(
                server,
                "call_formkiq_write_operation",
                {
                    "operation_id": "DeleteDocumentTag",
                    "args": {"path": {"documentId": "doc-123", "tagKey": key}},
                },
            )
            self.assertTrue(result.isError, key)
        self.assertEqual(self.requests, [])

    async def test_write_and_purge_require_their_dedicated_tools(self):
        server = await self.make_server(enable_writes=True, enable_purge=True)
        args = {"operation_id": "DeleteDocument", "args": {"documentId": "doc-123", "deleteType": "SOFT_DELETE"}}
        self.assertTrue((await call(server, "call_formkiq_operation", args)).isError)
        self.assertFalse((await call(server, "call_formkiq_write_operation", args)).isError)
        self.assertEqual(self.requests[-1].method, "DELETE")
        self.assertFalse((await call(server, "purge_document", {"documentId": "doc-123"})).isError)
        self.assertEqual(self.requests[-1].url.path, "/documents/doc-123/purge")
        self.assertTrue((await call(server, "call_formkiq_write_operation", {"operation_id": "PurgeDocument"})).isError)

    async def test_invalid_arguments_never_reach_backend(self):
        for arguments in (
            {"operation_id": "DocumentSearch"},
            {"operation_id": "GetDocument", "args": {"documentId": None}},
            {"operation_id": "GetDocuments", "args": {"limti": "5"}},
            {"operation_id": "GetDocuments", "args": {"query": None}},
        ):
            self.assertTrue((await call(self.server, "call_formkiq_operation", arguments)).isError)
        self.assertEqual(self.requests, [])

    async def test_upstream_failures_set_mcp_error_and_preserve_status(self):
        for status in (401, 403, 429, 500):
            self.status = status
            self.body = {"message": "upstream failure"}
            self.response_headers = {"Retry-After": "10"}
            result = await call(self.server, "get_version")
            self.assertTrue(result.isError)
            self.assertIn(f'"statusCode": {status}', result.content[0].text)
            self.assertIn('"retryAfter": "10"', result.content[0].text)

    async def test_streamed_response_limit_and_connection_cleanup(self):
        class Chunks(httpx.AsyncByteStream):
            def __init__(self):
                self.read = 0
                self.closed = False

            async def __aiter__(self):
                for _ in range(100):
                    self.read += 1
                    yield b"x" * 512

            async def aclose(self):
                self.closed = True

        chunks = Chunks()
        self.handler = lambda request: httpx.Response(200, stream=chunks, headers={"content-type": "text/plain"})
        server = await self.make_server(max_response_bytes=1024)
        result = await call(server, "get_version")
        self.assertTrue(result.isError)
        self.assertIn("MAX_RESPONSE_BYTES", result.content[0].text)
        self.assertEqual(chunks.read, 3)
        self.assertTrue(chunks.closed)

    async def test_content_types_empty_response_invalid_json_and_timeout(self):
        cases = [
            (
                httpx.Response(
                    200, content=b'{"version":"test"}', headers={"content-type": "application/vnd.test+json"}
                ),
                False,
            ),
            (httpx.Response(204), False),
            (httpx.Response(200, content=b"broken", headers={"content-type": "application/json"}), True),
            (httpx.Response(200, content=b"binary", headers={"content-type": "application/octet-stream"}), True),
        ]
        for response, is_error in cases:
            self.handler = lambda request, response=response: response
            server = await self.make_server()
            self.assertEqual((await call(server, "get_version")).isError, is_error)

        def timeout(request):
            raise httpx.ReadTimeout("sensitive request details", request=request)

        self.handler = timeout
        server = await self.make_server()
        result = await call(server, "get_version")
        self.assertTrue(result.isError)
        self.assertIn("timed out", result.content[0].text)
        self.assertNotIn("sensitive", result.content[0].text)

    async def test_concurrent_requests_share_client_without_blocking_event_loop(self):
        active = 0
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            nonlocal active
            active += 1
            if active == 3:
                all_started.set()
            await release.wait()
            return httpx.Response(200, json={"version": "test"})

        self.handler = handler
        server = await self.make_server()
        initial_clients = len(self.clients)
        tasks = [asyncio.create_task(call(server, "get_version")) for _ in range(3)]
        try:
            await asyncio.wait_for(all_started.wait(), 2)
            self.assertEqual(active, 3)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(len(self.clients), initial_clients)

    async def test_audit_logs_include_outcome_without_request_secrets(self):
        with self.assertLogs("formkiq.audit", level="INFO") as logs:
            await call(
                self.server,
                "call_formkiq_operation",
                {
                    "operation_id": "GetDocument",
                    "args": {"documentId": "private-id", "shareKey": "private-share-key"},
                },
            )
        output = "\n".join(logs.output)
        self.assertIn('"outcome": "success"', output)
        for secret in ("downstream-secret", "private-id", "private-share-key"):
            self.assertNotIn(secret, output)


class PolicyTests(unittest.TestCase):
    def test_endpoint_and_configuration_validation(self):
        for endpoint in (
            "http://public.example",
            "https://user:secret@example.test",
            "https://example.test?key=x",
            "relative",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(OpenApiProxyError):
                Settings(api_endpoint=endpoint)
        self.assertEqual(Settings(api_endpoint="http://127.0.0.1:8080/").api_endpoint, "http://127.0.0.1:8080")
        for values in ({"enable_purge": True}, {"max_response_bytes": 0}, {"port": -1}):
            with self.assertRaises(OpenApiProxyError):
                Settings(**values)
        with patch.dict(os.environ, {"FORMKIQ_ENABLE_WRITES": "maybe"}), self.assertRaises(OpenApiProxyError):
            Settings.from_env()

    def test_non_loopback_http_requires_caller_policies(self):
        server = create_server(Settings(host="0.0.0.0"), CATALOG)
        with self.assertRaisesRegex(OpenApiProxyError, "AUTH_FILE"):
            server.streamable_http_app()

    def test_policy_file_validation(self):
        client = {
            "id": "analyst",
            "token_sha256": hashlib.sha256(b"mcp-token").hexdigest(),
            "jwt_env": "TEST_FORMKIQ_JWT",
            "allowed_operations": ["GetDocument"],
            "allowed_sites": ["tenant-a"],
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEST_FORMKIQ_JWT": "test-jwt"}):
            path = Path(directory) / "auth.json"
            path.write_text(json.dumps({"clients": [client]}))
            self.assertEqual(load_policies(str(path))[0].id, "analyst")
            for clients in (
                [client, client],
                [{**client, "allowed_operations": ["Unknown"]}],
                [{**client, "token_sha256": "bad"}],
            ):
                path.write_text(json.dumps({"clients": clients}))
                with self.assertRaises(OpenApiProxyError):
                    load_policies(str(path))


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_authentication_authorization_and_caller_isolation(self):
        policies = tuple(
            CallerPolicy(
                id=name,
                token_sha256=hashlib.sha256(f"token-{name}".encode()).hexdigest(),
                jwt_env=f"TEST_JWT_{name.upper()}",
                allowed_operations=frozenset({"GetDocuments"}),
                allowed_sites=frozenset({f"site-{name}"}),
            )
            for name in ("alice", "bob")
        )
        requests = []
        clients = []

        async def upstream(request):
            requests.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"documents": []})

        def factory(**kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), **kwargs)
            clients.append(client)
            return client

        server = create_server(
            Settings(api_endpoint="https://formkiq.example.test", callers=policies), CATALOG, factory
        )
        app = server.streamable_http_app()
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}

        async def rpc(client, token, name, arguments=None):
            return await client.post(
                "/mcp",
                headers={**headers, "Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                },
            )

        with patch.dict(os.environ, {"TEST_JWT_ALICE": "jwt-alice", "TEST_JWT_BOB": "jwt-bob"}):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost:8000"
                ) as client:
                    self.assertEqual((await client.post("/mcp", headers=headers, json={})).status_code, 401)
                    self.assertEqual((await rpc(client, "invalid", "get_documents")).status_code, 401)
                    alice, bob = await asyncio.gather(
                        rpc(client, "token-alice", "get_documents", {"siteId": "site-alice"}),
                        rpc(client, "token-bob", "get_documents", {"siteId": "site-bob"}),
                    )
                    for result in (alice, bob):
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertFalse(result.json()["result"]["isError"], result.text)
                    self.assertEqual(
                        {(r.url.params["siteId"], r.headers["authorization"]) for r in requests},
                        {
                            ("site-alice", "Bearer jwt-alice"),
                            ("site-bob", "Bearer jwt-bob"),
                        },
                    )
                    for name, args in (
                        ("get_documents", {"siteId": "site-bob"}),
                        ("get_documents", {}),
                        ("get_version", {}),
                    ):
                        denied = await rpc(client, "token-alice", name, args)
                        self.assertTrue(denied.json()["result"]["isError"], denied.text)
                    listed = await rpc(client, "token-alice", "list_formkiq_operations")
                    self.assertEqual(
                        [op["operationId"] for op in listed.json()["result"]["structuredContent"]["result"]],
                        ["GetDocuments"],
                    )
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(len(clients), 1)
                    self.assertIsNone(caller_context.get())
        self.assertTrue(clients[0].is_closed)


class StdioTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, {"FORMKIQ_ENABLE_WRITES": "true", "FORMKIQ_MCP_AUTH_FILE": "/not-used"})
    async def test_stdio_initialization_tools_and_bundled_resource(self):
        import sys

        env = {"MCP_TRANSPORT": "stdio", "PYTHON_DOTENV_DISABLED": "1"}
        params = StdioServerParameters(command=sys.executable, args=["-B", "server.py"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                self.assertEqual(result.serverInfo.name, "FormKiQ OpenAPI Proxy")
                tools = (await session.list_tools()).tools
                self.assertEqual(len(tools), 29)
                schema_tool = next(tool for tool in tools if tool.name == "get_sites_schema")
                self.assertEqual(schema_tool.inputSchema["required"], ["siteId"])
                resource = await session.read_resource("openapi://formkiq/jwt")
                self.assertIn("openapi: 3.0", resource.contents[0].text)
                self.assertFalse(
                    (await session.call_tool("describe_operation", {"operation_id": "DocumentSearch"})).isError
                )


if __name__ == "__main__":
    unittest.main()
