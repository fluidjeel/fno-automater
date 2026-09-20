"""Focused offline validation for FYERS capability probe analysis."""

from __future__ import annotations

from datetime import UTC, datetime

from trading.data.fyers.capability_probe import (
    CapturedMessage,
    ProbeSymbolSet,
    analyze_samples,
    depth_level_count,
    documented_feed_catalog,
    generate_markdown_report,
    serialize_depth,
)


class _FakeDepth:
    def __init__(self) -> None:
        self.tbq = 1000
        self.tsq = 900
        self.bidprice = [100.0] + [0.0] * 49
        self.askprice = [100.5] + [0.0] * 49
        self.bidqty = [50] + [0] * 49
        self.askqty = [40] + [0] * 49
        self.bidordn = [3] + [0] * 49
        self.askordn = [2] + [0] * 49
        self.snapshot = True
        self.timestamp = 1_700_000_000
        self.sendtime = 1_700_000_001
        self.seqNo = 1


def test_depth_level_count_from_tbt_payload() -> None:
    """TBT payloads expose explicit bid/ask level arrays."""
    payload = serialize_depth(_FakeDepth())
    bid, ask = depth_level_count(payload)
    assert bid == 1
    assert ask == 1
    assert payload["snapshot"] is True
    assert payload["sequence_no"] == 1


def test_analyze_samples_flags_missing_aggressor() -> None:
    """Aggressor side must remain unavailable unless explicitly present."""
    now = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
    samples = (
        CapturedMessage(
            feed="data_ws",
            data_type="SymbolUpdate",
            symbol="NSE:NIFTY50-INDEX",
            receive_time=now,
            payload={
                "ltp": 24500.5,
                "bid_price": 24500.0,
                "ask_price": 24501.0,
                "exch_feed_time": 1_700_000_000,
            },
            message_kind="quote",
        ),
    )
    summary = analyze_samples(
        samples,
        feed="data_ws",
        data_type="SymbolUpdate",
        symbol="NSE:NIFTY50-INDEX",
    )
    assert summary.has_ltp is True
    assert summary.has_aggressor_side is False
    assert any("aggressor_side unavailable" in note for note in summary.notes)


def test_documented_feed_catalog_covers_three_feeds() -> None:
    """SDK catalog documents data socket, TBT socket, and REST depth."""
    catalog = documented_feed_catalog()
    assert catalog["data_ws"]["data_types"]["DepthUpdate"]["depth_levels"] == 5
    assert catalog["tbt_ws"]["depth_levels"] == 50
    assert catalog["tbt_ws"]["mcx_supported"] is False


def test_generate_markdown_report_includes_matrix() -> None:
    """Report renderer emits the requested audit sections."""
    now = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
    symbols = ProbeSymbolSet(
        nifty_option="NSE:NIFTY26SEP24000CE",
        stock_option="NSE:RELIANCE26SEP1400CE",
    )
    sample = CapturedMessage(
        feed="data_ws",
        data_type="SymbolUpdate",
        symbol=symbols.nifty_index,
        receive_time=now,
        payload={"ltp": 1.0, "bid_price": 0.9, "ask_price": 1.1},
        message_kind="quote",
    )
    report = generate_markdown_report(
        symbols=symbols,
        entitlement={
            "http_status": 403,
            "entitled": False,
            "socket_url": None,
            "message": "forbidden",
        },
        data_symbol_updates=(sample,),
        data_depth_updates=(),
        tbt_updates=(),
        data_errors={"SymbolUpdate": (), "DepthUpdate": ()},
        tbt_errors=("not entitled",),
        generated_at=now,
    )
    assert "## Provider capability matrix" in report
    assert "## CAS sufficiency" in report
    assert "aggressor_side unavailable" in report
    assert "Not sufficient for CAS" in report
