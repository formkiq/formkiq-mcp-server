from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from formkiq_mcp.security import Settings
from formkiq_mcp.server import main


class DotenvTests(unittest.TestCase):
    def start(self):
        with patch("formkiq_mcp.server.create_server") as factory:
            settings = []
            factory.side_effect = lambda: settings.append(Settings.from_env()) or factory.return_value
            main()
            return settings[0], factory.return_value.run.call_args.kwargs["transport"]

    def test_dotenv_loads_settings_and_transport_before_startup(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory), patch.dict(os.environ, {}, clear=True):
            Path(".env").write_text(
                'FORMKIQ_API_ENDPOINT_URL="https://formkiq.example.test"\n'
                "FORMKIQ_JWT='literal-${TOKEN}-value'\n"
                "MCP_TRANSPORT=stdio\n"
                "FORMKIQ_API_GROUPS=core,ocr\n"
                "FORMKIQ_ENABLE_WRITES=false\n"
            )
            settings, transport = self.start()
            self.assertEqual(settings.api_endpoint, "https://formkiq.example.test")
            self.assertEqual(settings.jwt, "literal-${TOKEN}-value")
            self.assertEqual(settings.api_groups, {"core", "ocr"})
            self.assertFalse(settings.enable_writes)
            self.assertEqual(transport, "stdio")

    def test_existing_environment_including_empty_values_wins(self):
        env = {"FORMKIQ_JWT": "host-token", "FORMKIQ_API_GROUPS": "", "MCP_TRANSPORT": "stdio"}
        with tempfile.TemporaryDirectory() as directory, chdir(directory), patch.dict(os.environ, env, clear=True):
            Path(".env").write_text("FORMKIQ_JWT=file-token\nFORMKIQ_API_GROUPS=ocr\nMCP_TRANSPORT=streamable-http\n")
            settings, transport = self.start()
            self.assertEqual(settings.jwt, "host-token")
            self.assertEqual(settings.api_groups, {"core"})
            self.assertEqual(transport, "stdio")

    def test_missing_dotenv_does_not_search_parent_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / ".env").write_text("FORMKIQ_JWT=parent-token\nFORMKIQ_ENABLE_WRITES=true\nMCP_TRANSPORT=stdio\n")
            (root / "child").mkdir()
            with chdir(root / "child"):
                settings, transport = self.start()
            self.assertIsNone(settings.jwt)
            self.assertFalse(settings.enable_writes)
            self.assertEqual(transport, "streamable-http")


class DotenvStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkout_entry_point_loads_dotenv_for_stdio_and_api_groups(self):
        entrypoint = Path(__file__).resolve().parents[1] / "server.py"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text("MCP_TRANSPORT=stdio\nFORMKIQ_API_GROUPS=core,ocr\n")
            params = StdioServerParameters(command=sys.executable, args=[str(entrypoint)], cwd=directory, env={})
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                self.assertIn("get_document_ocr", tools)
                self.assertNotIn("add_document_ocr", tools)
