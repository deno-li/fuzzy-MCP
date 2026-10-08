# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT

"""End-to-end: start the real server process over stdio (no network needed)."""

import sys

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio


async def test_server_starts_over_stdio_and_answers():
    params = StdioServerParameters(command=sys.executable, args=["-m", "fuzzy_mcp", "--sources", "reference"])
    async with Client(params, read_timeout_seconds=30) as client:
        assert client.server_info.name == "fuzzy-mcp"
        assert "SCB" in (client.instructions or "")
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"ref_lookup_region", "fuzzy_list_sources"} <= names
        assert not any(n.startswith("scb_") for n in names)
        result = await client.call_tool("ref_lookup_region", {"query": "Västerås", "limit": 1})
        assert result.structured_content["result"][0]["kod"] == "1980"
