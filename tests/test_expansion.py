from __future__ import annotations

import asyncio
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import httpx
import yaml

from formkiq_mcp.openapi import OpenApiCatalog, OpenApiProxyError, substitute_path_params
from formkiq_mcp.operations import ALLOWED_OPERATION_IDS, API_GROUPS, OPERATION_POLICIES, module_capabilities
from formkiq_mcp.security import CallerPolicy, Settings, caller_context
from formkiq_mcp.server import create_server
from formkiq_mcp.tools import tool_name
from tests import test_server as baseline

call = baseline.call

ADDITIONS = frozenset(
    """
    GetSites GetPermissionCatalog GetFolderDocuments IndexSearch GetSitesSchema
    GetSitesClassifications GetClassification GetSitesSchemaAttributeAllowedValues
    GetClassificationAttributeAllowedValues GetDocumentOcr GetDocumentFulltext
    GetDocumentAiPromptsResults GetDocumentAiPromptResults GetMalwareScanResults
    GetWorkflows GetWorkflow GetDocumentWorkflows GetDocumentWorkflow GetQueues
    GetQueue GetWorkflowQueueDocuments GetWorkflowDocuments AddDocument
    GetDocumentIdUpload AddFolder AddDocumentRetryAction AddDocumentOcr
    AddDocumentWorkflow AddDocumentWorkflowDecisions
""".split()
)


class ExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.body = {"ok": True}
        self.handler = lambda request: httpx.Response(200, json=self.body)
        self.server = await self.make_server()

    async def make_server(self, catalog=None, **options):
        options.setdefault("auto_discover", False)

        async def dispatch(request):
            self.requests.append(request)
            result = self.handler(request)
            return await result if asyncio.iscoroutine(result) else result

        server = create_server(
            Settings(api_endpoint="https://formkiq.example.test", jwt="test-jwt", **options),
            catalog or baseline.CATALOG,
            lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(dispatch), **kwargs),
        )
        lifespan = server.runtime.lifespan()
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        return server

    async def test_all_29_new_operations_validate_and_route_with_explicit_effects(self):
        self.assertEqual(len(ADDITIONS), 29)
        self.assertEqual(len(ALLOWED_OPERATION_IDS), 69)
        server = await self.make_server(
            api_groups=API_GROUPS,
            modules=frozenset({"opensearch", "ai", "malware", "workflows", "textract"}),
            enable_writes=True,
            enable_purge=True,
        )
        self.assertEqual(server.runtime.enabled_ids(), ALLOWED_OPERATION_IDS)
        bodies = {
            "IndexSearch": {"indexType": "folder"},
            "AddDocument": {"path": "test.txt", "contentType": "text/plain", "content": "sample"},
            "AddFolder": {"path": "folder"},
            "AddDocumentOcr": {"ocrEngine": "TESSERACT", "parseTypes": ["TEXT"]},
            "AddDocumentWorkflow": {"workflowId": "workflow-1"},
            "AddDocumentWorkflowDecisions": {"decision": "APPROVE", "comments": "Reviewed"},
        }
        for name in sorted(ADDITIONS):
            with self.subTest(operation=name):
                operation = server.runtime.catalog.operations[name]
                path = {
                    parameter["name"]: "tenant-a" if parameter["name"] == "siteId" else "id-1"
                    for parameter in operation["parameters"]
                    if parameter["in"] == "path"
                }
                args = dict(path)
                if name in bodies:
                    args["body"] = bodies[name]
                result = await call(server, tool_name(name), args)
                self.assertFalse(result.isError, result.content)
                self.assertEqual(self.requests[-1].method, operation["method"])
                self.assertEqual(self.requests[-1].url.path, substitute_path_params(operation["path"], path))
        self.assertEqual(len(self.requests), 29)

    async def test_get_upload_is_write_and_post_index_search_is_read(self):
        server = await self.make_server(enable_writes=True)
        args = {"operation_id": "GetDocumentIdUpload", "args": {"documentId": "doc"}}
        self.assertTrue((await call(server, "call_formkiq_operation", args)).isError)
        self.assertEqual(self.requests, [])
        self.assertFalse((await call(server, "call_formkiq_write_operation", args)).isError)
        self.assertEqual(self.requests[-1].method, "GET")
        result = await call(
            self.server,
            "call_formkiq_operation",
            {"operation_id": "IndexSearch", "args": {"body": {"indexType": "folder"}}},
        )
        self.assertFalse(result.isError)
        self.assertEqual(self.requests[-1].method, "POST")
        self.assertEqual(OPERATION_POLICIES["GetDocumentIdUpload"].access, "write")

    async def test_module_api_groups_discover_and_revoke_capabilities_without_granting_access(self):
        server = await self.make_server(api_groups=API_GROUPS)
        before = server.runtime.enabled_ids()
        self.assertFalse(
            {"GetWorkflow", "GetDocumentAiPromptsResults", "SearchFulltext", "GetMalwareScanResults"} & before
        )
        self.assertIn("GetDocumentOcr", before)  # Tesseract does not need an add-on.
        result = await call(
            server, "call_formkiq_operation", {"operation_id": "GetWorkflow", "args": {"workflowId": "w"}}
        )
        self.assertTrue(result.isError)
        self.assertEqual(self.requests, [])
        self.body = {
            "version": "1.19.2",
            "modules": ["workflow", "dataclassification", "opensearch_2.19.2", "antivirus", "unknown_module"],
        }
        self.assertFalse((await call(server, "get_version")).isError)
        self.assertTrue(
            {"GetWorkflow", "GetDocumentAiPromptsResults", "SearchFulltext", "GetMalwareScanResults"}
            <= server.runtime.enabled_ids()
        )
        restricted = CallerPolicy("limited", "0" * 64, "TEST_JWT", frozenset({"GetDocuments"}), frozenset({"default"}))
        context = caller_context.set(restricted)
        try:
            self.assertEqual(server.runtime.enabled_ids(), {"GetDocuments"})
        finally:
            caller_context.reset(context)
        self.body = {"version": "1.19.2", "modules": []}
        await call(server, "get_version")
        self.assertNotIn("GetWorkflow", server.runtime.enabled_ids())
        self.assertNotIn("SearchFulltext", server.runtime.enabled_ids())

    async def test_api_group_opt_in_is_required_even_when_module_is_installed(self):
        server = await self.make_server(modules=frozenset({"ai", "workflows", "opensearch"}))
        self.assertNotIn("GetWorkflow", server.runtime.enabled_ids())
        self.assertNotIn("GetResourceActivities", server.runtime.enabled_ids())
        result = await call(server, "describe_operation", {"operation_id": "GetWorkflow"})
        self.assertTrue(result.isError)
        self.assertIn("API group", result.content[0].text)
        override = await self.make_server(api_groups=frozenset({"workflows"}), modules=frozenset({"workflows"}))
        self.assertIn("GetWorkflow", override.runtime.enabled_ids())
        self.body = {"modules": []}
        await call(override, "get_version")
        self.assertIn("GetWorkflow", override.runtime.enabled_ids())

    async def test_permission_metadata_is_informational_and_preserved(self):
        result = await call(self.server, "describe_operation", {"operation_id": "GetSitesSchema"})
        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["requiredPermission"], "READ_SCHEMAS")
        self.assertEqual(result.structuredContent["access"], "read")
        self.assertEqual(result.structuredContent["apiGroup"], "core")
        self.assertNotIn("profile", result.structuredContent)
        self.assertIn("200", result.structuredContent["responseStatusCodes"])
        operations = (await call(self.server, "list_formkiq_operations")).structuredContent["result"]
        self.assertTrue(all(operation["apiGroup"] == "core" for operation in operations))
        self.assertTrue(all("profile" not in operation for operation in operations))
        self.body = {"roleTypes": [{"name": "CUSTOM", "groups": []}]}
        await call(self.server, "call_formkiq_operation", {"operation_id": "GetPermissionCatalog"})
        self.assertNotIn("DeleteDocument", self.server.runtime.enabled_ids())

    async def test_advanced_ocr_options_require_textract(self):
        server = await self.make_server(api_groups=frozenset({"ocr"}), enable_writes=True)
        for body in ({"ocrEngine": "TEXTRACT"}, {"parseTypes": ["FORMS"]}, {"parseTypes": ["tables"]}):
            result = await call(
                server,
                "call_formkiq_write_operation",
                {"operation_id": "AddDocumentOcr", "args": {"documentId": "doc", "body": body}},
            )
            self.assertTrue(result.isError)
            self.assertIn("textract", result.content[0].text)
        self.assertEqual(self.requests, [])
        self.body = {"modules": ["textract"]}
        await call(server, "get_version")
        self.assertFalse(
            (
                await call(
                    server,
                    "call_formkiq_write_operation",
                    {
                        "operation_id": "AddDocumentOcr",
                        "args": {"documentId": "doc", "body": {"ocrEngine": "TEXTRACT"}},
                    },
                )
            ).isError
        )

    async def test_search_pagination_and_nested_json_are_forwarded(self):
        body = {
            "query": {
                "attributes": [
                    {"key": "status", "eq": "approved"},
                    {"key": "invoice", "json": {"path": "$.total", "gte": 1000}},
                ]
            }
        }
        result = await call(
            self.server,
            "search_documents",
            {"body": body, "siteId": "tenant", "next": "page-2", "limit": "5", "projection": "DOCUMENTS"},
        )
        self.assertFalse(result.isError)
        request = self.requests[-1]
        self.assertEqual(json.loads(request.content), body)
        self.assertEqual(
            dict(request.url.params), {"siteId": "tenant", "next": "page-2", "limit": "5", "projection": "DOCUMENTS"}
        )

    async def test_counts_preserve_truncation_and_never_aggregate(self):
        for count, truncated in ((0, False), (34, False), (10000, True), (12, True)):
            self.body = {"count": count, "truncated": truncated}
            before = len(self.requests)
            result = await call(self.server, "count_documents", {"body": {"query": {"tag": {"key": "invoice"}}}})
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["countIsExact"], not truncated)
            self.assertEqual(result.structuredContent["body"], self.body)
            self.assertEqual(self.requests[-1].url.params["projection"], "COUNT")
            self.assertEqual(len(self.requests), before + 1)
        for invalid in (
            {"documents": []},
            {"count": True, "truncated": False},
            {"count": 10001, "truncated": True},
            {"count": 3},
            {"count": 3, "truncated": "false"},
        ):
            self.body = invalid
            self.assertTrue(
                (await call(self.server, "count_documents", {"body": {"query": {"text": "invoice"}}})).isError
            )

    async def test_delete_requires_an_explicit_mode_and_keeps_legacy_explicit_flags(self):
        server = await self.make_server(enable_writes=True)
        self.assertTrue(
            (
                await call(
                    server,
                    "call_formkiq_write_operation",
                    {"operation_id": "DeleteDocument", "args": {"documentId": "doc"}},
                )
            ).isError
        )
        self.assertEqual(self.requests, [])
        for mode in ("SOFT_DELETE", "HARD_DELETE", "CONTENT_ONLY"):
            self.assertFalse((await call(server, "delete_document", {"documentId": "doc", "deleteType": mode})).isError)
            self.assertEqual(self.requests[-1].url.params["deleteType"], mode)
        self.assertFalse(
            (
                await call(
                    server,
                    "call_formkiq_write_operation",
                    {"operation_id": "DeleteDocument", "args": {"documentId": "doc", "softDelete": True}},
                )
            ).isError
        )
        self.assertEqual(self.requests[-1].url.params["deleteType"], "SOFT_DELETE")
        self.assertTrue((await call(server, "delete_document", {"documentId": "doc", "deleteType": "PURGE"})).isError)

    async def test_accepted_processing_response_is_returned_without_polling(self):
        self.handler = lambda request: httpx.Response(202, json={"status": "PENDING", "jobId": "job-1"})
        server = await self.make_server(api_groups=frozenset({"ocr"}), enable_writes=True)
        result = await call(
            server,
            "call_formkiq_write_operation",
            {"operation_id": "AddDocumentOcr", "args": {"documentId": "doc", "body": {"ocrEngine": "TESSERACT"}}},
        )
        self.assertFalse(result.isError)
        self.assertEqual(result.structuredContent["statusCode"], 202)
        self.assertEqual(result.structuredContent["body"]["status"], "PENDING")
        self.assertEqual(len(self.requests), 1)

    async def test_declared_download_redirect_returns_url_without_forwarding_credentials(self):
        document = {
            "openapi": "3.0.3",
            "paths": {
                "/documents/{documentId}": {
                    "get": {
                        "operationId": "GetDocument",
                        "parameters": [
                            {"name": "documentId", "in": "path", "required": True, "schema": {"type": "string"}},
                            {"name": "siteId", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"307": {"description": "Download redirect"}},
                    }
                }
            },
        }
        catalog = OpenApiCatalog(yaml.safe_dump(document), frozenset({"GetDocument"}))
        server = await self.make_server(catalog=catalog)
        self.handler = lambda request: httpx.Response(
            307, headers={"Location": "https://download.example.test/file?signature=secret"}
        )
        result = await call(server, "get_document", {"documentId": "doc"})
        self.assertFalse(result.isError)
        self.assertEqual(
            result.structuredContent["body"]["contentUrl"], "https://download.example.test/file?signature=secret"
        )
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.host, "formkiq.example.test")
        for location in (
            "http://download.example.test/file",
            "file:///private/file",
            "https://user:secret@example.test/file",
            "",
        ):
            self.handler = lambda request, location=location: httpx.Response(307, headers={"Location": location})
            self.assertTrue((await call(server, "get_document", {"documentId": "doc"})).isError)

    async def test_undeclared_redirect_is_an_error(self):
        self.handler = lambda request: httpx.Response(307, headers={"Location": "https://download.example.test"})
        self.assertTrue((await call(self.server, "get_document", {"documentId": "doc"})).isError)
        self.assertEqual(len(self.requests), 1)

    async def test_http_path_sites_site_lists_and_upload_capabilities_are_enforced(self):
        policy = CallerPolicy(
            "analyst",
            hashlib.sha256(b"mcp-test-token").hexdigest(),
            "TEST_EXPANSION_JWT",
            frozenset({"GetSites", "GetSitesSchema", "GetDocumentIdUpload", "SearchFulltext"}),
            frozenset({"site-a"}),
        )
        server = await self.make_server(
            callers=(policy,), api_groups=frozenset({"fulltext"}), modules=frozenset({"opensearch"}), enable_writes=True
        )
        app = server.streamable_http_app()
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
            "Authorization": "Bearer mcp-test-token",
        }

        async def rpc(client, name, arguments):
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["result"]

        with patch.dict(os.environ, {"TEST_EXPANSION_JWT": "downstream-test-token"}):
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost:8000") as client,
            ):
                self.body = {
                    "sites": [
                        {
                            "siteId": "site-a",
                            "title": "Allowed",
                            "permissions": ["READ_DOCUMENTS"],
                            "config": {"secret": "hidden"},
                        },
                        {"siteId": "site-b", "title": "Denied"},
                    ],
                    "userClaims": {"secret": "hidden"},
                    "roles": ["ADMIN"],
                }
                listed = await rpc(client, "call_formkiq_operation", {"operation_id": "GetSites"})
                self.assertFalse(listed["isError"])
                self.assertEqual(
                    listed["structuredContent"]["body"],
                    {"sites": [{"siteId": "site-a", "title": "Allowed", "permissions": ["READ_DOCUMENTS"]}]},
                )
                self.body = {"attributes": []}
                allowed = await rpc(
                    client, "call_formkiq_operation", {"operation_id": "GetSitesSchema", "args": {"siteId": "site-a"}}
                )
                self.assertFalse(allowed["isError"])
                self.assertEqual(self.requests[-1].url.path, "/sites/site-a/schema/document")
                self.assertEqual(self.requests[-1].headers["authorization"], "Bearer downstream-test-token")
                before = len(self.requests)
                for args in ({"siteId": "site-b"}, {"path": {"siteId": "site-b"}}):
                    denied = await rpc(
                        client, "call_formkiq_operation", {"operation_id": "GetSitesSchema", "args": args}
                    )
                    self.assertTrue(denied["isError"])
                    self.assertIn("not authorized", denied["content"][0]["text"])
                denied = await rpc(
                    client,
                    "call_formkiq_operation",
                    {
                        "operation_id": "SearchFulltext",
                        "args": {
                            "siteId": "site-a",
                            "indexName": "site-b-index",
                            "body": {"query": {"attributes": [{"key": "category", "eq": "invoice"}]}},
                        },
                    },
                )
                self.assertTrue(denied["isError"])
                self.assertIn("indexName", denied["content"][0]["text"])
                denied = await rpc(
                    client,
                    "call_formkiq_operation",
                    {"operation_id": "GetDocumentIdUpload", "args": {"documentId": "doc", "siteId": "site-a"}},
                )
                self.assertTrue(denied["isError"])
                self.assertEqual(len(self.requests), before)
                allowed = await rpc(
                    client,
                    "call_formkiq_write_operation",
                    {"operation_id": "GetDocumentIdUpload", "args": {"documentId": "doc", "siteId": "site-a"}},
                )
                self.assertFalse(allowed["isError"])
                self.assertEqual(self.requests[-1].method, "GET")
        self.assertIsNone(caller_context.get())


class ApiGroupConfigurationTests(unittest.TestCase):
    def test_explicit_registry_and_configuration(self):
        self.assertEqual(
            {name for name, policy in OPERATION_POLICIES.items() if policy.access == "purge"}, {"PurgeDocument"}
        )
        self.assertFalse(
            {"GetApiKeys", "PublicAddDocument", "SetAntivirus", "GetDocumentMetadataExtractionResults"}
            & ALLOWED_OPERATION_IDS
        )
        for invalid_group in ("typo", "search"):
            with self.subTest(api_group=invalid_group), self.assertRaisesRegex(OpenApiProxyError, "FORMKIQ_API_GROUPS"):
                Settings(api_groups=frozenset({invalid_group}))
        with patch.dict(
            os.environ,
            {"FORMKIQ_API_GROUPS": "ocr, fulltext, workflows", "FORMKIQ_MODULES": "workflow,textract"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.api_groups, {"core", "ocr", "fulltext", "workflows"})
        self.assertEqual(module_capabilities(settings.modules), {"workflows", "textract"})
        self.assertEqual(
            module_capabilities(
                [
                    "MODULE_opensearch",
                    "opensearch_2.19.2",
                    "dataclassification",
                    "antivirus",
                    "workflow",
                    "site_permissions_automatic",
                ]
            ),
            {"opensearch", "ai", "malware", "workflows"},
        )
        self.assertEqual(module_capabilities(["no_opensearch", "opensearch_disabled", "workflow_false"]), set())

    def test_api_groups_environment_defaults_to_core(self):
        for env in ({}, {"FORMKIQ_API_GROUPS": ""}, {"FORMKIQ_API_GROUPS": " , "}):
            with (
                self.subTest(env=env),
                patch.dict(os.environ, env, clear=True),
            ):
                self.assertEqual(Settings.from_env().api_groups, {"core"})

    def test_invalid_api_groups_environment_is_rejected(self):
        with (
            patch.dict(os.environ, {"FORMKIQ_API_GROUPS": "typo"}, clear=True),
            self.assertRaisesRegex(OpenApiProxyError, "FORMKIQ_API_GROUPS"),
        ):
            Settings.from_env()
