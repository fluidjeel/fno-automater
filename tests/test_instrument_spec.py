"""InstrumentSpec parsing, provenance and persistence.

Covers invariant 6 (unknown reference data must not permit new exposure) and the
money rule that no float reaches an accounting path.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading.data.events import RawMarketCapture
from trading.data.fyers.symbol_master import parse_symbol_master
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.contracts import InstrumentSpec
from trading.domain.enums import Exchange, InstrumentKind, OptionType

FIXTURE = Path(__file__).parent / "fixtures" / "fyers_sym_master.json"
SOURCE = "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"


def _capture() -> RawMarketCapture:
    return RawMarketCapture(
        capture_id="cap-master-1",
        provider="fyers",
        endpoint="sym_details/NSE_FO",
        received_at=datetime(2026, 9, 13, 6, 0, tzinfo=UTC),
        payload=json.loads(FIXTURE.read_text(encoding="utf-8")),
        http_status=200,
    )


def _parse(**overrides: object) -> tuple[InstrumentSpec, ...]:
    kwargs: dict[str, object] = {
        "segment": "NSE_FO",
        "exchange": Exchange.NSE,
        "timezone": "Asia/Kolkata",
        "index_instrument_type": 10,
        "source": SOURCE,
    }
    kwargs.update(overrides)
    return parse_symbol_master(_capture(), **kwargs)  # type: ignore[arg-type]


class TestParseSymbolMaster:
    def test_option_row_carries_lot_tick_strike_and_expiry(self) -> None:
        specs = {spec.trading_symbol: spec for spec in _parse()}
        option = specs["NSE:NIFTY2691519050CE"]
        assert option.instrument_kind is InstrumentKind.OPTION
        assert option.option_type is OptionType.CALL
        assert option.strike == Decimal("19050")
        assert option.lot_size == 65
        assert option.tick_size == Decimal("0.05")
        assert option.price_precision == 2
        assert option.freeze_quantity == 1755
        assert option.expiry == date(2026, 9, 15)
        assert option.provider_token == "101126091536922"
        assert option.exchange_token == 36922

    def test_future_row_has_expiry_but_no_strike(self) -> None:
        specs = {spec.trading_symbol: spec for spec in _parse()}
        future = specs["NSE:NIFTY26SEPFUT"]
        assert future.instrument_kind is InstrumentKind.FUTURE
        assert future.strike is None
        assert future.option_type is None
        assert future.expiry == date(2026, 9, 29)
        assert future.tick_size == Decimal("0.1")
        assert future.price_precision == 1

    def test_index_row_is_quoted_but_not_tradable(self) -> None:
        specs = {spec.trading_symbol: spec for spec in _parse()}
        index = specs["NSE:NIFTY50-INDEX"]
        assert index.instrument_kind is InstrumentKind.INDEX
        assert index.lot_size == 0
        assert index.tick_size == Decimal("0.05")
        assert index.expiry is None
        assert index.upper_price_band is None

    def test_row_without_a_tick_size_is_skipped_not_defaulted(self) -> None:
        """Invariant 6: unusable reference data must not be filled in."""
        symbols = {spec.trading_symbol for spec in _parse()}
        assert "NSE:BROKEN-EQ" not in symbols

    def test_provenance_records_source_and_master_date(self) -> None:
        for spec in _parse():
            assert spec.source == SOURCE
            assert spec.verified_at == date(2026, 9, 11)

    def test_no_float_survives_the_parse_boundary(self) -> None:
        """Money rule: floats are rejected before reaching an accounting path."""
        for spec in _parse():
            assert isinstance(spec.tick_size, Decimal)
            assert spec.strike is None or isinstance(spec.strike, Decimal)

    def test_symbols_filter_restricts_the_catalog(self) -> None:
        specs = _parse(symbols=frozenset({"NSE:NIFTY50-INDEX"}))
        assert [spec.trading_symbol for spec in specs] == ["NSE:NIFTY50-INDEX"]


class TestInstrumentSpecContract:
    def test_float_tick_size_is_rejected(self) -> None:
        """Money rule: a float tick size cannot validate a price grid exactly."""
        with pytest.raises(ValueError, match="float is not permitted"):
            InstrumentSpec(
                trading_symbol="NSE:NIFTY26SEPFUT",
                exchange=Exchange.NSE,
                segment="NSE_FO",
                underlying="NIFTY",
                instrument_kind=InstrumentKind.FUTURE,
                provider_token="1",
                exchange_token=1,
                lot_size=65,
                tick_size=0.1,  # type: ignore[arg-type]
                price_precision=1,
                expiry=date(2026, 9, 29),
                trading_session="0915-1540",
                source=SOURCE,
                verified_at=date(2026, 9, 11),
            )

    def test_derivative_without_a_lot_size_is_rejected(self) -> None:
        """Invariant 6: Layer 2 cannot size a contract with no lot size."""
        with pytest.raises(ValueError, match="positive lot size"):
            InstrumentSpec(
                trading_symbol="NSE:NIFTY26SEPFUT",
                exchange=Exchange.NSE,
                segment="NSE_FO",
                underlying="NIFTY",
                instrument_kind=InstrumentKind.FUTURE,
                provider_token="1",
                exchange_token=1,
                lot_size=0,
                tick_size=Decimal("0.1"),
                price_precision=1,
                expiry=date(2026, 9, 29),
                trading_session="0915-1540",
                source=SOURCE,
                verified_at=date(2026, 9, 11),
            )

    def test_option_without_a_strike_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="strike and option_type"):
            InstrumentSpec(
                trading_symbol="NSE:NIFTY2691519050CE",
                exchange=Exchange.NSE,
                segment="NSE_FO",
                underlying="NIFTY",
                instrument_kind=InstrumentKind.OPTION,
                provider_token="1",
                exchange_token=1,
                lot_size=65,
                tick_size=Decimal("0.05"),
                price_precision=2,
                expiry=date(2026, 9, 15),
                trading_session="0915-1540",
                source=SOURCE,
                verified_at=date(2026, 9, 11),
            )


class TestInstrumentSpecStore:
    def test_round_trip_preserves_every_spec(self, tmp_path: Path) -> None:
        store = InstrumentSpecStore(tmp_path / "instruments")
        specs = _parse()
        store.write("NSE_FO", specs)
        assert store.load("NSE_FO") == specs

    def test_find_returns_none_before_any_download(self, tmp_path: Path) -> None:
        """Invariant 6: an unfetched catalog must not supply a lot or tick size."""
        store = InstrumentSpecStore(tmp_path / "instruments")
        assert store.find("NSE:NIFTY50-INDEX") is None

    def test_find_locates_a_symbol_across_segments(self, tmp_path: Path) -> None:
        store = InstrumentSpecStore(tmp_path / "instruments")
        store.write("NSE_FO", _parse())
        found = store.find("NSE:NIFTY26SEPFUT")
        assert found is not None
        assert found.lot_size == 65
