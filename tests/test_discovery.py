from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import unittest
from unittest.mock import patch

import httpx
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from formkiq_mcp.openapi import OpenApiCatalog, OpenApiProxyError
from formkiq_mcp.security import CallerPolicy, Settings, caller_context
from formkiq_mcp.server import create_server
from tests.test_server import CATALOG, call


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.clients = []

    def server(self, handler, *, catalog=CATALOG, **options):
        def factory(**kwargs):
            async def dispatch(request):
                self.requests.append(request)
                response = handler(request)
                return await response if asyncio.iscoroutine(response) else response

            client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch), **kwargs)
            self.clients.append(client)
            return client

        settings = {"api_endpoint": "https://formkiq.example.test", "jwt": "test-jwt", **options}
        return create_server(Settings(**settings), catalog, factory)

    async def test_initial_tool_list_includes_discovered_modules_once_per_lifespan(self):
        server = self.server(
            lambda _: httpx.Response(
                200,
                json={
                    "version": "1.19.2",
                    "modules": ["dataclassification", "antivirus", "workflow", "opensearch_2.19.2"],
                },
            ),
            api_groups=frozenset({"ai", "malware", "workflows", "audit"}),
        )
        self.assertEqual(self.requests, [])
        async with server.runtime.lifespan():
            tools = {tool.name for tool in await server.list_tools()}
            self.assertEqual(len(tools), 41)
            self.assertTrue(
                {
                    "get_document_ai_prompts_results",
                    "get_malware_scan_results",
                    "get_workflows",
                    "get_resource_activities",
                }
                <= tools
            )
            self.assertTrue({"search_fulltext", "add_document_workflow", "purge_document"}.isdisjoint(tools))

            async def nested():
                async with server.runtime.lifespan():
                    self.assertIn("get_workflows", {tool.name for tool in await server.list_tools()})

            await asyncio.gather(nested(), nested())
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(self.requests[0].method, "GET")
            self.assertEqual(self.requests[0].url.path, "/version")
            self.assertEqual(self.requests[0].headers["authorization"], "Bearer test-jwt")
            self.assertEqual(len(self.clients), 1)
            self.assertFalse(self.clients[0].is_closed)
        self.assertTrue(self.clients[0].is_closed)

    async def test_failed_or_invalid_discovery_is_nonfatal_and_never_logs_response_secrets(self):
        for response in (
            httpx.Response(401, json={"message": "upstream-secret"}),
            httpx.Response(200, content=b"upstream-secret", headers={"content-type": "application/json"}),
            httpx.Response(200, json={"modules": {"ai": True}}),
        ):
            with self.subTest(status=response.status_code, body=response.content):
                server = self.server(
                    lambda _: response, api_groups=frozenset({"ai", "workflows"}), modules=frozenset({"ai"})
                )
                with self.assertLogs("formkiq.discovery", level="INFO") as logs:
                    async with server.runtime.lifespan():
                        tools = {tool.name for tool in await server.list_tools()}
                        self.assertIn("get_documents", tools)
                        self.assertIn("get_document_ai_prompts_results", tools)
                        self.assertNotIn("get_workflows", tools)
                        denied = await call(server, "get_workflows")
                        self.assertTrue(denied.isError)
                        self.assertIn(server.runtime.discovery_message, denied.content[0].text)
                output = "\n".join(logs.output)
                self.assertIn("workflows", output)
                self.assertIn("FORMKIQ_MODULES", output)
                self.assertNotIn("upstream-secret", output)
                self.assertNotIn("test-jwt", output)

    async def test_timeout_is_bounded_and_manual_discovery_can_recover(self):
        should_hang = True

        async def hanging(_):
            if should_hang:
                await asyncio.Event().wait()
            return httpx.Response(200, json={"modules": ["opensearch"]})

        server = self.server(hanging, api_groups=frozenset({"fulltext"}))
        with patch("formkiq_mcp.server.DISCOVERY_TIMEOUT_SECONDS", 0.01):
            async with server.runtime.lifespan():
                self.assertIn("timed out", server.runtime.discovery_message)
                self.assertNotIn("search_fulltext", {tool.name for tool in await server.list_tools()})
                should_hang = False
                self.assertFalse((await call(server, "get_version")).isError)
                self.assertIn("search_fulltext", {tool.name for tool in await server.list_tools()})

    async def test_discovery_skips_missing_credentials_disabled_mode_and_custom_specs(self):
        custom = OpenApiCatalog(
            yaml.safe_dump({"openapi": "3.0.3", "paths": {"/documents": {"get": {"operationId": "GetDocuments"}}}}),
            frozenset({"GetDocuments"}),
        )
        for options, catalog, reason in (
            ({"api_endpoint": ""}, CATALOG, "FORMKIQ_API_ENDPOINT_URL"),
            ({"jwt": None}, CATALOG, "JWT"),
            ({"auto_discover": False}, CATALOG, "disabled"),
            ({}, custom, "custom spec"),
        ):
            with self.subTest(options=options):
                server = self.server(
                    lambda _: self.fail("Discovery should have been skipped"), catalog=catalog, **options
                )
                async with server.runtime.lifespan():
                    self.assertIn(reason, server.runtime.discovery_message)
                    self.assertIn("get_documents", {tool.name for tool in await server.list_tools()})
        self.assertEqual(self.requests, [])

    async def test_hosted_startup_uses_authorized_caller_without_widening_any_grants(self):
        policies = tuple(
            CallerPolicy(
                name,
                hashlib.sha256(name.encode()).hexdigest(),
                f"JWT_{name.upper()}",
                frozenset(operations),
                frozenset({"tenant"}),
            )
            for name, operations in (("limited", {"GetDocuments"}), ("discovery", {"GetVersion", "GetWorkflow"}))
        )
        server = self.server(
            lambda _: httpx.Response(200, json={"modules": ["workflows"]}),
            callers=policies,
            api_groups=frozenset({"workflows"}),
        )
        app = server.streamable_http_app()
        with patch.dict(os.environ, {"JWT_LIMITED": "limited-jwt", "JWT_DISCOVERY": "discovery-jwt"}):
            async with app.router.lifespan_context(app):
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.requests[0].headers["authorization"], "Bearer discovery-jwt")
                self.assertIsNone(caller_context.get())
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://localhost:8000"
                ) as client:
                    headers = {
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2025-11-25",
                        "Authorization": "Bearer limited",
                    }
                    response = await client.post(
                        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    names = {tool["name"] for tool in response.json()["result"]["tools"]}
                    self.assertIn("get_documents", names)
                    self.assertNotIn("get_workflow", names)
                    self.assertNotIn("get_version", names)
                self.assertEqual(len(self.requests), 1)

    async def test_stdio_initial_listing_contains_discovered_tools_without_a_manual_call(self):
        script = """
import httpx
from formkiq_mcp.security import Settings
from formkiq_mcp.server import create_server

def factory(**options):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"modules": ["ai", "malware", "workflows"]})),
        **options,
    )

create_server(
    Settings(api_endpoint="https://formkiq.example.test", jwt="test-jwt", api_groups=frozenset({"ai", "malware", "workflows", "audit"})),
    client_factory=factory,
).run(transport="stdio")
"""
        params = StdioServerParameters(command=sys.executable, args=["-c", script], env={})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            self.assertEqual(len(tools), 41)
            self.assertTrue({"get_workflows", "get_malware_scan_results", "get_document_ai_prompts_results"} <= tools)

    async def test_hosted_discovery_requires_a_get_version_grant_even_with_a_global_jwt(self):
        policy = CallerPolicy("limited", "0" * 64, "JWT_LIMITED", frozenset({"GetDocuments"}), frozenset({"tenant"}))
        server = self.server(lambda _: self.fail("Caller did not grant GetVersion"), callers=(policy,))
        async with server.runtime.lifespan():
            self.assertIn("no configured caller is allowed GetVersion", server.runtime.discovery_message)
        self.assertEqual(self.requests, [])

    async def test_cancelled_startup_closes_client_and_releases_lock(self):
        started = asyncio.Event()

        async def hanging(_):
            started.set()
            await asyncio.Event().wait()

        server = self.server(hanging)
        lifespan = server.runtime.lifespan()
        task = asyncio.create_task(lifespan.__aenter__())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.clients[0].is_closed)
        self.assertIsNone(server.runtime.client)
        self.assertEqual(server.runtime.users, 0)
        self.assertFalse(server.runtime.lock.locked())

    def test_auto_discovery_environment_setting_defaults_on_and_can_be_disabled(self):
        for env, expected in (({}, True), ({"FORMKIQ_AUTO_DISCOVER": "false"}, False)):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(Settings.from_env().auto_discover, expected)
        with (
            patch.dict(os.environ, {"FORMKIQ_AUTO_DISCOVER": "maybe"}, clear=True),
            self.assertRaisesRegex(OpenApiProxyError, "FORMKIQ_AUTO_DISCOVER"),
        ):
            Settings.from_env()
