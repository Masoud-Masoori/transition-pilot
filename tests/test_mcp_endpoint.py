"""Smoke test for the FastMCP /mcp endpoint.

Confirms the MCP protocol path actually works end-to-end against a live
local server. Runs the same reconciliation pipeline through the MCP tool
that the Prompt Opinion platform will call into.

Run: python -m pytest tests/test_mcp_endpoint.py -v -s
(server must be running on 127.0.0.1:8089)
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastmcp import Client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8089/mcp/")


async def _run():
    async with Client(MCP_URL) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools]
        assert "build_transition_packet" in names, f"got: {names}"
        assert "list_demo_cases" in names

        cases_result = await client.call_tool("list_demo_cases", {})
        cases = cases_result.data if hasattr(cases_result, "data") else cases_result
        assert any(c.get("case_id") == "ahrq_warfarin_tmp_smx"
                   for c in cases), f"got: {cases}"

        memo_result = await client.call_tool(
            "build_transition_packet",
            {"case_id": "ahrq_warfarin_tmp_smx"},
        )
        memo = memo_result.data if hasattr(memo_result, "data") else memo_result
        assert "memo" in memo, f"got: {memo}"
        assert len(memo["memo"]["failures_prevented"]) >= 1
        assert memo["memo"]["failures_prevented"][0]["pattern_id"] == "warfarin_antibiotic"
        # Logic-Link must be populated — Evidence-or-Null guarantee
        assert len(memo["memo"]["failures_prevented"][0]["logic_link"]) >= 2


def test_mcp_smoke():
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
    print("[mcp test] OK")
