"""ADESK-B6: PORTFOLIO SHADOW and SAME_THESIS_DRIVER recall."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.ai.decision_log import DecisionLog
from trading.ai.portfolio_desk import (
    deterministic_same_thesis_pairs,
    maybe_log_portfolio_shadow,
    same_thesis_driver_recall,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts.identification import ConfidenceKind
from trading.domain.contracts.trade_thesis import InvalidationCondition, TradeThesis
from trading.domain.enums import (
    AgentAction,
    Comparator,
    DeskRole,
    DirectionalClaim,
    DriverCode,
    InvalidationMetric,
    InvalidationSeverity,
    SharedFateCode,
)
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _thesis(trade_id: str, driver: DriverCode) -> TradeThesis:
    inv = (
        InvalidationCondition(
            condition_id="a",
            metric=InvalidationMetric.DTE,
            comparator=Comparator.LTE,
            threshold=Decimal("1"),
            severity=InvalidationSeverity.HARD,
        ),
        InvalidationCondition(
            condition_id="b",
            metric=InvalidationMetric.MAE_R,
            comparator=Comparator.GT,
            threshold=Decimal("1"),
            severity=InvalidationSeverity.HARD,
        ),
    )
    return TradeThesis(
        thesis_id=f"TH-{trade_id}",
        trade_id=trade_id,
        snapshot_id="s",
        written_at=NOW,
        author=DeskRole.ENTRY,
        model_id="m",
        prompt_version="p",
        directional_claim=DirectionalClaim.BULLISH,
        horizon_days=5,
        primary_driver=driver,
        supporting_reason_codes=("x",),
        contradicting_reason_codes=("y",),
        invalidation=inv,
        confidence=Decimal("0.5"),
        confidence_kind=ConfidenceKind.RAW_SCORE,
        thesis_hash=f"h-{trade_id}",
    )


def test_same_thesis_driver_recall() -> None:
    t1 = _thesis("t1", DriverCode.TREND_CONTINUATION)
    t2 = _thesis("t2", DriverCode.TREND_CONTINUATION)
    t3 = _thesis("t3", DriverCode.MEAN_REVERSION)
    truth = deterministic_same_thesis_pairs((t1, t2, t3))
    assert ("t1", "t2") in truth
    recall = same_thesis_driver_recall(
        ground_truth=truth, agent_flagged=frozenset({("t1", "t2")})
    )
    assert recall.recall == Decimal("1.0000")
    miss = same_thesis_driver_recall(ground_truth=truth, agent_flagged=frozenset())
    assert miss.recall == Decimal("0.0000")


def test_shadow_logs_veto_on_shared_driver(tmp_path: Path) -> None:
    store = TradingStore.open(tmp_path / "pf.sqlite", clock=FrozenClock(NOW))
    log = DecisionLog(store)
    open_t = (_thesis("open1", DriverCode.TREND_CONTINUATION),)
    cand = _thesis("cand", DriverCode.TREND_CONTINUATION)
    result = maybe_log_portfolio_shadow(
        as_of=NOW,
        candidate_trade_id="cand",
        open_theses=open_t,
        candidate_thesis=cand,
        decision_log=log,
        enabled=True,
        run_id="r",
        snapshot_id="snap",
    )
    assert result.status == "LOGGED"
    assert result.veto is not None
    assert result.veto.action is AgentAction.VETO_ENTRY
    assert result.veto.shared_fate[0].code is SharedFateCode.SAME_THESIS_DRIVER
    assert len(log.list(role=DeskRole.PORTFOLIO)) == 1
    store.close()
