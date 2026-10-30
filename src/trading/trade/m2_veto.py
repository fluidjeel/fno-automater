"""Five-minute M2 entry veto. The entry path reads a saved verdict."""

from __future__ import annotations

import importlib
import os
from datetime import datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from trading.domain.contracts.base import ExactDecimal, NonEmptyStr, StrictModel

__all__ = [
    "M2VetoAction",
    "M2VetoAdmission",
    "M2VetoConfig",
    "M2VetoVerdict",
    "admit_m2_entry",
    "bar_move_state",
    "in_session",
    "load_m2_veto_config",
    "lots_after_veto",
    "parse_model_verdict",
    "read_verdict",
    "write_verdict",
]

_IST = ZoneInfo("Asia/Kolkata")


class M2VetoAction(StrEnum):
    """Closed verdict vocabulary written by the five-minute check."""

    APPROVE = "APPROVE"
    VETO = "VETO"
    SIZE_DOWN = "SIZE_DOWN"


class M2VetoConfig(StrictModel):
    """Validated settings for the veto file and the session window."""

    schema_version: NonEmptyStr
    enabled: bool
    valid_for_seconds: int = Field(ge=60, le=900)
    size_down_fraction: ExactDecimal = Field(gt=Decimal("0"), lt=Decimal("1"))
    verdict_path: NonEmptyStr
    log_path: NonEmptyStr
    session_open_ist: NonEmptyStr
    session_close_ist: NonEmptyStr
    nifty_symbol: NonEmptyStr
    vix_symbol: NonEmptyStr
    bar_resolution: NonEmptyStr
    completed_bars: int = Field(ge=3, le=50)
    quote_max_age_seconds: int = Field(ge=1, le=300)
    model_id: NonEmptyStr = "deepseek-flash"
    thinking: Literal["disabled"] = "disabled"

    @field_validator("session_open_ist", "session_close_ist")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        time.fromisoformat(value)
        return value


class M2VetoVerdict(StrictModel):
    """One saved verdict. `valid_until` is the entry-path freshness clock."""

    schema_version: NonEmptyStr = "1"
    action: M2VetoAction
    size_fraction: ExactDecimal = Field(ge=Decimal("0"), le=Decimal("1"))
    reasons: tuple[NonEmptyStr, ...] = Field(min_length=1)
    as_of: datetime
    valid_until: datetime
    source: NonEmptyStr
    direction: NonEmptyStr | None = None
    m2_agentic_id: NonEmptyStr | None = None
    decision_id: NonEmptyStr | None = None

    @field_validator("as_of", "valid_until")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("verdict timestamps must be timezone-aware")
        return value


class M2VetoAdmission(StrictModel):
    """What the entry path is allowed to do with a new M2 trade."""

    blocks_new_entry: bool
    size_fraction: ExactDecimal
    detail: NonEmptyStr


def load_m2_veto_config(path: Any | None = None) -> M2VetoConfig:
    """Load the veto config from the repo `config/m2_veto.yaml`."""
    path_cls = importlib.import_module("pathlib").Path
    yaml_mod = importlib.import_module("yaml")
    if path is None:
        repo = path_cls(__file__).resolve().parents[3] / "config" / "m2_veto.yaml"
        target = repo if repo.is_file() else path_cls("config/m2_veto.yaml")
    else:
        target = path_cls(path)
    raw: Any = yaml_mod.safe_load(target.read_text(encoding="utf-8"))
    return M2VetoConfig.model_validate(raw)


def in_session(now: datetime, config: M2VetoConfig) -> bool:
    """Return whether `now` falls inside the configured exchange-local window."""
    local = now.astimezone(_IST).time()
    return (
        time.fromisoformat(config.session_open_ist)
        <= local
        <= time.fromisoformat(config.session_close_ist)
    )


def admit_m2_entry(
    now: datetime, *, config: M2VetoConfig | None = None
) -> M2VetoAdmission:
    """Decide a new M2 entry from the saved verdict. Missing or stale blocks."""
    loaded = config or load_m2_veto_config()
    if not loaded.enabled:
        return M2VetoAdmission(
            blocks_new_entry=False,
            size_fraction=Decimal("1"),
            detail="m2 veto disabled",
        )
    verdict = read_verdict(loaded)
    if verdict is None:
        return M2VetoAdmission(
            blocks_new_entry=True,
            size_fraction=Decimal("0"),
            detail="no five-minute veto verdict",
        )
    if now > verdict.valid_until:
        return M2VetoAdmission(
            blocks_new_entry=True,
            size_fraction=Decimal("0"),
            detail="five-minute veto verdict expired",
        )
    if verdict.action is M2VetoAction.VETO:
        return M2VetoAdmission(
            blocks_new_entry=True,
            size_fraction=Decimal("0"),
            detail=verdict.reasons[0],
        )
    if verdict.action is M2VetoAction.SIZE_DOWN:
        return M2VetoAdmission(
            blocks_new_entry=False,
            size_fraction=verdict.size_fraction,
            detail=verdict.reasons[0],
        )
    return M2VetoAdmission(
        blocks_new_entry=False,
        size_fraction=Decimal("1"),
        detail=verdict.reasons[0],
    )


def lots_after_veto(approved_lots: int, admission: M2VetoAdmission) -> int:
    """Apply a size-down fraction. A fraction of one lot is zero lots."""
    if admission.blocks_new_entry or approved_lots <= 0:
        return 0
    scaled = (Decimal(approved_lots) * admission.size_fraction).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return int(scaled)


def bar_move_state(
    *,
    quote: Decimal,
    bar_open: Decimal,
    bar_high: Decimal,
    bar_low: Decimal,
    bar_close: Decimal,
) -> str:
    """Label whether the latest quote still extends the last completed 5-minute bar."""
    direction = bar_close - bar_open
    span = bar_high - bar_low
    if span <= 0 or direction == 0:
        return "flat"
    midpoint = bar_low + (span / Decimal("2"))
    extended = quote <= bar_close if direction < 0 else quote >= bar_close
    retraced = quote >= midpoint if direction < 0 else quote <= midpoint
    if extended:
        return "developing"
    if retraced:
        return "exhausted"
    return "stalling"


def parse_model_verdict(
    payload: dict[str, Any],
    *,
    now: datetime,
    config: M2VetoConfig,
) -> M2VetoVerdict:
    """Validate model JSON. The size fraction comes from config."""
    action = M2VetoAction(str(payload.get("action") or ""))
    raw_reasons = payload.get("reasons")
    if not isinstance(raw_reasons, list) or not raw_reasons:
        raise ValueError("verdict reasons must be a non-empty list")
    reasons = tuple(
        str(item).strip()[:200] for item in raw_reasons if str(item).strip()
    )
    if not reasons:
        raise ValueError("verdict reasons were empty")
    fraction = Decimal("0") if action is M2VetoAction.VETO else Decimal("1")
    if action is M2VetoAction.SIZE_DOWN:
        fraction = config.size_down_fraction
    return M2VetoVerdict(
        action=action,
        size_fraction=fraction,
        reasons=reasons,
        as_of=now,
        valid_until=now + timedelta(seconds=config.valid_for_seconds),
        source="m2-veto-5m",
    )


def _default_repo_root() -> Any:
    env = os.environ.get("TRADING_REPO_ROOT")
    if env:
        return importlib.import_module("pathlib").Path(env).resolve()
    return importlib.import_module("pathlib").Path(__file__).resolve().parents[3]


def verdict_file(config: M2VetoConfig, *, repo_root: Any | None = None) -> Any:
    """Resolve the verdict path under the repo root."""
    path_cls = importlib.import_module("pathlib").Path
    root = path_cls(repo_root) if repo_root is not None else _default_repo_root()
    return root / config.verdict_path


def read_verdict(config: M2VetoConfig, *, repo_root: Any | None = None) -> M2VetoVerdict | None:
    """Return the saved verdict, or None when the file is missing or unreadable."""
    path = verdict_file(config, repo_root=repo_root)
    if not path.is_file():
        return None
    json_mod = importlib.import_module("json")
    try:
        raw = json_mod.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return M2VetoVerdict.model_validate(raw)
    except ValueError:
        return None


def write_verdict(
    verdict: M2VetoVerdict,
    config: M2VetoConfig,
    *,
    repo_root: Any | None = None,
) -> None:
    """Atomically replace the verdict file and append one log line."""
    path_cls = importlib.import_module("pathlib").Path
    json_mod = importlib.import_module("json")
    root = path_cls(repo_root) if repo_root is not None else _default_repo_root()
    path = root / config.verdict_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = verdict.model_dump(mode="json")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json_mod.dumps(payload), encoding="utf-8")
    temporary.replace(path)
    log_path = root / config.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json_mod.dumps(payload) + "\n")
