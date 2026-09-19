"""Layer 4 agentic research loop. Proposal-only; no live authority."""

from trading.ai.history_ports import SnapshotMarketPort, StaticNewsPort
from trading.ai.loop import SYSTEM_PROMPT, run_weekly_agent
from trading.ai.openai_compat import OpenAICompatLlm, parse_openai_chat_completion
from trading.ai.ports import (
    LlmPort,
    LlmTimeoutError,
    LlmToolCall,
    LlmTurn,
    MarketReadPort,
    NewsReadPort,
)
from trading.ai.recording import RecordingLlm, persist_agent_run
from trading.ai.tools import TOOL_SPECS, ToolContext, dispatch_tool

__all__ = [
    "SYSTEM_PROMPT",
    "TOOL_SPECS",
    "LlmPort",
    "LlmTimeoutError",
    "LlmToolCall",
    "LlmTurn",
    "MarketReadPort",
    "NewsReadPort",
    "OpenAICompatLlm",
    "RecordingLlm",
    "SnapshotMarketPort",
    "StaticNewsPort",
    "ToolContext",
    "dispatch_tool",
    "parse_openai_chat_completion",
    "persist_agent_run",
    "run_weekly_agent",
]
