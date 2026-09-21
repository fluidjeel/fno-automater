"""Build FeatureSnapshot from canonical market events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from trading.data.cas_features import cas_snapshot_version, compute_cas_features
from trading.data.config import UnderlyingConfig
from trading.data.events import CanonicalMarketEvent
from trading.data.prices import positive_decimal as _positive
from trading.domain.contracts import (
    ContractRef,
    DataQualityReport,
    FeatureSnapshot,
    InstrumentSpec,
    Lineage,
    MarketQuote,
    SnapshotTimes,
    Versions,
)
from trading.domain.primitives import Price, TickSize

__all__ = [
    "DEFAULT_TICK_SIZE",
    "MarketSnapshotBuilder",
    "OptionChainSnapshotBuilder",
    "PriceUnavailableError",
]

# Used only when neither the instrument master nor a captured reference event
# supplies a tick size, which happens when replaying pre-catalog events.
DEFAULT_TICK_SIZE = "0.05"


class PriceUnavailableError(ValueError):
    """Raised when no feed supplies an authoritative price for the cycle."""


def _latest_event(
    events: Sequence[CanonicalMarketEvent],
    event_type: str,
) -> CanonicalMarketEvent | None:
    matches = [event for event in events if event.event_type == event_type]
    return matches[-1] if matches else None


def _signed(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal, str)):
        return Decimal(str(value))
    return None


def _int_qty(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, (float, Decimal, str)):
        parsed = int(Decimal(str(value)))
        return parsed if parsed >= 0 else None
    return None


def _atm_leg(strikes: list[Any], spot: Decimal) -> dict[str, Any] | None:
    legs: list[dict[str, Any]] = []
    for row in strikes:
        if isinstance(row, dict) and row.get("option_type") == "CE":
            legs.append(row)
    if not legs:
        return None
    return min(
        legs,
        key=lambda row: abs(Decimal(str(row.get("strike_price", 0) or 0)) - spot),
    )


class MarketSnapshotBuilder:
    """Build FeatureSnapshot from option chain, quotes, bars, depth and reference."""

    def __init__(
        self,
        underlying: UnderlyingConfig,
        *,
        config_version: str,
        config_checksum: str,
        code_version: str,
        greeks_calculation_version: str = "fyers_chain",
        instrument_spec: InstrumentSpec | None = None,
    ) -> None:
        self._underlying = underlying
        self._greeks_calculation_version = greeks_calculation_version
        self._instrument_spec = instrument_spec
        self._versions = Versions(
            code_version=code_version,
            config_version=config_version,
            config_checksum=config_checksum,
        )

    def _tick_size(self, reference: CanonicalMarketEvent | None) -> TickSize:
        """Prefer the instrument master, then the captured reference event.

        Replay passes no spec and reads the tick recorded in the reference event
        at capture time, which is what keeps a replay deterministic when the
        master is later refreshed. The live path writes the spec tick into that
        event, so the two agree. `DEFAULT_TICK_SIZE` is a last resort for events
        captured before the instrument catalog existed; it is not an authority.
        """
        if self._instrument_spec is not None:
            return TickSize.of(self._instrument_spec.tick_size)
        if reference is not None:
            ref_tick = reference.payload.get("tick_size")
            if isinstance(ref_tick, str) and ref_tick:
                return TickSize.of(ref_tick)
        return TickSize.of(DEFAULT_TICK_SIZE)

    def build_index(
        self,
        events: Sequence[CanonicalMarketEvent],
        *,
        as_of: datetime,
        quality: DataQualityReport,
    ) -> FeatureSnapshot:
        """Build a cash-index snapshot from quotes and bars (no option chain)."""
        if not events:
            raise ValueError("cannot build a snapshot from zero events")
        quote = _latest_event(events, "QUOTE_SNAPSHOT")
        if quote is None:
            raise ValueError("quote event is required for index snapshot build")
        bar = _latest_event(events, "BAR_SNAPSHOT")
        reference = _latest_event(events, "INSTRUMENT_REFERENCE")
        depth = _latest_event(events, "DEPTH_SNAPSHOT")
        tick = self._tick_size(reference)
        bid = ask = last = open_ = high = low = close = None
        volume: int | None = None
        bid_size: int | None = None
        ask_size: int | None = None
        bar_is_final = False
        quotes = quote.payload.get("quotes", [])
        if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict):
            row = quotes[0]
            bid = _positive(row.get("bid"))
            ask = _positive(row.get("ask"))
            last = _positive(row.get("lp", row.get("ltp")))
            open_ = _positive(row.get("open_price"))
            high = _positive(row.get("high_price"))
            low = _positive(row.get("low_price"))
            close = _positive(row.get("prev_close_price"))
            volume = _int_qty(row.get("volume"))
        if depth is not None:
            bids = depth.payload.get("bid_levels", [])
            asks = depth.payload.get("ask_levels", [])
            if isinstance(bids, list) and bids and isinstance(bids[0], dict):
                bid = _positive(bids[0].get("price")) or bid
                bid_size = _int_qty(bids[0].get("volume"))
            if isinstance(asks, list) and asks and isinstance(asks[0], dict):
                ask = _positive(asks[0].get("price")) or ask
                ask_size = _int_qty(asks[0].get("volume"))
        if bar is not None:
            bars = bar.payload.get("bars", [])
            if isinstance(bars, list) and bars and isinstance(bars[-1], dict):
                latest_bar = bars[-1]
                open_ = _positive(latest_bar.get("open")) or open_
                high = _positive(latest_bar.get("high")) or high
                low = _positive(latest_bar.get("low")) or low
                close = _positive(latest_bar.get("close")) or close
                last = _positive(latest_bar.get("close")) or last
                volume = _int_qty(latest_bar.get("volume")) or volume
                bar_is_final = bool(bar.payload.get("is_final", True))
        if last is None or last <= 0:
            raise PriceUnavailableError(
                "no authoritative last price in quote, depth or bar"
            )
        market = MarketQuote(
            last=Price.snap(last, tick),
            bid=Price.snap(bid, tick) if bid is not None else None,
            ask=Price.snap(ask, tick) if ask is not None else None,
            open=Price.snap(open_, tick) if open_ is not None else None,
            high=Price.snap(high, tick) if high is not None else None,
            low=Price.snap(low, tick) if low is not None else None,
            close=Price.snap(close, tick) if close is not None else None,
            volume=volume,
            bid_size=bid_size,
            ask_size=ask_size,
            bar_is_final=bar_is_final,
        )
        latest = max(events, key=lambda event: event.receive_time)
        times = SnapshotTimes(
            event_time=latest.event_time,
            source_time=latest.source_time,
            receive_time=latest.receive_time,
            calculation_time=as_of,
        )
        features: dict[str, Decimal] = {}
        if self._underlying.underlying == "INDIAVIX":
            features["india_vix"] = last
        if self._instrument_spec is not None:
            features["lot_size"] = Decimal(self._instrument_spec.lot_size)
        macro = quote.payload.get("macro_news_factor")
        if isinstance(macro, dict):
            sentiment = macro.get("sentiment")
            coverage = macro.get("coverage")
            event_count = macro.get("event_count")
            if isinstance(sentiment, str | int | Decimal) and not isinstance(
                sentiment, bool
            ):
                features["macro_news_sentiment"] = Decimal(str(sentiment))
            if isinstance(coverage, str | int | Decimal) and not isinstance(
                coverage, bool
            ):
                features["macro_news_coverage"] = Decimal(str(coverage))
            if isinstance(event_count, int) and not isinstance(event_count, bool):
                features["macro_news_event_count"] = Decimal(event_count)
        source_ids = [event.symbol for event in events]
        raw_refs = [event.raw_ref for event in events]
        macro_ids = self._macro_source_ids(quote.payload)
        macro_refs = self._macro_source_refs(quote.payload)
        lineage = Lineage(
            provider="fyers",
            source_ids=tuple(dict.fromkeys((*source_ids, *macro_ids))),
            raw_event_refs=tuple(dict.fromkeys((*raw_refs, *macro_refs))),
            normalization_version=quote.normalization_version,
            versions=self._versions,
        )
        contract = ContractRef(
            exchange=self._underlying.exchange,
            symbol=self._underlying.underlying,
            instrument_kind=self._underlying.instrument_kind,
            asset_class=self._underlying.asset_class,
            underlying=self._underlying.underlying,
        )
        return FeatureSnapshot(
            snapshot_id=f"SNAP-{quote.event_id}",
            contract=contract,
            times=times,
            market=market,
            feature_set_version=self._underlying.feature_set_version,
            features=features,
            quality=quality,
            lineage=lineage,
        )

    def build(
        self,
        events: Sequence[CanonicalMarketEvent],
        *,
        as_of: datetime,
        quality: DataQualityReport,
    ) -> FeatureSnapshot:
        if not events:
            raise ValueError("cannot build a snapshot from zero events")
        chain = _latest_event(events, "OPTION_CHAIN_SNAPSHOT")
        if chain is None:
            raise ValueError("option chain event is required for snapshot build")
        quote = _latest_event(events, "QUOTE_SNAPSHOT")
        bar = _latest_event(events, "BAR_SNAPSHOT")
        reference = _latest_event(events, "INSTRUMENT_REFERENCE")
        depth = _latest_event(events, "DEPTH_SNAPSHOT")
        tick = self._tick_size(reference)
        spot = Decimal("0")
        strikes = chain.payload.get("strikes", [])
        if isinstance(strikes, list):
            for row in strikes:
                if isinstance(row, dict) and row.get("option_type") in {"", None}:
                    spot = Decimal(str(row.get("ltp", row.get("fp", 0)) or 0))
                    break
            if spot <= 0 and strikes and isinstance(strikes[0], dict):
                first = strikes[0]
                spot = Decimal(str(first.get("ltp", first.get("fp", 0)) or 0))
        bid = ask = last = open_ = high = low = close = None
        volume: int | None = None
        bid_size: int | None = None
        ask_size: int | None = None
        bar_is_final = False
        if quote is not None:
            quotes = quote.payload.get("quotes", [])
            if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict):
                row = quotes[0]
                bid = _positive(row.get("bid"))
                ask = _positive(row.get("ask"))
                last = _positive(row.get("lp", row.get("ltp")))
                open_ = _positive(row.get("open_price"))
                high = _positive(row.get("high_price"))
                low = _positive(row.get("low_price"))
                close = _positive(row.get("prev_close_price"))
                volume = _int_qty(row.get("volume"))
        if depth is not None:
            bids = depth.payload.get("bid_levels", [])
            asks = depth.payload.get("ask_levels", [])
            if isinstance(bids, list) and bids and isinstance(bids[0], dict):
                bid = _positive(bids[0].get("price")) or bid
                bid_size = _int_qty(bids[0].get("volume"))
            if isinstance(asks, list) and asks and isinstance(asks[0], dict):
                ask = _positive(asks[0].get("price")) or ask
                ask_size = _int_qty(asks[0].get("volume"))
            buy = _int_qty(depth.payload.get("total_buy_qty"))
            sell = _int_qty(depth.payload.get("total_sell_qty"))
            if bid_size is None:
                bid_size = buy
            if ask_size is None:
                ask_size = sell
        if bar is not None:
            bars = bar.payload.get("bars", [])
            if isinstance(bars, list) and bars and isinstance(bars[-1], dict):
                latest_bar = bars[-1]
                open_ = _positive(latest_bar.get("open")) or open_
                high = _positive(latest_bar.get("high")) or high
                low = _positive(latest_bar.get("low")) or low
                close = _positive(latest_bar.get("close")) or close
                last = _positive(latest_bar.get("close")) or last
                volume = _int_qty(latest_bar.get("volume")) or volume
                bar_is_final = bool(bar.payload.get("is_final", True))
        if last is None and spot > 0:
            last = spot
        if last is None or last <= 0:
            raise PriceUnavailableError(
                "no authoritative last price in quote, depth, bar or chain; the "
                "quality gate must mark the cycle INVALID rather than pricing "
                "against a substituted value"
            )
        market = MarketQuote(
            last=Price.snap(last, tick),
            bid=Price.snap(bid, tick) if bid is not None else None,
            ask=Price.snap(ask, tick) if ask is not None else None,
            open=Price.snap(open_, tick) if open_ is not None else None,
            high=Price.snap(high, tick) if high is not None else None,
            low=Price.snap(low, tick) if low is not None else None,
            close=Price.snap(close, tick) if close is not None else None,
            volume=volume,
            bid_size=bid_size,
            ask_size=ask_size,
            bar_is_final=bar_is_final,
        )
        latest = max(events, key=lambda event: event.receive_time)
        times = SnapshotTimes(
            event_time=latest.event_time,
            source_time=latest.source_time,
            receive_time=latest.receive_time,
            calculation_time=as_of,
        )
        features: dict[str, Decimal] = {}
        if isinstance(strikes, list) and spot > 0:
            atm = _atm_leg(strikes, spot)
            if atm is not None:
                greeks = atm.get("greeks")
                greeks_dict = greeks if isinstance(greeks, dict) else {}
                iv = _positive(greeks_dict.get("iv", atm.get("iv")))
                if iv is not None:
                    features["atm_iv"] = iv
                delta = _signed(greeks_dict.get("delta"))
                gamma = _signed(greeks_dict.get("gamma"))
                theta = _signed(greeks_dict.get("theta"))
                vega = _signed(greeks_dict.get("vega"))
                if delta is not None:
                    features["atm_delta"] = delta
                if gamma is not None:
                    features["atm_gamma"] = gamma
                if theta is not None:
                    features["atm_theta"] = theta
                if vega is not None:
                    features["atm_vega"] = vega
        call_oi = _int_qty(chain.payload.get("call_oi"))
        put_oi = _int_qty(chain.payload.get("put_oi"))
        if call_oi is not None:
            features["call_oi"] = Decimal(call_oi)
        if put_oi is not None:
            features["put_oi"] = Decimal(put_oi)
        if call_oi and put_oi is not None:
            features["pcr"] = (Decimal(put_oi) / Decimal(call_oi)).quantize(
                Decimal("0.0001")
            )
        vix = chain.payload.get("india_vix")
        if isinstance(vix, dict):
            vix_ltp = _positive(vix.get("ltp"))
            if vix_ltp is not None:
                features["india_vix"] = vix_ltp
        if reference is not None:
            features["expiry_count"] = Decimal(
                int(reference.payload.get("expiry_count", 0))
            )
            lot = reference.payload.get("lot_size")
            if isinstance(lot, int) and not isinstance(lot, bool):
                features["lot_size"] = Decimal(lot)
        if self._instrument_spec is not None:
            features["lot_size"] = Decimal(self._instrument_spec.lot_size)
        if depth is not None:
            buy = _int_qty(depth.payload.get("total_buy_qty"))
            sell = _int_qty(depth.payload.get("total_sell_qty"))
            if buy is not None:
                features["depth_bid_qty"] = Decimal(buy)
            if sell is not None:
                features["depth_ask_qty"] = Decimal(sell)
        for key, value in compute_cas_features(events).items():
            features[key] = value
        feature_set_version = (
            cas_snapshot_version(features) or self._underlying.feature_set_version
        )
        macro = chain.payload.get("macro_news_factor")
        if isinstance(macro, dict):
            sentiment = macro.get("sentiment")
            coverage = macro.get("coverage")
            event_count = macro.get("event_count")
            if isinstance(sentiment, str | int | Decimal) and not isinstance(
                sentiment, bool
            ):
                features["macro_news_sentiment"] = Decimal(str(sentiment))
            if isinstance(coverage, str | int | Decimal) and not isinstance(
                coverage, bool
            ):
                features["macro_news_coverage"] = Decimal(str(coverage))
            if isinstance(event_count, int) and not isinstance(event_count, bool):
                features["macro_news_event_count"] = Decimal(event_count)
        source_ids = [event.symbol for event in events]
        raw_refs = [event.raw_ref for event in events]
        if self._greeks_calculation_version:
            source_ids.append(self._greeks_calculation_version)
        if self._instrument_spec is not None:
            source_ids.append(self._instrument_spec.source)
        macro_ids = self._macro_source_ids(chain.payload)
        macro_refs = self._macro_source_refs(chain.payload)
        lineage = Lineage(
            provider="fyers",
            source_ids=tuple(dict.fromkeys((*source_ids, *macro_ids))),
            raw_event_refs=tuple(dict.fromkeys((*raw_refs, *macro_refs))),
            normalization_version=chain.normalization_version,
            versions=self._versions,
        )
        contract = ContractRef(
            exchange=self._underlying.exchange,
            symbol=self._underlying.underlying,
            instrument_kind=self._underlying.instrument_kind,
            asset_class=self._underlying.asset_class,
            underlying=self._underlying.underlying,
        )
        return FeatureSnapshot(
            snapshot_id=f"SNAP-{chain.event_id}",
            contract=contract,
            times=times,
            market=market,
            feature_set_version=feature_set_version,
            features=features,
            quality=quality,
            lineage=lineage,
        )

    @staticmethod
    def _macro_source_ids(payload: dict[str, object]) -> tuple[str, ...]:
        macro = payload.get("macro_news_factor")
        if not isinstance(macro, dict):
            return ()
        ids = macro.get("evidence_ids", ())
        return (
            tuple(value for value in ids if isinstance(value, str))
            if isinstance(ids, list)
            else ()
        )

    @staticmethod
    def _macro_source_refs(payload: dict[str, object]) -> tuple[str, ...]:
        macro = payload.get("macro_news_factor")
        if not isinstance(macro, dict):
            return ()
        refs = macro.get("evidence_refs", ())
        if not isinstance(refs, list):
            return ()
        return tuple(value for value in refs if isinstance(value, str))


OptionChainSnapshotBuilder = MarketSnapshotBuilder
