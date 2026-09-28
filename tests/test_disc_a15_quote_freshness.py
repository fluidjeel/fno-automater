"""DISC-A15: quote freshness from fetch/calculation time, not bar event time."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from tests.test_risk_gateway import instrument_spec
from trading.config import load_config, load_paper_data_requirements
from trading.config.discovery import load_discovery_config
from trading.config.schema import FreshnessRules
from trading.data.events import RawMarketCapture
from trading.data.normalize import (
    normalize_fyers_history,
    normalize_fyers_option_chain,
    normalize_fyers_quotes,
)
from trading.data.quality import assess_combined_snapshot
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    Greeks,
    IntentLeg,
)
from trading.domain.contracts.paper_data import PaperDataField, PaperDataPresence
from trading.domain.contracts.snapshot import SnapshotTimes
from trading.domain.enums import DataQuality, EntryProfile, OptionType, ReasonCode, Side
from trading.risk.snapshot_bundle import validate_leg_snapshot_bundle
from trading.safety.paper_data import PaperDataInputs, assess_paper_data
from trading.strategies.base import StrategyContext
from trading.strategies.macro import MacroAssessment, MacroBias
from trading.strategies.multileg_options import BullPutCreditStrategy
from trading.strategies.quote_freshness import quote_freshness_limits

REPO_ROOT = Path(__file__).resolve().parent.parent
DISCOVERY = load_discovery_config(REPO_ROOT / "config" / "discovery.yaml").config
REQUIREMENTS = load_paper_data_requirements(REPO_ROOT / "config" / "paper_data.yaml")
ACCOUNT_CONFIG = load_config(REPO_ROOT / "config" / "base.yaml")
CHAIN_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fyers_option_chain_nifty.json"
QUOTES_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fyers_quotes_nifty.json"

# Live-session pattern: decision at 04:04 UTC against a 04:00 bar open.
NOW = datetime(2026, 9, 28, 4, 4, 0, tzinfo=UTC)
BAR_OPEN = datetime(2026, 9, 28, 4, 0, 0, tzinfo=UTC)


def _bar_open_times() -> SnapshotTimes:
    """Fresh quote/calculation with a stale 5m bar event_time."""
    return f.snapshot_times(
        event_time=BAR_OPEN,
        source_time=BAR_OPEN,
        receive_time=NOW - timedelta(seconds=5),
        calculation_time=NOW - timedelta(seconds=2),
    )


def _paper_inputs(snapshot: FeatureSnapshot) -> PaperDataInputs:
    instrument = instrument_spec()
    return PaperDataInputs(
        now=NOW,
        snapshots=(snapshot,),
        event_risk=f.event_risk_state(
            as_of=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        ),
        portfolio=f.portfolio_snapshot(),
        broker_state_ok=True,
        margin_confirmed=True,
        margin_required=f.money("6900"),
        instruments={instrument.trading_symbol: instrument},
    )


def _option_snapshot() -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id="SNAP-BAR-OPEN",
        contract=f.option_contract(),
        times=_bar_open_times(),
        market=f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            last=f.price("92.00"),
            volume=5000,
            bid_size=300,
            ask_size=300,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.52"),
            ),
        ),
        features={"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
    )


def _capture(path: Path, received_at: datetime) -> RawMarketCapture:
    payload = json.loads(path.read_text(encoding="utf-8"))
    data = payload.get("data")
    if isinstance(data, dict):
        data["timestamp"] = int(received_at.timestamp())
    return RawMarketCapture(
        capture_id=f"cap-{received_at.isoformat()}",
        provider="fyers",
        endpoint="fixture",
        received_at=received_at,
        payload=payload,
        http_status=200,
    )


class TestDiscA15PaperData:
    def test_discovery_ignores_bar_open_event_time(self) -> None:
        """P0 must not DATA_STALE when quote is seconds old but bar open is 4m."""
        assessment = assess_paper_data(
            REQUIREMENTS,
            _paper_inputs(_option_snapshot()),
            entry_profile=EntryProfile.DISCOVERY,
            discovery_config=DISCOVERY,
        )
        assert assessment.p0_ok
        ltp = next(row for row in assessment.results if row.field is PaperDataField.LTP)
        assert ltp.presence is PaperDataPresence.PRESENT

    def test_strict_still_uses_bar_event_time(self) -> None:
        """STRICT P0 age stays on event_time so existing 120s gate is unchanged."""
        assessment = assess_paper_data(
            REQUIREMENTS,
            _paper_inputs(_option_snapshot()),
            entry_profile=EntryProfile.STRICT,
        )
        assert not assessment.p0_ok
        ltp = next(row for row in assessment.results if row.field is PaperDataField.LTP)
        assert ltp.presence is PaperDataPresence.STALE
        assert ltp.reason_code is ReasonCode.DATA_STALE


class TestDiscA15QualityState:
    def test_combined_quality_uses_receive_time_not_bar_open(self) -> None:
        """Layer-1 quality must not mark STALE on a just-fetched 5m bar."""
        chain = normalize_fyers_option_chain(
            _capture(CHAIN_FIXTURE, NOW),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="c.json",
        )
        quote = normalize_fyers_quotes(
            _capture(QUOTES_FIXTURE, NOW),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="q.json",
        )
        history_payload = {
            "s": "ok",
            "code": 200,
            "message": "",
            "candles": [
                [
                    int(BAR_OPEN.timestamp()),
                    24400.0,
                    24550.0,
                    24380.0,
                    24500.5,
                    1234567,
                ]
            ],
        }
        bar = normalize_fyers_history(
            RawMarketCapture(
                capture_id="cap-bar-open",
                provider="fyers",
                endpoint="history",
                received_at=NOW,
                payload=history_payload,
                http_status=200,
            ),
            symbol="NSE:NIFTY50-INDEX",
            resolution="5",
            normalization_version="1",
            raw_ref="h.json",
        )
        assert bar.event_time == BAR_OPEN
        assert bar.receive_time == NOW
        report = assess_combined_snapshot(
            chain=chain,
            quote=quote,
            bar=bar,
            now=NOW,
            chain_max_age_ms=120_000,
            quote_max_age_ms=60_000,
            bar_max_age_ms=300_000,
        )
        assert report.permits_new_exposure
        assert report.state is not DataQuality.STALE


class TestDiscA15SnapshotBundle:
    def _freshness(self) -> FreshnessRules:
        return ACCOUNT_CONFIG.config.freshness

    def test_discovery_bundle_uses_hard_limit_and_calculation_time(self) -> None:
        snap = _option_snapshot()
        intent = f.intent(
            snapshot_id="SNAP-PARENT",
            legs=(
                IntentLeg(
                    leg_id="leg-1",
                    contract=snap.contract,
                    side=Side.BUY,
                    ratio=1,
                ),
            ),
        )
        bundle = validate_leg_snapshot_bundle(
            intent,
            {"leg-1": snap},
            now=NOW,
            freshness=self._freshness(),
            entry_profile=EntryProfile.DISCOVERY,
            discovery_config=DISCOVERY,
        )
        assert bundle.reason is None

    def test_strict_bundle_still_rejects_bar_open_age(self) -> None:
        snap = _option_snapshot()
        intent = f.intent(
            snapshot_id="SNAP-PARENT",
            legs=(
                IntentLeg(
                    leg_id="leg-1",
                    contract=snap.contract,
                    side=Side.BUY,
                    ratio=1,
                ),
            ),
        )
        bundle = validate_leg_snapshot_bundle(
            intent,
            {"leg-1": snap},
            now=NOW,
            freshness=self._freshness(),
            entry_profile=EntryProfile.STRICT,
        )
        assert bundle.reason is ReasonCode.DATA_STALE


class TestDiscA15DataInvalidLink:
    def test_binder_permits_exposure_with_fresh_receive_quality(self) -> None:
        """DATA_INVALID at BIND was quality STALE from bar event_time; receive fixes it."""
        from trading.data.config import SessionConfig

        chain = normalize_fyers_option_chain(
            _capture(CHAIN_FIXTURE, NOW),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="c.json",
        )
        report = assess_combined_snapshot(
            chain=chain,
            quote=None,
            bar=None,
            now=NOW,
            chain_max_age_ms=120_000,
            quote_max_age_ms=60_000,
            bar_max_age_ms=300_000,
            session=SessionConfig(
                timezone="Asia/Kolkata",
                open_local="09:15",
                close_local="15:30",
                verified_source="test",
                verified_at=date(2026, 9, 13),
                segment="NSE_FO",
            ),
        )
        assert report.state is not DataQuality.STALE
        underlying = f.snapshot(
            times=_bar_open_times(),
            quality=report,
            market=f.quote(last=f.price("24500"), close=f.price("24500")),
        )
        assert underlying.permits_new_exposure

    def test_multileg_strategy_not_data_invalid_on_bar_open_pattern(self) -> None:
        times = _bar_open_times()
        strict_ms, hard_ms = quote_freshness_limits(
            entry_profile=EntryProfile.DISCOVERY,
            freshness=ACCOUNT_CONFIG.config.freshness,
            discovery_config=DISCOVERY,
        )
        underlying = f.snapshot(
            times=times,
            market=f.quote(last=f.price("24500"), close=f.price("24500")),
        )
        leg = f.snapshot(
            contract=f.option_contract(
                symbol="NIFTY26SEP24000PE", option_type=OptionType.PUT
            ),
            times=times,
            market=f.quote(
                bid=f.price("90"), ask=f.price("90.10"), last=f.price("90.05")
            ),
            derivatives=DerivativesContext(
                days_to_expiry=10,
                open_interest=5000,
                option_type=OptionType.PUT,
                underlying_price=f.price("24500"),
                greeks=Greeks(
                    model="fixture",
                    calculation_version="1",
                    converged=True,
                    implied_volatility=Decimal("15"),
                    delta=Decimal("-0.30"),
                ),
            ),
            features={"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
        )
        wing = f.snapshot(
            contract=f.option_contract(
                symbol="NIFTY26SEP23800PE",
                strike=Decimal("23800"),
                option_type=OptionType.PUT,
            ),
            times=times,
            market=f.quote(
                bid=f.price("40"), ask=f.price("40.10"), last=f.price("40.05")
            ),
            derivatives=DerivativesContext(
                days_to_expiry=10,
                open_interest=5000,
                option_type=OptionType.PUT,
                underlying_price=f.price("24500"),
                greeks=Greeks(
                    model="fixture",
                    calculation_version="1",
                    converged=True,
                    implied_volatility=Decimal("15"),
                    delta=Decimal("-0.15"),
                ),
            ),
            features={"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
        )
        ctx = StrategyContext(
            underlying=underlying,
            candidates=(leg, wing),
            view=f.portfolio_view(),
            now=NOW,
            macro=MacroAssessment(
                regime="NEUTRAL_VOLATILITY",
                directional_bias=MacroBias.BULLISH,
                confidence=Decimal("0.8"),
                fresh_until=NOW + timedelta(hours=1),
                evidence_ids=("src-1",),
                model_version="macro-v1",
            ),
            entry_profile=EntryProfile.DISCOVERY,
            strict_quote_max_age_ms=strict_ms,
            hard_quote_max_age_ms=hard_ms,
        )
        decision = BullPutCreditStrategy().evaluate(ctx)
        assert not any(
            rejection.reason is ReasonCode.DATA_INVALID
            for rejection in decision.rejections
        )
