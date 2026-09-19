"""Offline NIFTY breakout identification + MAE/MFE judgment. No broker, no LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from trading.analytics.judgment import label_excursion
from trading.config.evaluation import EvaluationConfig
from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.contracts.common import (
    ContractRef,
    DataQualityReport,
    Lineage,
    Versions,
)
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.snapshot import (
    DerivativesContext,
    Greeks,
    MarketQuote,
    SnapshotTimes,
)
from trading.domain.enums import (
    AssetClass,
    DataQuality,
    Exchange,
    InstrumentKind,
    OptionType,
)
from trading.domain.primitives import Price, TickSize
from trading.identification.allow_table import (
    allowed_families_for,
    session_bucket_for,
)
from trading.identification.binders import (
    BoundCandidates,
    bind_cas_microstructure,
    bind_debit_spread,
    bind_long_option,
)
from trading.identification.config import IdentificationPolicy
from trading.identification.router import route_nifty_options

__all__ = [
    "BreakoutBucket",
    "BreakoutRow",
    "ChainQuality",
    "JudgmentVerdict",
    "format_breakout_report",
    "persist_breakout_report",
    "run_breakout_scenarios",
    "scoreboard",
    "verdict_for",
]

_TICK = TickSize.of("0.05")
_LOT = Decimal(75)
OPEN_AUCTION = datetime(2026, 9, 21, 3, 35, tzinfo=UTC)  # 09:05 IST
CLOSE_AUCTION = datetime(2026, 9, 21, 10, 2, tzinfo=UTC)  # 15:32 IST
CONTINUOUS = datetime(2026, 9, 21, 5, 30, tzinfo=UTC)  # 11:00 IST
WEEKLY_EXPIRY = date(2026, 9, 29)
MONTHLY_EXPIRY = date(2026, 10, 29)
_LINEAGE = Lineage(
    provider="breakout-sim",
    source_ids=("sim-1",),
    raw_event_refs=("sim-1",),
    normalization_version="1",
    versions=Versions(
        code_version="0.1.0", config_version="1", config_checksum="breakout"
    ),
)


class BreakoutBucket(StrEnum):
    CAS = "CAS"
    POSITIONAL = "positional"
    DIRECTION = "direction"


class ChainQuality(StrEnum):
    LIQUID = "liquid"
    WIDE = "wide"
    THIN_OI = "thin_oi"
    DELTA_MISS = "delta_miss"


class JudgmentVerdict(StrEnum):
    CORRECT_ENTER = "CORRECT_ENTER"
    LOSS_SHOULD_HAVE_PASSED = "LOSS_SHOULD_HAVE_PASSED"
    CORRECT_PASS = "CORRECT_PASS"  # noqa: S105 - verdict label, not a secret
    MISSED = "MISSED"
    NO_LABEL = "NO_LABEL"


@dataclass(frozen=True, slots=True)
class BreakoutRow:
    name: str
    bucket: BreakoutBucket
    regime: dict[str, str]
    paper_winner: str | None
    paper_tenor: str | None
    forced_choice: bool
    failed_gates: tuple[str, ...]
    binder_eligible: bool
    entered: bool
    judgment_label: str
    verdict: JudgmentVerdict
    mae: str
    mfe: str
    net_mfe: str
    charges: str
    winner_score: str | None
    allowed_families: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bucket": self.bucket.value,
            "regime": self.regime,
            "paper_winner": self.paper_winner,
            "paper_tenor": self.paper_tenor,
            "forced_choice": self.forced_choice,
            "failed_gates": list(self.failed_gates),
            "binder_eligible": self.binder_eligible,
            "entered": self.entered,
            "judgment_label": self.judgment_label,
            "verdict": self.verdict.value,
            "mae": self.mae,
            "mfe": self.mfe,
            "net_mfe": self.net_mfe,
            "charges": self.charges,
            "winner_score": self.winner_score,
            "allowed_families": list(self.allowed_families),
        }


def verdict_for(*, entered: bool, should_enter: bool | None) -> JudgmentVerdict:
    """Map an identification decision onto the ex-post MAE/MFE label."""
    if should_enter is None:
        return JudgmentVerdict.NO_LABEL
    if entered and should_enter:
        return JudgmentVerdict.CORRECT_ENTER
    if entered and not should_enter:
        return JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED
    if not entered and not should_enter:
        return JudgmentVerdict.CORRECT_PASS
    return JudgmentVerdict.MISSED


def run_breakout_scenarios(
    policy: IdentificationPolicy,
    evaluation: EvaluationConfig,
) -> tuple[BreakoutRow, ...]:
    """Route 10 CAS + 10 positional + 10 direction cells and judge the path."""
    charges = evaluation.fill_model.charges_per_lot.require(
        "fill_model.charges_per_lot"
    )
    return tuple(
        _run_one(spec, policy=policy, charges_per_lot=charges, evaluation=evaluation)
        for spec in _SPECS
    )


def scoreboard(rows: tuple[BreakoutRow, ...] | list[BreakoutRow]) -> dict[str, Any]:
    """Wins/losses/precision grouped by bucket, then overall."""
    buckets = [item.value for item in BreakoutBucket]
    out: dict[str, Any] = {
        "by_bucket": {name: _bucket_stats(rows, name) for name in buckets},
    }
    out["overall"] = _bucket_stats(rows, None)
    return out


def format_breakout_report(rows: tuple[BreakoutRow, ...] | list[BreakoutRow]) -> str:
    """Readable per-bucket table plus the win/loss scoreboard."""
    lines: list[str] = []
    grouped: dict[str, list[BreakoutRow]] = {item.value: [] for item in BreakoutBucket}
    for row in rows:
        grouped[row.bucket.value].append(row)
    header = (
        f"{'name':<32} {'winner':<24} {'label':<13} {'verdict':<24} "
        f"{'mae':>8} {'mfe':>8} {'net':>8} gates"
    )
    for bucket in BreakoutBucket:
        lines.append(f"=== {bucket.value} ({len(grouped[bucket.value])}) ===")
        lines.append(header)
        lines.append("-" * len(header))
        for row in grouped[bucket.value]:
            winner = row.paper_winner or "None"
            gates = ",".join(row.failed_gates) if row.failed_gates else "-"
            lines.append(
                f"{row.name:<32} {winner:<24} {row.judgment_label:<13} "
                f"{row.verdict.value:<24} {row.mae:>8} {row.mfe:>8} "
                f"{row.net_mfe:>8} {gates}"
            )
        lines.append("")
    board = scoreboard(rows)
    lines.append("=== scoreboard ===")
    for name in [item.value for item in BreakoutBucket] + ["overall"]:
        stats = board["by_bucket"][name] if name != "overall" else board["overall"]
        precision = stats["precision"] if stats["precision"] is not None else "-"
        lines.append(
            f"{name:<12} n={stats['n']:<3} wins={stats['wins']:<3} "
            f"losses={stats['losses']:<3} correct_pass={stats['correct_pass']:<3} "
            f"missed={stats['missed']:<3} precision={precision}"
        )
    return "\n".join(lines)


def persist_breakout_report(
    rows: tuple[BreakoutRow, ...] | list[BreakoutRow],
    path: Path,
    *,
    generated_at: datetime,
) -> None:
    """Write the scoreboard and per-scenario rows as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "breakout-simulation-v1",
        "generated_at": generated_at.isoformat(),
        "scoreboard": scoreboard(rows),
        "scenarios": [row.to_json() for row in rows],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _bucket_stats(
    rows: tuple[BreakoutRow, ...] | list[BreakoutRow], bucket: str | None
) -> dict[str, Any]:
    selected = [row for row in rows if bucket is None or row.bucket.value == bucket]
    wins = sum(1 for row in selected if row.verdict is JudgmentVerdict.CORRECT_ENTER)
    losses = sum(
        1 for row in selected if row.verdict is JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED
    )
    correct_pass = sum(
        1 for row in selected if row.verdict is JudgmentVerdict.CORRECT_PASS
    )
    missed = sum(1 for row in selected if row.verdict is JudgmentVerdict.MISSED)
    no_label = sum(1 for row in selected if row.verdict is JudgmentVerdict.NO_LABEL)
    precision = None
    if wins + losses:
        precision = str(
            (Decimal(wins) / Decimal(wins + losses)).quantize(Decimal("0.0001"))
        )
    return {
        "n": len(selected),
        "wins": wins,
        "losses": losses,
        "correct_pass": correct_pass,
        "missed": missed,
        "no_label": no_label,
        "precision": precision,
    }


@dataclass(frozen=True, slots=True)
class _Spec:
    name: str
    bucket: BreakoutBucket
    trend: TrendState
    volatility: VolatilityState
    iv_percentile: Decimal
    iv_rv_ratio: Decimal
    event_state: str
    calculated_at: datetime
    sides: tuple[OptionType, ...]
    dte: int
    quality: ChainQuality
    mae: Decimal
    mfe: Decimal
    lots: int = 1
    macro_status: MacroStatus = MacroStatus.NEUTRAL
    equalize_scores: bool = False
    trend_score: Decimal = Decimal("0.72")
    realized_volatility_ratio: Decimal = Decimal("1.00")
    volume: int = 8000
    implied_volatility: Decimal = Decimal("14")


def _run_one(
    spec: _Spec,
    *,
    policy: IdentificationPolicy,
    charges_per_lot: Decimal,
    evaluation: EvaluationConfig,
) -> BreakoutRow:
    market = _market(spec, policy)
    expiry = (
        WEEKLY_EXPIRY if spec.dte <= policy.contracts.weekly_dte_max else MONTHLY_EXPIRY
    )
    chain = _chain(
        as_of=spec.calculated_at,
        option_types=spec.sides,
        dte=spec.dte,
        expiry=expiry,
        quality=spec.quality,
        volume=spec.volume,
        implied_volatility=spec.implied_volatility,
    )
    long_option = bind_long_option(chain, market=market, policy=policy)
    debit = bind_debit_spread(chain, market=market, policy=policy)
    cas = bind_cas_microstructure(chain, market=market, policy=policy)
    if spec.equalize_scores and long_option.binding.eligible and debit.binding.eligible:
        debit = BoundCandidates(
            binding=debit.binding.model_copy(
                update={"score": long_option.binding.score}
            ),
            candidates=debit.candidates,
            setup_features=debit.setup_features,
        )
    decision, _opportunities = route_nifty_options(
        market,
        long_option=long_option,
        debit_spread=debit,
        cas_microstructure=cas,
        policy=policy,
    )
    original = {
        "positional_long_option": long_option,
        "debit_spread": debit,
        "cas_microstructure": cas,
    }
    bound = original.get(decision.paper_winner or "")
    binder_eligible = bound is not None and bound.binding.eligible
    entered = decision.paper_winner is not None and binder_eligible
    should_enter = label_excursion(
        mae=spec.mae,
        mfe=spec.mfe,
        lots=spec.lots,
        charges_per_lot=charges_per_lot,
        thresholds=evaluation.judgment,
    )
    charges = charges_per_lot * Decimal(spec.lots)
    net_mfe = spec.mfe - charges
    session = session_bucket_for(spec.calculated_at, policy)
    allowed = tuple(sorted(allowed_families_for(market, policy)))
    tenor = None if decision.paper_tenor is None else decision.paper_tenor.value
    return BreakoutRow(
        name=spec.name,
        bucket=spec.bucket,
        regime={
            "trend": spec.trend.value,
            "volatility": spec.volatility.value,
            "iv_percentile": str(spec.iv_percentile),
            "iv_rv_ratio": str(spec.iv_rv_ratio),
            "event_state": spec.event_state,
            "macro_status": spec.macro_status.value,
            "session": session.value,
            "dte": str(spec.dte),
            "quality": spec.quality.value,
        },
        paper_winner=decision.paper_winner,
        paper_tenor=tenor,
        forced_choice=decision.forced_choice,
        failed_gates=decision.failed_gate_ids,
        binder_eligible=binder_eligible,
        entered=entered,
        judgment_label="should_enter" if should_enter else "should_pass",
        verdict=verdict_for(entered=entered, should_enter=should_enter),
        mae=str(spec.mae),
        mfe=str(spec.mfe),
        net_mfe=str(net_mfe),
        charges=str(charges),
        winner_score=(
            None if decision.winner_score is None else str(decision.winner_score)
        ),
        allowed_families=allowed,
    )


def _market(spec: _Spec, policy: IdentificationPolicy) -> MarketState:
    return MarketState(
        market_state_id=f"breakout-{spec.name}",
        feature_version=policy.feature_version,
        calculated_at=spec.calculated_at,
        source_snapshot_ids=(f"snap-{spec.name}",),
        trend=spec.trend,
        volatility=spec.volatility,
        return_15m=Decimal("0.004") if spec.mfe > spec.mae else Decimal("0.0005"),
        return_60m=Decimal("0.011") if spec.mfe > spec.mae else Decimal("0.001"),
        realized_volatility_ratio=spec.realized_volatility_ratio,
        iv_percentile=spec.iv_percentile,
        iv_rv_ratio=spec.iv_rv_ratio,
        trend_score=spec.trend_score,
        event_state=spec.event_state,
        macro_status=spec.macro_status,
        quality=DataQuality.VALID,
        warmup_complete=True,
        completed_bar_count=80,
        session_count=22,
    )


def _chain(
    *,
    as_of: datetime,
    option_types: tuple[OptionType, ...],
    dte: int,
    expiry: date,
    quality: ChainQuality,
    volume: int,
    implied_volatility: Decimal,
) -> tuple[FeatureSnapshot, ...]:
    legs: list[FeatureSnapshot] = []
    for option_type in option_types:
        legs.extend(
            _side(
                as_of=as_of,
                option_type=option_type,
                dte=dte,
                expiry=expiry,
                quality=quality,
                volume=volume,
                implied_volatility=implied_volatility,
            )
        )
    return tuple(legs)


def _side(
    *,
    as_of: datetime,
    option_type: OptionType,
    dte: int,
    expiry: date,
    quality: ChainQuality,
    volume: int,
    implied_volatility: Decimal,
) -> tuple[FeatureSnapshot, ...]:
    atm, short, wing, pin = _strikes(option_type)
    rows: list[tuple[str, str, str, str, int, int]]
    if quality is ChainQuality.DELTA_MISS:
        rows = [
            (atm, "0.38", "70", "72", 18000, volume),
            (short, "0.22", "40", "42", 14000, max(volume // 2, 1)),
            (pin, "0.12", "18", "19", 60000, 12000),
        ]
    else:
        rows = [
            (wing, "0.62", "148", "150", 18000, max(volume // 2, 1)),
            (atm, "0.525", "99", "100", 25000, volume),
            (short, "0.28", "66", "67", 16000, max(volume // 2, 1)),
            (pin, "0.12", "18", "19", 60000, 12000),
        ]
    oi_scale = 1
    wide = False
    if quality is ChainQuality.THIN_OI:
        oi_scale = 0
    if quality is ChainQuality.WIDE:
        wide = True
    out: list[FeatureSnapshot] = []
    for strike, delta, bid, ask, oi, vol in rows:
        use_oi = 400 if oi_scale == 0 else oi
        use_bid, use_ask = (
            (Decimal("90"), Decimal("115")) if wide else (Decimal(bid), Decimal(ask))
        )
        suffix = "CE" if option_type is OptionType.CALL else "PE"
        month = {9: "SEP", 10: "OCT"}[expiry.month]
        out.append(
            _option(
                symbol=f"NIFTY{expiry.day:02d}{month}{strike}{suffix}",
                strike=strike,
                delta=delta if option_type is OptionType.CALL else f"-{delta}",
                bid=use_bid,
                ask=use_ask,
                option_type=option_type,
                oi=use_oi,
                volume=vol,
                dte=dte,
                expiry=expiry,
                as_of=as_of,
                implied_volatility=implied_volatility,
            )
        )
    return tuple(out)


def _strikes(option_type: OptionType) -> tuple[str, str, str, str]:
    if option_type is OptionType.CALL:
        return "24000", "24100", "23900", "24300"
    return "24000", "23900", "24100", "23700"


def _option(
    *,
    symbol: str,
    strike: str,
    delta: str,
    bid: Decimal,
    ask: Decimal,
    option_type: OptionType,
    oi: int,
    volume: int,
    dte: int,
    expiry: date,
    as_of: datetime,
    implied_volatility: Decimal,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_id=f"snap-{symbol}",
        contract=ContractRef(
            exchange=Exchange.NFO,
            symbol=symbol,
            instrument_kind=InstrumentKind.OPTION,
            asset_class=AssetClass.EQUITY_INDEX,
            underlying="NIFTY",
            expiry=expiry,
            strike=Decimal(strike),
            option_type=option_type,
        ),
        times=SnapshotTimes(
            event_time=as_of,
            source_time=as_of,
            receive_time=as_of + timedelta(milliseconds=40),
            calculation_time=as_of + timedelta(milliseconds=90),
        ),
        market=MarketQuote(
            bid=Price(bid, _TICK),
            ask=Price(ask, _TICK),
            last=Price(bid, _TICK),
            volume=volume,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=oi,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=implied_volatility,
                delta=Decimal(delta),
            ),
            underlying_price=Price(Decimal("24000"), _TICK),
        ),
        feature_set_version="nifty-breakout-sim-v1",
        features={"lot_size": _LOT, "top_of_book_observed": Decimal(1)},
        quality=DataQualityReport(
            state=DataQuality.VALID,
            age_ms=90,
            warmup_complete=True,
            source_status="connected",
        ),
        lineage=_LINEAGE,
    )


# Path outcomes in INR for 1 Nifty lot. Charges are ₹100/lot from evaluation.yaml.
_WIN_MAE = Decimal("900")
_WIN_MFE = Decimal("5200")
_HOLD_MAE = Decimal("1800")
_HOLD_MFE = Decimal("14000")
_FADE_MAE = Decimal("4800")
_FADE_MFE = Decimal("700")
_THIN_MAE = Decimal("40")
_THIN_MFE = Decimal("80")
_CRUSH_MAE = Decimal("3600")
_CRUSH_MFE = Decimal("250")
_MISS_MAE = Decimal("1100")
_MISS_MFE = Decimal("8600")


def _cas(
    name: str,
    *,
    trend: TrendState,
    when: datetime,
    sides: tuple[OptionType, ...],
    quality: ChainQuality,
    mae: Decimal,
    mfe: Decimal,
    volatility: VolatilityState = VolatilityState.EXPANDING,
    iv: str = "32",
    rv: str = "1.05",
    event: str = "NORMAL",
    macro: MacroStatus = MacroStatus.NEUTRAL,
    volume: int = 9000,
    iv_level: str = "13",
    trend_score: str = "0.74",
    realized: str = "1.35",
) -> _Spec:
    return _Spec(
        name=name,
        bucket=BreakoutBucket.CAS,
        trend=trend,
        volatility=volatility,
        iv_percentile=Decimal(iv),
        iv_rv_ratio=Decimal(rv),
        event_state=event,
        calculated_at=when,
        sides=sides,
        dte=8,
        quality=quality,
        mae=mae,
        mfe=mfe,
        macro_status=macro,
        trend_score=Decimal(trend_score),
        realized_volatility_ratio=Decimal(realized),
        volume=volume,
        implied_volatility=Decimal(iv_level),
    )


def _pos(
    name: str,
    *,
    trend: TrendState,
    quality: ChainQuality,
    mae: Decimal,
    mfe: Decimal,
    iv: str = "30",
    rv: str = "1.00",
    event: str = "NORMAL",
    volatility: VolatilityState = VolatilityState.EXPANDING,
    volume: int = 7000,
    iv_level: str = "12",
    realized: str = "1.30",
) -> _Spec:
    side = (OptionType.CALL,) if trend is TrendState.UP else (OptionType.PUT,)
    return _Spec(
        name=name,
        bucket=BreakoutBucket.POSITIONAL,
        trend=trend,
        volatility=volatility,
        iv_percentile=Decimal(iv),
        iv_rv_ratio=Decimal(rv),
        event_state=event,
        calculated_at=CONTINUOUS,
        sides=side,
        dte=28,
        quality=quality,
        mae=mae,
        mfe=mfe,
        trend_score=Decimal("0.68"),
        realized_volatility_ratio=Decimal(realized),
        volume=volume,
        implied_volatility=Decimal(iv_level),
    )


def _dir(
    name: str,
    *,
    trend: TrendState,
    quality: ChainQuality,
    mae: Decimal,
    mfe: Decimal,
    iv: str = "28",
    rv: str = "0.98",
    equalize: bool = False,
    volatility: VolatilityState = VolatilityState.EXPANDING,
    volume: int = 8500,
    iv_level: str = "13",
    realized: str = "1.40",
    trend_score: str = "0.80",
) -> _Spec:
    side = (OptionType.CALL,) if trend is TrendState.UP else (OptionType.PUT,)
    return _Spec(
        name=name,
        bucket=BreakoutBucket.DIRECTION,
        trend=trend,
        volatility=volatility,
        iv_percentile=Decimal(iv),
        iv_rv_ratio=Decimal(rv),
        event_state="NORMAL",
        calculated_at=CONTINUOUS,
        sides=side,
        dte=8,
        quality=quality,
        mae=mae,
        mfe=mfe,
        equalize_scores=equalize,
        trend_score=Decimal(trend_score),
        realized_volatility_ratio=Decimal(realized),
        volume=volume,
        implied_volatility=Decimal(iv_level),
    )


_SPECS: tuple[_Spec, ...] = (
    # CAS — auction microstructure. Compression→expansion, fades, and rejects.
    _cas(
        "cas_open_continuation_up",
        trend=TrendState.UP,
        when=OPEN_AUCTION,
        sides=(OptionType.CALL,),
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        volume=11000,
    ),
    _cas(
        "cas_close_continuation_down",
        trend=TrendState.DOWN,
        when=CLOSE_AUCTION,
        sides=(OptionType.PUT,),
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        iv="34",
        volume=10000,
    ),
    _cas(
        "cas_open_false_break_fade",
        trend=TrendState.UP,
        when=OPEN_AUCTION,
        sides=(OptionType.CALL,),
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        volatility=VolatilityState.NORMAL,
        realized="0.85",
        trend_score="0.51",
        volume=6000,
    ),
    _cas(
        "cas_close_false_break_up",
        trend=TrendState.UP,
        when=CLOSE_AUCTION,
        sides=(OptionType.CALL,),
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        volatility=VolatilityState.COMPRESSED,
        realized="0.72",
        trend_score="0.48",
        volume=5500,
    ),
    _cas(
        "cas_wide_auction_cross",
        trend=TrendState.UP,
        when=OPEN_AUCTION,
        sides=(OptionType.CALL,),
        quality=ChainQuality.WIDE,
        mae=_FADE_MAE,
        mfe=_CRUSH_MFE,
        volatility=VolatilityState.EXPANDING,
        volume=4000,
    ),
    _cas(
        "cas_thin_oi_reject",
        trend=TrendState.DOWN,
        when=CLOSE_AUCTION,
        sides=(OptionType.PUT,),
        quality=ChainQuality.THIN_OI,
        mae=_FADE_MAE,
        mfe=_THIN_MFE,
        volume=1200,
    ),
    _cas(
        "cas_iv_crush_after_event",
        trend=TrendState.UP,
        when=OPEN_AUCTION,
        sides=(OptionType.CALL,),
        quality=ChainQuality.LIQUID,
        mae=_CRUSH_MAE,
        mfe=_CRUSH_MFE,
        iv="78",
        rv="1.45",
        event="CAUTION",
        iv_level="26",
        volume=9500,
    ),
    _cas(
        "cas_open_volume_expansion",
        trend=TrendState.UP,
        when=OPEN_AUCTION,
        sides=(OptionType.CALL,),
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        volume=16000,
        trend_score="0.81",
    ),
    _cas(
        "cas_macro_conflict_chop",
        trend=TrendState.MIXED,
        when=CLOSE_AUCTION,
        sides=(OptionType.CALL, OptionType.PUT),
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        macro=MacroStatus.CONFLICT,
        volatility=VolatilityState.NORMAL,
        iv="55",
        rv="1.22",
        iv_level="19",
        trend_score="0.12",
        realized="1.05",
    ),
    _cas(
        "cas_close_hold_above_level",
        trend=TrendState.DOWN,
        when=CLOSE_AUCTION,
        sides=(OptionType.PUT,),
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        iv="36",
        volume=8000,
        trend_score="0.70",
    ),
    # Positional — monthly tenor, hold for session+, debit when IV is rich.
    _pos(
        "pos_compression_break_call",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_HOLD_MAE,
        mfe=_HOLD_MFE,
        volatility=VolatilityState.COMPRESSED,
        realized="0.70",
        volume=6500,
    ),
    _pos(
        "pos_trend_day_put_hold",
        trend=TrendState.DOWN,
        quality=ChainQuality.LIQUID,
        mae=_HOLD_MAE,
        mfe=_HOLD_MFE,
        iv="29",
        volume=7200,
    ),
    _pos(
        "pos_false_break_multiday_fade",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        volatility=VolatilityState.NORMAL,
        realized="0.90",
        volume=5000,
    ),
    _pos(
        "pos_thin_mfe_after_charges",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_THIN_MAE,
        mfe=_THIN_MFE,
        volatility=VolatilityState.NORMAL,
        realized="1.05",
        volume=4800,
    ),
    _pos(
        "pos_high_iv_debit_call",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_HOLD_MAE,
        mfe=_HOLD_MFE,
        iv="74",
        rv="1.32",
        iv_level="24",
        volume=7800,
    ),
    _pos(
        "pos_mid_iv_debit_put",
        trend=TrendState.DOWN,
        quality=ChainQuality.LIQUID,
        mae=_HOLD_MAE,
        mfe=_HOLD_MFE,
        iv="52",
        rv="1.20",
        iv_level="18",
        volume=6900,
    ),
    _pos(
        "pos_wide_spread_monthly",
        trend=TrendState.UP,
        quality=ChainQuality.WIDE,
        mae=_FADE_MAE,
        mfe=_THIN_MFE,
        volume=3000,
    ),
    _pos(
        "pos_caution_event_holds",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_HOLD_MAE,
        mfe=_HOLD_MFE,
        event="CAUTION",
        iv="31",
        volume=7100,
    ),
    _pos(
        "pos_chop_after_break",
        trend=TrendState.DOWN,
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        volatility=VolatilityState.NORMAL,
        realized="0.88",
        volume=5200,
    ),
    _pos(
        "pos_block_new_event_chop",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        event="BLOCK_NEW",
        volume=6400,
    ),
    # Direction — weekly UP/DOWN, false breaks both ways, one fail-fast tie.
    _dir(
        "dir_up_low_iv_long_call",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
    ),
    _dir(
        "dir_down_low_iv_long_put",
        trend=TrendState.DOWN,
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        iv="27",
    ),
    _dir(
        "dir_up_false_break",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        volatility=VolatilityState.NORMAL,
        realized="0.78",
        trend_score="0.55",
        volume=5000,
    ),
    _dir(
        "dir_down_false_break",
        trend=TrendState.DOWN,
        quality=ChainQuality.LIQUID,
        mae=_FADE_MAE,
        mfe=_FADE_MFE,
        volatility=VolatilityState.COMPRESSED,
        realized="0.74",
        trend_score="0.52",
        volume=4700,
    ),
    _dir(
        "dir_mid_iv_debit_call",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        iv="54",
        rv="1.22",
        iv_level="17",
    ),
    _dir(
        "dir_high_iv_debit_put",
        trend=TrendState.DOWN,
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        iv="76",
        rv="1.38",
        iv_level="25",
    ),
    _dir(
        "dir_score_tie_fail_fast",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        equalize=True,
        iv="30",
        rv="1.00",
        trend_score="0.66",
    ),
    _dir(
        "dir_thin_liquidity_weekly",
        trend=TrendState.DOWN,
        quality=ChainQuality.THIN_OI,
        mae=_FADE_MAE,
        mfe=_THIN_MFE,
        volume=900,
    ),
    _dir(
        "dir_trend_followthrough",
        trend=TrendState.UP,
        quality=ChainQuality.LIQUID,
        mae=_WIN_MAE,
        mfe=_WIN_MFE,
        volume=14000,
        trend_score="0.86",
        realized="1.55",
    ),
    _dir(
        "dir_missed_delta_outside_band",
        trend=TrendState.UP,
        quality=ChainQuality.DELTA_MISS,
        mae=_MISS_MAE,
        mfe=_MISS_MFE,
        volume=13000,
        trend_score="0.84",
    ),
)
