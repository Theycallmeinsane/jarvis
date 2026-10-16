"""A tiny MCP server used by the tests (and handy as a template for your own)."""
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("demo")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


@server.tool()
def shout(text: str) -> str:
    """Return the text in capitals (not marked read-only, so Jarvis asks first)."""
    return text.upper()


if __name__ == "__main__":
    server.run("stdio")
