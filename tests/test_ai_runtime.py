"""ADESK-B1: shared desk runtime stop mapping.

No behaviour change for weekly/advise.
"""

from __future__ import annotations

from trading.ai.runtime import ToolLoopStop


def test_tool_loop_stop_values_are_stable() -> None:
    assert ToolLoopStop.DISABLED.value == "DISABLED"
    assert ToolLoopStop.OBSERVE.value == "OBSERVE"
    assert ToolLoopStop.TERMINAL.value == "TERMINAL"
    assert set(ToolLoopStop) == {
        ToolLoopStop.DISABLED,
        ToolLoopStop.OBSERVE,
        ToolLoopStop.BUDGET,
        ToolLoopStop.TIMEOUT,
        ToolLoopStop.SCHEMA,
        ToolLoopStop.TERMINAL,
        ToolLoopStop.EMPTY,
    }
