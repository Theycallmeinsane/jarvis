"""Talks to a real MCP server over stdio (tests/fixtures/demo_mcp_server.py)."""
import unittest
from pathlib import Path

from jarvis.config import load_config

SERVER = Path(__file__).parent / "fixtures" / "demo_mcp_server.py"


class MCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from jarvis import mcp_client

        mcp_client._manager = None
        cls.cfg = load_config()
        cls.cfg["mcp_servers"] = {"demo": {"command": "python", "args": [str(SERVER)]}}

    @classmethod
    def tearDownClass(cls):
        from jarvis import mcp_client

        if mcp_client._manager:
            mcp_client._manager.stop()
        mcp_client._manager = None

    def test_tools_are_exposed_and_callable(self):
        from jarvis.tools import ToolBox

        box = ToolBox(self.cfg)
        self.assertIn("demo_add", box.tools)
        self.assertEqual(box.run("demo_add", {"a": 2, "b": 40}), "42")
        self.assertEqual(box.run("demo_shout", {"text": "hi"}), "HI")

    def test_confirmation_policy_follows_annotations(self):
        from jarvis.tools import ToolBox

        box = ToolBox(self.cfg)
        self.assertFalse(box.get("demo_add").dangerous)    # read_only_hint
        self.assertTrue(box.get("demo_shout").dangerous)   # not read-only -> ask first
        schema = box.get("demo_add").schema(self.cfg)["parameters"]
        self.assertEqual(sorted(schema["required"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
