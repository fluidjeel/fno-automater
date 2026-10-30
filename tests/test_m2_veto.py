"""Five-minute M2 veto admission. No network and no orders."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading.ai.llm_settings import LlmSettings
from trading.ai.openai_compat import chat_completion_body
from trading.trade.m2_veto import (
    M2VetoAction,
    M2VetoConfig,
    M2VetoVerdict,
    admit_m2_entry,
    bar_move_state,
    load_m2_veto_config,
    lots_after_veto,
    parse_model_verdict,
    write_verdict,
)

_NOW = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)


def _config(tmp_path: Path) -> M2VetoConfig:
    return M2VetoConfig(
        schema_version="1",
        enabled=True,
        valid_for_seconds=300,
        size_down_fraction=Decimal("0.5"),
        verdict_path=str(tmp_path / "verdict.json"),
        log_path=str(tmp_path / "log.jsonl"),
        session_open_ist="09:15",
        session_close_ist="15:30",
        nifty_symbol="NSE:NIFTY50-INDEX",
        vix_symbol="NSE:INDIAVIX-INDEX",
        bar_resolution="5",
        completed_bars=6,
        quote_max_age_seconds=60,
    )


def test_missing_verdict_blocks(tmp_path: Path) -> None:
    admission = admit_m2_entry(_NOW, config=_config(tmp_path))
    assert admission.blocks_new_entry is True
    assert "no five-minute" in admission.detail
    assert lots_after_veto(1, admission) == 0


def test_approve_allows_and_size_down_floors(tmp_path: Path) -> None:
    config = _config(tmp_path)
    approve = parse_model_verdict(
        {"action": "APPROVE", "reasons": ["5-minute move still developing"]},
        now=_NOW,
        config=config,
    )
    write_verdict(approve, config)
    allowed = admit_m2_entry(_NOW, config=config)
    assert allowed.blocks_new_entry is False
    assert lots_after_veto(1, allowed) == 1

    smaller = parse_model_verdict(
        {"action": "SIZE_DOWN", "reasons": ["latest quote is stalling"]},
        now=_NOW,
        config=config,
    )
    assert smaller.action is M2VetoAction.SIZE_DOWN
    write_verdict(smaller, config)
    sized = admit_m2_entry(_NOW, config=config)
    assert sized.blocks_new_entry is False
    assert lots_after_veto(1, sized) == 0
    assert lots_after_veto(2, sized) == 1


def test_expired_verdict_blocks(tmp_path: Path) -> None:
    config = _config(tmp_path)
    write_verdict(
        M2VetoVerdict(
            action=M2VetoAction.APPROVE,
            size_fraction=Decimal("1"),
            reasons=("still developing",),
            as_of=_NOW - timedelta(minutes=10),
            valid_until=_NOW - timedelta(minutes=5),
            source="test",
        ),
        config,
    )
    admission = admit_m2_entry(_NOW, config=config)
    assert admission.blocks_new_entry is True
    assert "expired" in admission.detail


def test_down_bar_still_falling_is_developing() -> None:
    assert (
        bar_move_state(
            quote=Decimal("100"),
            bar_open=Decimal("110"),
            bar_high=Decimal("111"),
            bar_low=Decimal("102"),
            bar_close=Decimal("103"),
        )
        == "developing"
    )


def test_configured_model_is_flash_without_thinking() -> None:
    loaded = load_m2_veto_config()
    assert loaded.model_id == "deepseek-flash"
    assert loaded.thinking == "disabled"
    settings = LlmSettings(DEEPSEEK_API_KEY="test", LLM_MODEL="deepseek-chat")
    body = chat_completion_body(
        settings,
        [{"role": "user", "content": "{}"}],
        json_object=True,
        model=loaded.model_id,
        thinking=loaded.thinking,
    )
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] != settings.llm_model


def test_down_bar_retrace_is_exhausted() -> None:
    assert (
        bar_move_state(
            quote=Decimal("108"),
            bar_open=Decimal("110"),
            bar_high=Decimal("111"),
            bar_low=Decimal("100"),
            bar_close=Decimal("101"),
        )
        == "exhausted"
    )
