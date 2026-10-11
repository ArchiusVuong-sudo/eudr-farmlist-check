"""One isolated mapping query on the Claude Agent SDK. Run as a child process with an allowlisted env.

stdin: {"system": str, "prompt": str, "model": str, "headers": [str], "max_turns": int}
stdout: one JSON object {"ok": bool, "proposal": {...} | null, "model": str, "duration_ms": int,
        "cost_usd": float | null, "num_turns": int, "error": str | null, "api_error_status": int | null}

Isolation: no built-in tools, one in-process MCP tool (submit_mapping) that validates its input,
no filesystem settings, strict MCP config, no session persistence, a fresh temp cwd, few turns,
and the 'dontAsk' permission mode so nothing outside the allowlist can be approved.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from .schema import TOOL_INPUT_SCHEMA, parse_proposal

SERVER = "farmlist"
TOOL = "submit_mapping"


async def run(req: dict[str, Any]) -> dict[str, Any]:
    headers: list[str] = req["headers"]
    accepted: dict[str, Any] = {}

    @tool(TOOL, "Submit the column mapping for this farm list (one entry per column).", TOOL_INPUT_SCHEMA)
    async def submit_mapping(args: dict[str, Any]) -> dict[str, Any]:
        proposal, problems = parse_proposal(args, headers)
        if proposal is None:
            return {
                "content": [{"type": "text", "text": "Rejected, fix and call again: " + "; ".join(problems)}],
                "is_error": True,
            }
        accepted["proposal"] = proposal.model_dump()
        return {"content": [{"type": "text", "text": "Accepted."}]}

    server = create_sdk_mcp_server(SERVER, tools=[submit_mapping])
    out: dict[str, Any] = {"ok": False, "proposal": None, "model": req["model"], "duration_ms": 0,
                           "cost_usd": None, "num_turns": 0, "error": None, "api_error_status": None}
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="farmlist-query-") as cwd:
        options = ClaudeAgentOptions(
            tools=[],
            allowed_tools=[f"mcp__{SERVER}__{TOOL}"],
            mcp_servers={SERVER: server},
            strict_mcp_config=True,
            setting_sources=[],
            permission_mode="dontAsk",
            max_turns=int(req.get("max_turns", 4)),
            cwd=cwd,
            model=req["model"],
            system_prompt=req["system"],
            extra_args={"no-session-persistence": None},
        )
        async for msg in query(prompt=req["prompt"], options=options):
            if isinstance(msg, AssistantMessage):
                out["model"] = msg.model or out["model"]
                if msg.error:
                    out["error"] = str(msg.error)
            elif isinstance(msg, ResultMessage):
                out["num_turns"] = msg.num_turns
                out["cost_usd"] = msg.total_cost_usd
                out["api_error_status"] = msg.api_error_status
                if msg.is_error:
                    out["error"] = "; ".join(msg.errors or []) or msg.result or msg.subtype
    out["duration_ms"] = round((time.monotonic() - t0) * 1000)
    if "proposal" in accepted:
        out["ok"] = True
        out["proposal"] = accepted["proposal"]
    elif not out["error"]:
        out["error"] = "the model ended without a valid submit_mapping call"
    return out


def main() -> None:
    req = json.loads(sys.stdin.read())
    try:
        result = anyio.run(run, req)
    except Exception as exc:  # noqa: BLE001 - report every failure as data to the parent
        result = {"ok": False, "proposal": None, "model": req.get("model"), "duration_ms": 0, "cost_usd": None,
                  "num_turns": 0, "error": f"{type(exc).__name__}: {exc}"[:800], "api_error_status": None}
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
