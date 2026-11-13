"""Investigate agent: a LangGraph agent with read-only MCP tools (server.py). Owner: agent & evals lead.

Used by the Investigate button on a risk card:
    from agent import investigate
    investigate("7300172", "Carrier insurance expired")

CLI:
    python agent.py 7300172
Set MODEL=fake in .env for a scripted stand-in model (no API key needed).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

HERE = pathlib.Path(__file__).parent
load_dotenv(HERE / ".env")

try:  # optional tracing: pip install arize-phoenix-otel openinference-instrumentation-langchain
    from phoenix.otel import register
    register(project_name="broker-desk-copilot", auto_instrument=True)
except ImportError:
    pass

MODEL = os.environ.get("MODEL", "anthropic:claude-sonnet-5-5")
SYSTEM_PROMPT = (
    "You help a freight broker investigate one load in the Meridian TMS. "
    "Use the tools to look up the shipment, its carrier, facilities and activity log; never guess IDs, dates or numbers. "
    "If a tool returns ok=false, read its 'detail'. "
    "Answer in at most 5 short bullet points: what is going on, the evidence, and what the broker should do next."
)

# The MCP server is a child process and doesn't inherit the environment automatically.
PASS_THROUGH = ("TMS_API_BASE", "TMS_API_KEY", "DEMO_MODE", "DRY_RUN", "PATH", "SYSTEMROOT", "TEMP", "TMP")


class _ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _fake_model(record_id: str) -> _ScriptedModel:
    return _ScriptedModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "get_shipment", "args": {"shipment_id": record_id}, "id": "call_1"}]),
        AIMessage(content=f"(fake model) Looked up shipment {record_id}; set MODEL in .env for a real investigation."),
    ]))


async def load_tools():
    client = MultiServerMCPClient({
        "meridian": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(HERE / "server.py")],
            "cwd": str(HERE),
            "env": {k: os.environ[k] for k in PASS_THROUGH if k in os.environ},
        }
    })
    return await client.get_tools()


async def ainvestigate(record_id: str, context: str = "") -> str:
    tools = await load_tools()
    model = _fake_model(record_id) if MODEL == "fake" else MODEL
    agent = create_agent(model, tools, system_prompt=SYSTEM_PROMPT)
    question = f"Investigate shipment {record_id}. " + (f"It was flagged: {context}. " if context else "") + "What's going on and what should I do?"
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    return result["messages"][-1].content


def investigate(record_id: str, context: str = "") -> str:
    """Sync wrapper for Streamlit."""
    return asyncio.run(ainvestigate(record_id, context))


if __name__ == "__main__":
    print(investigate(sys.argv[1] if len(sys.argv) > 1 else "7300172"))
