"""DISC-A23: batched REST /quotes must normalize every requested symbol."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.data.events import RawMarketCapture
from trading.data.normalize import normalize_fyers_quotes_batch
from trading.domain.clock import FrozenClock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource
from trading.domain.primitives import Price, TickSize
from trading.runtime.paper_session import _quotes_for_symbols
from trading.runtime.rest_quote_monitor import RestQuoteMonitor

NOW = datetime(2026, 9, 14, 6, 15, tzinfo=UTC)
SYMBOLS = (
    "NSE:NIFTY26SEP24000CE",
    "NSE:NIFTY26SEP24100CE",
    "NSE:NIFTY26SEP24200CE",
)


def _batched_capture(
    rows: list[dict[str, object]],
    *,
    capture_id: str = "batch-quotes",
) -> RawMarketCapture:
    return RawMarketCapture(
        capture_id=capture_id,
        provider="fyers",
        endpoint="quotes",
        received_at=NOW,
        payload={"s": "ok", "d": rows},
        http_status=200,
    )


def _ok_row(symbol: str, *, bid: float, ask: float, lp: float) -> dict[str, object]:
    return {
        "n": symbol,
        "s": "ok",
        "v": {"lp": lp, "bid": bid, "ask": ask},
    }


class TestNormalizeFyersQuotesBatch:
    def test_batched_payload_returns_all_requested_symbols(self) -> None:
        """Invariant 6: every ok REST row must be available to protection."""
        capture = _batched_capture(
            [
                _ok_row(SYMBOLS[0], bid=90.0, ask=90.2, lp=90.1),
                _ok_row(SYMBOLS[1], bid=80.0, ask=80.2, lp=80.1),
                _ok_row(SYMBOLS[2], bid=70.0, ask=70.2, lp=70.1),
            ]
        )
        events = normalize_fyers_quotes_batch(
            capture,
            symbols=SYMBOLS,
            normalization_version="1",
            raw_ref="test://quotes",
        )
        assert set(events) == set(SYMBOLS)
        for symbol in SYMBOLS:
            quotes = events[symbol].payload["quotes"]
            assert isinstance(quotes, list)
            assert len(quotes) == 1
            assert quotes[0]["symbol"] == symbol

    def test_err_or_incomplete_rows_are_omitted(self) -> None:
        """Invariant 6: invalid REST rows must not masquerade as fresh quotes."""
        capture = _batched_capture(
            [
                _ok_row(SYMBOLS[0], bid=90.0, ask=90.2, lp=90.1),
                {
                    "n": SYMBOLS[1],
                    "s": "error",
                    "v": {"lp": 80.1, "bid": 80.0, "ask": 80.2},
                },
                {
                    "n": SYMBOLS[2],
                    "s": "ok",
                    "v": {"lp": 70.1, "bid": 70.0},
                },
            ]
        )
        events = normalize_fyers_quotes_batch(
            capture,
            symbols=SYMBOLS,
            normalization_version="1",
            raw_ref="test://quotes",
        )
        assert set(events) == {SYMBOLS[0]}


class TestQuotesForSymbols:
    def test_returns_market_quotes_for_all_ok_rows(self) -> None:
        capture = _batched_capture(
            [
                _ok_row(SYMBOLS[0], bid=90.0, ask=90.2, lp=90.1),
                _ok_row(SYMBOLS[1], bid=80.0, ask=80.2, lp=80.1),
                _ok_row(SYMBOLS[2], bid=70.0, ask=70.2, lp=70.1),
            ]
        )
        quotes = _quotes_for_symbols(capture, SYMBOLS)
        assert set(quotes) == set(SYMBOLS)
        tick = TickSize.of(Decimal("0.05"))
        assert quotes[SYMBOLS[0]].bid == Price.snap("90.00", tick)
        assert quotes[SYMBOLS[1]].ask == Price.snap("80.20", tick)

    def test_skips_err_and_missing_bid_ask_rows(self) -> None:
        capture = _batched_capture(
            [
                _ok_row(SYMBOLS[0], bid=90.0, ask=90.2, lp=90.1),
                {
                    "n": SYMBOLS[1],
                    "s": "error",
                    "v": {"lp": 80.1, "bid": 80.0, "ask": 80.2},
                },
                {
                    "n": SYMBOLS[2],
                    "s": "ok",
                    "v": {"lp": 70.1},
                },
            ]
        )
        quotes = _quotes_for_symbols(capture, SYMBOLS)
        assert set(quotes) == {SYMBOLS[0]}


class TestProtectionRestDelivery:
    def test_rest_monitor_delivers_all_leg_quotes(self) -> None:
        """Invariant 6: protection must receive every held-leg REST quote."""
        capture = _batched_capture(
            [
                _ok_row(SYMBOLS[0], bid=90.0, ask=90.2, lp=90.1),
                _ok_row(SYMBOLS[1], bid=80.0, ask=80.2, lp=80.1),
                _ok_row(SYMBOLS[2], bid=70.0, ask=70.2, lp=70.1),
            ]
        )
        clock = FrozenClock(NOW)
        received: dict[str, MarketQuote] = {}

        def fetch(symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            return _quotes_for_symbols(capture, symbols)

        rest = RestQuoteMonitor(clock, fetch, poll_seconds=1)
        rest.subscribe(frozenset(SYMBOLS))

        def handler(
            symbol: str,
            quote: MarketQuote,
            source: QuoteMonitorSource,
            received_at: datetime,
        ) -> None:
            assert source is QuoteMonitorSource.REST
            assert received_at == NOW
            received[symbol] = quote

        rest.start()
        rest.set_handler(handler)
        rest.tick()
        assert set(received) == set(SYMBOLS)
