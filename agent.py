"""LangChain agent: forecast next 5 minutes of load, call MCP set_warm_pool_size.

Prefers Bedrock (langchain-aws). Falls back to a local heuristic stand-in that
still invokes the same MCP tool so the control-plane path is always real on camera.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
TRAFFIC = ROOT / "traffic.csv"
REGION = "us-east-1"
# "Now" is just before the spike in traffic.csv — agent must act on the forecast.
NOW_MINUTE = 24
HORIZON = 5

SYSTEM = (
    "You are a capacity planner for a serverless GPU inference platform. "
    "Cold starts cost ~3000ms; warm hits cost ~100ms. "
    "Given recent request volume, forecast the next 5 minutes and call "
    "set_warm_pool_size once with a pool large enough to absorb the peak. "
    "Prefer a slightly larger pool over under-provisioning. Be concise."
)


def _load_dotenv() -> None:
    for f in (
        ROOT / "../../../.env",
        Path.home() / ".config/startup-demos.env",
        ROOT / ".env",
    ):
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k and v and k not in os.environ:
                os.environ[k] = v


def _traffic_rows() -> list[tuple[int, int]]:
    rows: list[tuple[int, int]] = []
    with TRAFFIC.open() as f:
        for row in csv.DictReader(f):
            rows.append((int(row["minute"]), int(row["requests"])))
    return rows


def _traffic_prompt(rows: list[tuple[int, int]]) -> str:
    history = [r for r in rows if r[0] <= NOW_MINUTE]
    future = [r for r in rows if NOW_MINUTE < r[0] <= NOW_MINUTE + HORIZON]
    # Future rows are ground truth for the sim; the prompt only shows history +
    # a hint that a spike is expected (the agent reasons; the CSV spike is the
    # synthetic forecast the demo feeds in — we include upcoming minutes so the
    # model/heuristic can plan, matching the approved idea's "CSV of the last
    # 30 minutes with an upcoming spike").
    lines = ["minute,requests  # last 30 min including upcoming forecast window"]
    for m, n in rows:
        lines.append(f"{m},{n}")
    return (
        f"Region: {REGION}\n"
        f"Current minute: {NOW_MINUTE}\n"
        f"History through T={NOW_MINUTE}: peak={max(n for _, n in history)}\n"
        f"Forecast window T={NOW_MINUTE + 1}..{NOW_MINUTE + HORIZON} "
        f"(in the CSV): peak={max((n for _, n in future), default=0)}\n\n"
        + "\n".join(lines)
        + f"\n\nCall set_warm_pool_size(region={REGION!r}, count=<peak for next {HORIZON} min>)."
    )


def _text(result) -> str:
    parts = []
    for block in result.content or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts) if parts else str(result)


def _message_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip() if content else ""


def _make_llm():
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    try:
        import boto3
        from langchain_aws import ChatBedrock

        boto3.client("sts", region_name=region).get_caller_identity()
        model = os.environ.get(
            "BEDROCK_MODEL",
            "anthropic.claude-3-haiku-20240307-v1:0",
        )
        print(f"llm: Bedrock ({model})", flush=True)
        return ChatBedrock(
            model_id=model,
            region_name=region,
            model_kwargs={"temperature": 0},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"llm: Bedrock unavailable ({exc})", flush=True)

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        try:
            from langchain_anthropic import ChatAnthropic

            model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
            print(f"llm: Anthropic API ({model})", flush=True)
            return ChatAnthropic(model=model, temperature=0, api_key=key)
        except Exception as exc:  # noqa: BLE001
            print(f"llm: Anthropic unavailable ({exc})", flush=True)

    print("llm: local heuristic stand-in (still calls MCP)", flush=True)
    return None


def _forecast_count(rows: list[tuple[int, int]]) -> int:
    window = [n for m, n in rows if NOW_MINUTE < m <= NOW_MINUTE + HORIZON]
    return max(window) if window else 1


async def _bind(session: ClientSession) -> StructuredTool:
    async def set_warm_pool_size(region: str, count: int) -> str:
        """Set desired warm worker pool size for a region."""
        out = _text(
            await session.call_tool(
                "set_warm_pool_size",
                {"region": region, "count": int(count)},
            )
        )
        print(f"→ MCP set_warm_pool_size({region!r}, {count}) → {out}", flush=True)
        return out

    return StructuredTool.from_function(
        name="set_warm_pool_size",
        description=(
            "Write the desired warm-pool size for a region into Redis so the "
            "scheduler can pre-provision workers before a traffic spike."
        ),
        coroutine=set_warm_pool_size,
    )


async def _heuristic(tool: StructuredTool, rows: list[tuple[int, int]]) -> str:
    count = _forecast_count(rows)
    reasoning = (
        f"Local stand-in reasoning: at T={NOW_MINUTE}, the next {HORIZON} minutes "
        f"in the forecast CSV peak at {count} concurrent requests. "
        f"Setting warm pool to {count} in {REGION} so the spike is absorbed warm."
    )
    print(f"[reasoning] {reasoning}", flush=True)
    return await tool.ainvoke({"region": REGION, "count": count})


async def _llm_agent(llm, tool: StructuredTool, prompt: str) -> None:
    from langchain.agents import create_agent

    print("\n=== agent reasoning + MCP tool call ===\n", flush=True)
    agent = create_agent(llm, [tool], system_prompt=SYSTEM)
    result = await agent.ainvoke({"messages": [HumanMessage(content=prompt)]})
    for msg in result["messages"]:
        kind = type(msg).__name__
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                print(f"→ tool {tc.get('name')}({json.dumps(tc.get('args', {}))})", flush=True)
        text = _message_text(getattr(msg, "content", ""))
        if text and kind in ("AIMessage", "ToolMessage"):
            if kind == "ToolMessage" and len(text) > 300:
                text = text[:300] + "…"
            print(f"[{kind}] {text}\n", flush=True)


async def run() -> None:
    rows = _traffic_rows()
    prompt = _traffic_prompt(rows)
    env = {**os.environ}
    env.setdefault("REDIS_URL", "redis://127.0.0.1:16380/0")

    params = StdioServerParameters(
        command=PYTHON,
        args=[str(ROOT / "mcp_server.py")],
        cwd=str(ROOT),
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool = await _bind(session)
            llm = _make_llm()
            if llm is None:
                await _heuristic(tool, rows)
                return
            try:
                await _llm_agent(llm, tool, prompt)
            except Exception as exc:  # noqa: BLE001
                print(f"llm agent failed ({exc}); falling back to heuristic", flush=True)
                await _heuristic(tool, rows)


if __name__ == "__main__":
    _load_dotenv()
    asyncio.run(run())
