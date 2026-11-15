from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest.mock import patch

import httpx
import yaml
from jsonschema import Draft202012Validator

from formkiq_mcp.openapi import OpenApiCatalog
from formkiq_mcp.operations import API_GROUPS, OPERATION_POLICIES
from formkiq_mcp.security import CallerPolicy, Settings, caller_context
from formkiq_mcp.server import create_server
from formkiq_mcp.tools import tool_name
from tests.test_server import CATALOG, call


class EndpointToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response = {"ok": True}

    async def server(self, catalog=CATALOG, **options):
        options.setdefault("auto_discover", False)

        async def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.response)

        server = create_server(
            Settings(api_endpoint="https://formkiq.example.test", jwt="test-jwt", **options),
            catalog,
            lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
        )
        lifespan = server.runtime.lifespan()
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        return server

    async def test_all_enabled_endpoints_have_unique_tools_and_valid_schemas(self):
        server = await self.server(
            api_groups=API_GROUPS,
            modules=frozenset({"ai", "malware", "workflows", "opensearch"}),
            enable_writes=True,
            enable_purge=True,
        )
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(len(tools), 74)
        for name, policy in OPERATION_POLICIES.items():
            with self.subTest(operation=name):
                tool = tools[tool_name(name)]
                Draft202012Validator.check_schema(tool.inputSchema)
                self.assertEqual(tool.annotations.readOnlyHint, policy.access == "read")
                self.assertEqual(tool.annotations.destructiveHint, policy.access != "read")
                self.assertIn(f"API group: {policy.api_group}", tool.description)
                self.assertFalse(tool.inputSchema["additionalProperties"])
        self.assertTrue(
            {"documentId", "artifactId", "shareKey", "siteId"} <= tools["get_document"].inputSchema["properties"].keys()
        )
        self.assertIn("actionStatus", tools["get_documents"].inputSchema["properties"])
        self.assertFalse(tools["get_document_id_upload"].annotations.readOnlyHint)
        self.assertTrue(tools["index_search"].annotations.readOnlyHint)
        listed = (await call(server, "list_formkiq_operations")).structuredContent["result"]
        self.assertEqual({entry["toolName"] for entry in listed}, {tool_name(name) for name in OPERATION_POLICIES})

    async def test_api_groups_modules_and_write_flags_filter_discovery_and_direct_calls(self):
        server = await self.server(api_groups=frozenset({"ocr", "fulltext", "workflows"}))
        names = {tool.name for tool in await server.list_tools()}
        self.assertEqual(len(names), 30)
        self.assertIn("get_document_ocr", names)
        self.assertTrue({"search_fulltext", "get_workflows", "add_document_ocr", "purge_document"}.isdisjoint(names))
        for name, args in (
            ("get_workflows", {}),
            ("search_fulltext", {"body": {}}),
            ("get_document_id_upload", {"documentId": "doc"}),
            ("purge_document", {"documentId": "doc"}),
        ):
            self.assertTrue((await call(server, name, args)).isError)
        self.assertEqual(self.requests, [])
        self.response = {"modules": ["workflow", "opensearch_2.19.2"]}
        self.assertFalse((await call(server, "get_version")).isError)
        names = {tool.name for tool in await server.list_tools()}
        self.assertTrue({"search_fulltext", "get_workflows"} <= names)
        self.response = {"modules": []}
        await call(server, "get_version")
        names = {tool.name for tool in await server.list_tools()}
        self.assertTrue({"search_fulltext", "get_workflows"}.isdisjoint(names))
        self.assertTrue((await call(server, "get_workflows")).isError)
        self.assertEqual(len(self.requests), 2)

    async def test_flat_parameters_route_to_path_query_body_and_headers(self):
        server = await self.server(enable_writes=True)
        result = await call(server, "get_folder_documents", {"path": "invoices/2026", "siteId": "tenant", "limit": "5"})
        self.assertFalse(result.isError, result.content)
        self.assertEqual(self.requests[-1].url.path, "/folders")
        self.assertEqual(self.requests[-1].url.params["path"], "invoices/2026")
        result = await call(server, "get_document_tags", {"documentId": "a/b", "siteId": "tenant", "previous": "page"})
        self.assertFalse(result.isError, result.content)
        self.assertIn("a%2Fb", self.requests[-1].url.raw_path.decode())
        self.assertEqual(self.requests[-1].url.params["previous"], "page")
        result = await call(
            server, "add_document_tags", {"documentId": "doc", "body": {"tags": [{"key": "kind", "value": "invoice"}]}}
        )
        self.assertFalse(result.isError, result.content)
        self.assertEqual(self.requests[-1].method, "POST")
        self.assertEqual(json.loads(self.requests[-1].content), {"tags": [{"key": "kind", "value": "invoice"}]})

    async def test_schema_errors_and_dot_segments_never_reach_backend(self):
        server = await self.server(enable_writes=True)
        for name, args in (
            ("get_document", {}),
            ("get_document", {"documentId": "doc", "unexpected": "secret"}),
            ("get_documents", {"limit": 5}),
            ("get_sites", {"status": "typo"}),
            ("search_documents", {"body": {"query": "wrong"}}),
            ("add_document_tags", {"documentId": "doc", "body": {"tags": "wrong"}}),
            ("delete_document_tag", {"documentId": "doc", "tagKey": ".."}),
            ("delete_document", {"documentId": "doc"}),
        ):
            with self.subTest(tool=name, args=args):
                result = await call(server, name, args)
                self.assertTrue(result.isError)
                self.assertNotIn("secret", result.content[0].text)
        self.assertEqual(self.requests, [])

    async def test_colliding_parameter_names_nullable_and_recursive_body_refs(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/documents/{id}": {
                    "post": {
                        "operationId": "AddDocument",
                        "parameters": [
                            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                            {"name": "id", "in": "query", "schema": {"type": "string"}},
                            {"name": "body", "in": "query", "schema": {"type": "string"}},
                            {"name": "X-Test", "in": "header", "schema": {"type": "string"}},
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}},
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "Node": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "nullable": True},
                            "child": {"$ref": "#/components/schemas/Node"},
                        },
                    }
                }
            },
        }
        server = await self.server(OpenApiCatalog(yaml.safe_dump(spec), frozenset({"AddDocument"})), enable_writes=True)
        tool = next(t for t in await server.list_tools() if t.name == "add_document")
        Draft202012Validator.check_schema(tool.inputSchema)
        result = await call(
            server,
            "add_document",
            {
                "path_id": "doc",
                "query_id": "lookup",
                "query_body": "query-value",
                "X-Test": "header-value",
                "body": {"label": None, "child": {"label": "child"}},
            },
        )
        self.assertFalse(result.isError, result.content)
        self.assertEqual(self.requests[-1].url.path, "/documents/doc")
        self.assertEqual(dict(self.requests[-1].url.params), {"id": "lookup", "body": "query-value"})
        self.assertEqual(self.requests[-1].headers["x-test"], "header-value")
        result = await call(server, "add_document", {"path_id": "doc", "body": {"child": {"label": 1}}})
        self.assertTrue(result.isError)
        self.assertEqual(len(self.requests), 1)

    async def test_http_tool_lists_are_caller_specific_and_cached_tools_cannot_bypass_grants(self):
        policies = tuple(
            CallerPolicy(
                name,
                hashlib.sha256(name.encode()).hexdigest(),
                f"JWT_{name.upper()}",
                frozenset(operations),
                frozenset({name}),
            )
            for name, operations in (("alice", {"GetDocuments"}), ("bob", {"GetSitesSchema", "AddDocument"}))
        )
        server = await self.server(callers=policies, enable_writes=True)
        app = server.streamable_http_app()
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}

        async def rpc(client, user, method, params=None):
            response = await client.post(
                "/mcp",
                headers={**headers, "Authorization": f"Bearer {user}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params or {},
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["result"]

        with patch.dict(os.environ, {"JWT_ALICE": "alice-jwt", "JWT_BOB": "bob-jwt"}):
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost:8000") as client,
            ):
                alice = {t["name"] for t in (await rpc(client, "alice", "tools/list"))["tools"]}
                bob = {t["name"] for t in (await rpc(client, "bob", "tools/list"))["tools"]}
                self.assertEqual(
                    alice, {"get_documents", "call_formkiq_operation", "describe_operation", "list_formkiq_operations"}
                )
                self.assertEqual(
                    bob,
                    {
                        "get_sites_schema",
                        "add_document",
                        "call_formkiq_operation",
                        "call_formkiq_write_operation",
                        "describe_operation",
                        "list_formkiq_operations",
                    },
                )
                # Bob's listing refreshes the SDK cache; Alice still cannot invoke Bob's tools.
                denied = await rpc(
                    client,
                    "alice",
                    "tools/call",
                    {"name": "add_document", "arguments": {"siteId": "alice", "body": {"path": "file.txt"}}},
                )
                self.assertTrue(denied["isError"])
                denied = await rpc(
                    client, "bob", "tools/call", {"name": "get_sites_schema", "arguments": {"siteId": "alice"}}
                )
                self.assertTrue(denied["isError"])
                self.assertEqual(self.requests, [])
                allowed = await rpc(
                    client, "bob", "tools/call", {"name": "get_sites_schema", "arguments": {"siteId": "bob"}}
                )
                self.assertFalse(allowed["isError"], allowed)
                self.assertEqual(self.requests[-1].url.path, "/sites/bob/schema/document")
                self.assertEqual(self.requests[-1].headers["authorization"], "Bearer bob-jwt")
        self.assertIsNone(caller_context.get())
