"""ATM option and front-month future candidates from the instrument master."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import tests.factories as f
from trading.data.events import CanonicalMarketEvent
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.contracts import InstrumentSpec
from trading.domain.enums import (
    Exchange,
    InstrumentKind,
    OptionType,
)
from trading.runtime.candidates import build_option_candidates, front_month_future

NOW = f.NOW
ZONE = ZoneInfo("Asia/Kolkata")


def _spec(**overrides: object) -> InstrumentSpec:
    payload: dict[str, object] = {
        "trading_symbol": "NSE:NIFTY26SEP24500CE",
        "exchange": Exchange.NFO,
        "segment": "NSE_FO",
        "underlying": "NIFTY",
        "instrument_kind": InstrumentKind.OPTION,
        "provider_token": "1",
        "exchange_token": 1,
        "lot_size": 75,
        "tick_size": Decimal("0.05"),
        "price_precision": 2,
        "expiry": date(2026, 9, 24),
        "strike": Decimal("24500"),
        "option_type": OptionType.CALL,
        "trading_session": "0915-1530",
        "source": "fixture",
        "verified_at": date(2026, 9, 11),
    }
    payload.update(overrides)
    return InstrumentSpec.model_validate(payload)


def test_option_candidates_skip_rows_without_master_lots(tmp_path: Path) -> None:
    store = InstrumentSpecStore(tmp_path)
    store.write("NSE_FO", (_spec(),))
    chain = CanonicalMarketEvent(
        event_id="chain-1",
        provider="fyers",
        symbol="NSE:NIFTY50-INDEX",
        event_type="OPTION_CHAIN_SNAPSHOT",
        event_time=NOW,
        source_time=NOW,
        receive_time=NOW,
        provider_sequence=None,
        payload={
            "strikes": [
                {"strike_price": -1, "option_type": "", "ltp": 24500},
                {
                    "symbol": "NSE:NIFTY26SEP24500CE",
                    "strike_price": 24500,
                    "option_type": "CE",
                    "ltp": 120,
                    "bid": 119.5,
                    "ask": 120.5,
                    "oi": 2000,
                },
                {
                    "symbol": "NSE:NIFTY26SEP24600CE",
                    "strike_price": 24600,
                    "option_type": "CE",
                    "ltp": 80,
                    "oi": 100,
                },
            ]
        },
        raw_ref="raw://chain",
        normalization_version="1",
    )
    underlying = f.snapshot(
        market=f.quote(last=f.price("24500"), close=f.price("24400"))
    )
    candidates, instruments = build_option_candidates(
        chain,
        store,
        underlying=underlying,
        as_of=NOW,
        zone=ZONE,
        strikes_each_side=1,
    )
    assert len(candidates) == 1
    assert "NSE:NIFTY26SEP24500CE" in instruments
    assert candidates[0].derivatives is not None
    assert candidates[0].derivatives.days_to_expiry >= 0
    assert instruments["NSE:NIFTY26SEP24500CE"].lot_size == 75


def test_front_month_future_returns_none_without_catalog(tmp_path: Path) -> None:
    store = InstrumentSpecStore(tmp_path)
    assert (
        front_month_future(
            store,
            underlying="CRUDEOIL",
            exchange=Exchange.MCX,
            as_of=NOW,
            zone=ZONE,
        )
        is None
    )


def test_front_month_future_picks_nearest_unexpired(tmp_path: Path) -> None:
    store = InstrumentSpecStore(tmp_path)
    near = _spec(
        trading_symbol="MCX:CRUDEOIL26SEPFUT",
        exchange=Exchange.MCX,
        segment="MCX_COM",
        underlying="CRUDEOIL",
        instrument_kind=InstrumentKind.FUTURE,
        option_type=None,
        strike=None,
        expiry=date(2026, 9, 18),
        lot_size=100,
    )
    far = near.model_copy(
        update={
            "trading_symbol": "MCX:CRUDEOIL26OCTFUT",
            "expiry": date(2026, 10, 16),
        }
    )
    store.write("MCX_COM", (far, near))
    chosen = front_month_future(
        store,
        underlying="CRUDEOIL",
        exchange=Exchange.MCX,
        as_of=NOW,
        zone=ZONE,
    )
    assert chosen is not None
    assert chosen.trading_symbol == "MCX:CRUDEOIL26SEPFUT"
