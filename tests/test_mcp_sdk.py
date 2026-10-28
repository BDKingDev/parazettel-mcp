"""Protocol-level tests against the real MCP SDK server (no mocked MCPServer).

The other MCP tests patch MCPServer and call the tool functions directly, so
they cannot catch SDK API changes. These go through an in-process mcp Client:
initialize, list tools, call tools and read the results as a client would.
"""

import re
import threading

import anyio
import pytest
from mcp import Client

from parazettel_mcp.config import config
from parazettel_mcp.server.mcp_server import ZettelkastenMcpServer
from parazettel_mcp.services.embedding_provider import HashEmbeddingProvider


@pytest.fixture
def server(test_config):
    server = ZettelkastenMcpServer()
    yield server
    server.close()


def _text_unchecked(result) -> str:
    return "".join(block.text for block in result.content)


def _text(result) -> str:
    assert not result.is_error, result
    return _text_unchecked(result)


def test_initialize_reports_name_version_and_instructions(server):
    async def scenario():
        async with Client(server.mcp) as client:
            return client.server_info, client.instructions

    info, instructions = anyio.run(scenario)
    assert info.name == config.server_name
    # MCP 2 no longer defaults the version to the SDK's own; we pass ours.
    assert info.version == config.server_version
    assert instructions and "Parazettel" in instructions


def test_list_tools_exposes_schemas(server):
    async def scenario():
        async with Client(server.mcp) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in anyio.run(scenario)}
    assert len(tools) == len(server.mcp._tool_manager.list_tools())
    create = tools["pzk_create_note"]
    assert create.input_schema["required"] == ["title", "content"]
    assert "note_type" in create.input_schema["properties"]
    assert create.description


def test_create_then_search_round_trip(server):
    async def scenario():
        async with Client(server.mcp) as client:
            area = await client.call_tool(
                "pzk_create_area", {"title": "SDK area", "content": "Home."}
            )
            area_id = re.search(r"ID: (\S+)", _text(area)).group(1)
            created = await client.call_tool(
                "pzk_create_note",
                {
                    "title": "Protocol round trip",
                    "content": "Written through the MCP 2 client session.",
                    "tags": "mcp-sdk",
                    "source": "manual",
                    "area_id": area_id,
                },
            )
            found = await client.call_tool(
                "pzk_search_notes", {"query": "round trip", "tags": "mcp-sdk"}
            )
            return created, found

    created, found = anyio.run(scenario)
    assert "Note created successfully" in _text(created)
    assert "Protocol round trip" in _text(found)
    # MCP 2 also returns a str result as structured content.
    assert found.structured_content == {"result": _text(found)}


def test_tool_failure_comes_back_as_tool_text_not_protocol_error(server):
    async def scenario():
        async with Client(server.mcp) as client:
            return await client.call_tool("pzk_get_note", {"identifier": "missing"})

    assert "Note not found" in _text(anyio.run(scenario))


def test_missing_required_argument_is_a_tool_error(server):
    async def scenario():
        async with Client(server.mcp) as client:
            return await client.call_tool("pzk_get_note", {})

    result = anyio.run(scenario)
    assert result.is_error
    assert "identifier" in _text_unchecked(result)


class _ThreadRecordingProvider(HashEmbeddingProvider):
    """Records which thread each embed runs on (the lazy model load site)."""

    def __init__(self) -> None:
        super().__init__(dim=8)
        self.threads: list[threading.Thread] = []

    def embed_documents(self, texts):
        self.threads.append(threading.current_thread())
        return super().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        self.threads.append(threading.current_thread())
        return super().embed_query(text)


def test_direct_mode_loads_embedding_model_on_startup_thread(test_config, monkeypatch):
    """MCP 2 runs sync tools on worker threads; the first embed (which imports
    fastembed for real providers) must still happen on the startup thread."""
    provider = _ThreadRecordingProvider()
    monkeypatch.setattr(
        "parazettel_mcp.storage.note_repository.build_embedding_provider",
        lambda _config: provider,
    )
    server = ZettelkastenMcpServer()
    try:

        async def scenario():
            async with Client(server.mcp) as client:
                await client.call_tool(
                    "pzk_create_area", {"title": "Embedded area", "content": "x"}
                )

        anyio.run(scenario)
    finally:
        server.close()

    assert provider.threads[0] is threading.main_thread()
    # The tool call itself embedded on a worker thread: why the pre-warm matters.
    assert provider.threads[-1] is not threading.main_thread()
