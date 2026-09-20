"""Layer 4 agentic research loop. Proposal-only; no live authority."""

from trading.ai.advise import ADVISE_SYSTEM_PROMPT, run_advise_agent
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
from trading.ai.tools import ADVISE_TOOL_SPECS, TOOL_SPECS, ToolContext, dispatch_tool

__all__ = [
    "ADVISE_SYSTEM_PROMPT",
    "ADVISE_TOOL_SPECS",
    "SYSTEM_PROMPT",
    "TOOL_SPECS",
    "DeltaGapCounter",
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
    "build_delta_packet",
    "dispatch_tool",
    "parse_openai_chat_completion",
    "persist_agent_run",
    "prefix_hash",
    "run_advise_agent",
    "run_weekly_agent",
]
from trading.ai.packets import DeltaGapCounter, build_delta_packet, prefix_hash
