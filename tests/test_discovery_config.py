"""Tests for DISC-A0: Profile switch, discovery config, LIVE guard, and heartbeat."""

from __future__ import annotations

import json
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trading.config.discovery import (
    DiscoveryConfig,
    DiscoveryConfigError,
    load_discovery_config,
)
from trading.config.schema import Environment
from trading.domain.enums import EntryProfile, ExecutionMode, ModeId
from trading.domain.primitives import Currency, Money
from trading.runtime.paper_session import (
    PaperSession,
    PaperSessionConfig,
)
from trading.runtime.startup_validation import (
    StartupValidationError,
    validate_startup_configuration,
)

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.fromisoformat("2026-09-22T05:00:00+00:00")
IST = ZoneInfo("Asia/Kolkata")


def _sample_session_config(**overrides: object) -> PaperSessionConfig:
    payload: dict[str, object] = {
        "poll_interval_seconds": 60,
        "eod_local": "15:40",
        "option_strikes_each_side": 2,
        "experiment_prefix": "EXP-TEST",
        "strategy_ids": ("positional_long_option",),
        "strategy_stances": {"positional_long_option": ExecutionMode.PAPER},
        "commodity_underlying": "CRUDEOIL",
        "commodity_exchange": "MCX",
        "commodity_segment": "MCX_COM",
        "cohort_dir": "data/paper/cohorts",
        "store_path": "data/paper/trading.sqlite",
        "broker_state_path": "data/paper/broker_state.json",
    }
    payload.update(overrides)
    return PaperSessionConfig.model_validate(payload)


class _DummyRealBroker:
    __module__ = "trading.broker.fyers"


class TestDiscoveryConfig:
    def test_load_canonical_discovery_config_succeeds(self) -> None:
        loaded = load_discovery_config(ROOT / "config" / "discovery.yaml")
        cfg = loaded.config
        assert cfg.profile_version == "discovery-v1"
        assert loaded.checksum
        assert cfg.books.starting_equity_per_mode == Decimal("700000")
        assert cfg.modes[ModeId.M1_CAS.value].per_trade_guide == Decimal("0.01")
        assert cfg.modes[ModeId.M2_DIRECTIONAL.value].per_trade_guide == Decimal("0.02")
        assert cfg.modes[
            ModeId.M3_TACTICAL_POSITIONAL.value
        ].per_trade_guide == Decimal("0.02")
        assert cfg.modes[
            ModeId.M4_STRATEGIC_POSITIONAL.value
        ].per_trade_guide == Decimal("0.03")
        assert cfg.bug_guard_trade_risk_fraction == Decimal("0.10")
        assert cfg.strict_quote_max_age_ms == 120000
        assert cfg.cas_strict_quote_max_age_ms == 30000
        assert cfg.hard_quote_max_age_ms == 300000

    def test_discovery_config_fraction_greater_than_one_fails(
        self, tmp_path: Path
    ) -> None:
        yaml_content = (ROOT / "config" / "discovery.yaml").read_text(encoding="utf-8")
        bad_yaml = yaml_content.replace(
            'bug_guard_trade_risk_fraction: "0.10"',
            'bug_guard_trade_risk_fraction: "1.50"',
        )
        bad_path = tmp_path / "bad_discovery.yaml"
        bad_path.write_text(bad_yaml, encoding="utf-8")
        with pytest.raises(DiscoveryConfigError):
            load_discovery_config(bad_path)

    def test_discovery_config_missing_mode_fails(self, tmp_path: Path) -> None:
        yaml_content = (ROOT / "config" / "discovery.yaml").read_text(encoding="utf-8")
        bad_yaml = yaml_content.replace("M4_STRATEGIC_POSITIONAL:", "IGNORED_MODE:")
        bad_path = tmp_path / "missing_mode.yaml"
        bad_path.write_text(bad_yaml, encoding="utf-8")
        with pytest.raises(DiscoveryConfigError):
            load_discovery_config(bad_path)


class TestStartupValidationDiscovery:
    def test_startup_validation_discovery_paper_passes(self) -> None:
        cfg = _sample_session_config(entry_profile=EntryProfile.DISCOVERY)
        validated, warnings = validate_startup_configuration(
            cfg,
            environment=Environment.PAPER,
            discovery_path=ROOT / "config" / "discovery.yaml",
        )
        assert validated.entry_profile is EntryProfile.DISCOVERY
        assert warnings == []

    def test_startup_validation_discovery_live_env_fails(self) -> None:
        cfg = _sample_session_config(entry_profile=EntryProfile.DISCOVERY)
        with pytest.raises(StartupValidationError, match=r"Environment.LIVE"):
            validate_startup_configuration(
                cfg,
                environment=Environment.LIVE,
                discovery_path=ROOT / "config" / "discovery.yaml",
            )

    def test_startup_validation_discovery_real_broker_fails(self) -> None:
        cfg = _sample_session_config(entry_profile=EntryProfile.DISCOVERY)
        with pytest.raises(StartupValidationError, match=r"real broker adapter"):
            validate_startup_configuration(
                cfg,
                environment=Environment.PAPER,
                broker=_DummyRealBroker(),
                discovery_path=ROOT / "config" / "discovery.yaml",
            )

    def test_startup_validation_discovery_malformed_yaml_fails(
        self, tmp_path: Path
    ) -> None:
        bad_path = tmp_path / "discovery.yaml"
        bad_path.write_text(
            "schema_version: '1'\nprofile_version: 'bad'\n", encoding="utf-8"
        )
        cfg = _sample_session_config(entry_profile=EntryProfile.DISCOVERY)
        with pytest.raises(StartupValidationError, match=r"[Mm]alformed|[Ii]nvalid"):
            validate_startup_configuration(
                cfg,
                environment=Environment.PAPER,
                discovery_path=bad_path,
            )

    def test_startup_validation_strict_default_when_absent(self) -> None:
        cfg = _sample_session_config()
        assert cfg.entry_profile is EntryProfile.STRICT
        validated, warnings = validate_startup_configuration(cfg)
        assert validated.entry_profile is EntryProfile.STRICT
        assert warnings == []


class _MockRecovery:
    alerts: tuple[object, ...] = ()


class _MockTradeManager:
    def list_positions(self) -> list[object]:
        return []


class _MockRunner:
    def __init__(self) -> None:
        self.trade_manager = _MockTradeManager()

    def open_position_count(self) -> int:
        return 0

    def recover_lifecycle(self) -> _MockRecovery:
        return _MockRecovery()


class _MockSink:
    def send(self, text: str) -> bool:
        return True


def _make_test_session(
    cfg: PaperSessionConfig,
    tmp_path: Path,
    *,
    discovery_config: DiscoveryConfig | None = None,
) -> PaperSession:
    runner = _MockRunner()
    session = PaperSession(
        runner=runner,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        session_config=cfg,
        session_hours=(time(9, 15), time(15, 30)),
        timezone=IST,
        notifier=_MockSink(),
        request_builder=lambda _now: ((), {}),
        observation_start=NOW,
        capital_limit=Money.of("700000", Currency.INR),
        risk_policy_version="1",
        fill_model_version="touch-v1",
        code_version="1",
        charges_verified=True,
        cohort_dir=tmp_path / "cohorts",
        session_heartbeat_path=Path(cfg.session_heartbeat_path),
        discovery_config=discovery_config,
    )
    return session


class TestHeartbeatAndBanner:
    def test_paper_session_heartbeat_has_entry_profile_discovery(
        self, tmp_path: Path
    ) -> None:
        hb_path = tmp_path / "session_heartbeat.json"
        cfg = _sample_session_config(
            entry_profile=EntryProfile.DISCOVERY,
            session_heartbeat_path=str(hb_path),
        )
        session = _make_test_session(cfg, tmp_path)
        session._write_session_heartbeat(now=NOW, result=None, had_requests=False)
        payload = json.loads(hb_path.read_text(encoding="utf-8"))
        assert payload["entry_profile"] == "DISCOVERY"
        assert payload["profile_version"] == "discovery-v1"

    def test_paper_session_heartbeat_has_entry_profile_strict_when_absent(
        self, tmp_path: Path
    ) -> None:
        hb_path = tmp_path / "session_heartbeat.json"
        cfg = _sample_session_config(
            session_heartbeat_path=str(hb_path),
        )
        session = _make_test_session(cfg, tmp_path)
        session._write_session_heartbeat(now=NOW, result=None, had_requests=False)
        payload = json.loads(hb_path.read_text(encoding="utf-8"))
        assert payload["entry_profile"] == "STRICT"
        assert payload["profile_version"] == "strict"

    def test_startup_banner_prints_discovery_temporary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _sample_session_config(
            entry_profile=EntryProfile.DISCOVERY,
            session_heartbeat_path=str(tmp_path / "hb.json"),
        )
        session = _make_test_session(cfg, tmp_path)

        def _fake_run_loop(once: bool = False) -> int:
            return 0

        session._run_loop = _fake_run_loop  # type: ignore[method-assign]
        session.run(once=True)
        captured = capsys.readouterr()
        assert "ENTRY PROFILE: DISCOVERY (temporary)" in captured.out

    def test_startup_banner_prints_strict_when_absent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _sample_session_config(
            session_heartbeat_path=str(tmp_path / "hb.json"),
        )
        session = _make_test_session(cfg, tmp_path)

        def _fake_run_loop(once: bool = False) -> int:
            return 0

        session._run_loop = _fake_run_loop  # type: ignore[method-assign]
        session.run(once=True)
        captured = capsys.readouterr()
        assert "ENTRY PROFILE: STRICT" in captured.out
