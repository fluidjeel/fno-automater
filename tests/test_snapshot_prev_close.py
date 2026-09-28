"""Regression: ``market.close`` must stay the previous-session close.

Fyers ``prev_close_price`` is the reference for close-relative direction. The
intraday bar close used to overwrite it, making ``close == last`` and forcing
``technical_bias`` to NEUTRAL (every directional spread got DIRECTION_NEUTRAL).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading.data.config import UnderlyingConfig
from trading.data.events import CanonicalMarketEvent
from trading.data.snapshot_builder import MarketSnapshotBuilder
from trading.domain.contracts import FeatureSnapshot
from trading.domain.contracts.common import DataQualityReport
from trading.domain.enums import (
    AssetClass,
    DataQuality,
    Exchange,
    InstrumentKind,
    ReasonCode,
)
from trading.strategies._common import (
    DEFAULT_TECHNICAL_CONFIDENCE,
    resolve_direction,
    technical_bias,
)
from trading.strategies.macro import MacroBias

NOW = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
PREV_CLOSE = Decimal("23140.50")
QUOTE_LTP = Decimal("22851.00")
BAR_CLOSE = Decimal("22850.00")


def _underlying() -> UnderlyingConfig:
    return UnderlyingConfig(
        symbol="NSE:NIFTY50-INDEX",
        feature_set_version="nifty_index_v1",
        exchange=Exchange.NSE,
        underlying="NIFTY",
        instrument_kind=InstrumentKind.INDEX,
        asset_class=AssetClass.EQUITY_INDEX,
    )


def _builder() -> MarketSnapshotBuilder:
    return MarketSnapshotBuilder(
        _underlying(),
        config_version="1",
        config_checksum="abc",
        code_version="0.1.0",
    )


def _quality() -> DataQualityReport:
    return DataQualityReport(
        state=DataQuality.VALID,
        age_ms=0,
        warmup_complete=True,
        source_status="test",
        reason_codes=(ReasonCode.OK,),
    )


def _event(event_type: str, payload: dict[str, Any]) -> CanonicalMarketEvent:
    return CanonicalMarketEvent(
        event_id=f"EVT-{event_type}",
        provider="fyers",
        symbol="NSE:NIFTY50-INDEX",
        event_type=event_type,
        event_time=NOW,
        source_time=NOW,
        receive_time=NOW,
        provider_sequence=None,
        payload=payload,
        raw_ref=f"test/{event_type}.json",
        normalization_version="1",
    )


def _quote_event() -> CanonicalMarketEvent:
    return _event(
        "QUOTE_SNAPSHOT",
        {
            "quotes": [
                {
                    "symbol": "NSE:NIFTY50-INDEX",
                    "lp": str(QUOTE_LTP),
                    "open_price": "23050.00",
                    "high_price": "23080.00",
                    "low_price": "22820.00",
                    "prev_close_price": str(PREV_CLOSE),
                    "volume": 0,
                }
            ]
        },
    )


def _bar_event() -> CanonicalMarketEvent:
    return _event(
        "BAR_SNAPSHOT",
        {
            "symbol": "NSE:NIFTY50-INDEX",
            "resolution": "1",
            "bar_count": 1,
            "bars": [
                {
                    "timestamp": int(NOW.timestamp()),
                    "open": "22860.00",
                    "high": "22870.00",
                    "low": "22840.00",
                    "close": str(BAR_CLOSE),
                    "volume": 0,
                }
            ],
            "is_final": True,
        },
    )


def _chain_event() -> CanonicalMarketEvent:
    return _event(
        "OPTION_CHAIN_SNAPSHOT",
        {"strikes": [{"option_type": "", "ltp": str(BAR_CLOSE)}]},
    )


def _assert_bearish_against_previous_close(snapshot: FeatureSnapshot) -> None:
    assert snapshot.market.close is not None
    assert snapshot.market.close.value == PREV_CLOSE
    assert snapshot.market.last is not None
    assert snapshot.market.last.value == BAR_CLOSE
    assert technical_bias(snapshot) is MacroBias.BEARISH
    assert resolve_direction(snapshot, None, NOW) == (
        MacroBias.BEARISH,
        DEFAULT_TECHNICAL_CONFIDENCE,
    )


def test_index_snapshot_keeps_previous_close_when_bar_present() -> None:
    snapshot = _builder().build_index(
        (_quote_event(), _bar_event()), as_of=NOW, quality=_quality()
    )
    _assert_bearish_against_previous_close(snapshot)


def test_chain_snapshot_keeps_previous_close_when_bar_present() -> None:
    snapshot = _builder().build(
        (_chain_event(), _quote_event(), _bar_event()),
        as_of=NOW,
        quality=_quality(),
    )
    _assert_bearish_against_previous_close(snapshot)


def test_bar_close_never_masquerades_as_previous_close() -> None:
    """Without a quote there is no previous close; the bar only supplies last."""
    snapshot = _builder().build(
        (_chain_event(), _bar_event()), as_of=NOW, quality=_quality()
    )
    assert snapshot.market.close is None
    assert snapshot.market.last is not None
    assert snapshot.market.last.value == BAR_CLOSE
    assert technical_bias(snapshot) is MacroBias.NEUTRAL
