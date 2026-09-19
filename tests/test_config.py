"""Configuration boundaries.

Invariant 10: storage uncertainty or clock drift blocks time-sensitive entries.
Invariant 19: critical values carry validity and lineage.
Invariant 21: config version and checksum make a decision reproducible.
Invariant 23: a live config change carries a version and checksum.
BROKER_SPEC.md: never hard-code a remembered exchange threshold.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from trading.config import (
    AppConfig,
    ConfigLoadError,
    ConfigNotVerifiedError,
    Environment,
    RiskLimits,
    StorageRules,
    VerifiedValue,
    load_agent_config,
    load_config,
    load_config_text,
    load_evaluation_config,
)
from trading.config.risk_policy import load_risk_policy_text
from trading.domain.enums import Exchange, ReasonCode

BASE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "base.yaml"
RISK_CONFIG = Path(__file__).resolve().parent.parent / "config" / "risk.yaml"
EVALUATION_CONFIG = (
    Path(__file__).resolve().parent.parent / "config" / "evaluation.yaml"
)


def base_text() -> str:
    return BASE_CONFIG.read_text(encoding="utf-8")


def base_payload() -> dict[str, object]:
    payload = yaml.safe_load(base_text())
    assert isinstance(payload, dict)
    return payload


class TestShippedConfiguration:
    def test_base_config_loads(self) -> None:
        loaded = load_config(BASE_CONFIG)
        assert loaded.config.environment is Environment.BACKTEST

    def test_every_exchange_controlled_value_ships_unverified(self) -> None:
        """A shipped default for an exchange rule is a latent wrong-size order."""
        pending = load_config(BASE_CONFIG).config.unverified_paths()
        assert pending, "the base config must not ship verified market rules"
        assert any("max_orders_per_second" in path for path in pending)
        assert any("min_cash_fraction_of_margin" in path for path in pending)

    def test_base_config_is_not_live_ready(self) -> None:
        config = load_config(BASE_CONFIG).config
        with pytest.raises(ConfigNotVerifiedError):
            config.require_ready_for(Environment.LIVE)

    def test_evaluation_policy_loads_with_unverified_charges(self) -> None:
        loaded = load_evaluation_config(EVALUATION_CONFIG)
        assert loaded.config.fill_model.version == "conservative-v1"
        with pytest.raises(ConfigNotVerifiedError):
            loaded.config.fill_model.charges_per_lot.require(
                "fill_model.charges_per_lot"
            )

    def test_agent_policy_ships_disabled(self) -> None:
        loaded = load_agent_config(
            Path(__file__).resolve().parent.parent / "config" / "agent.yaml"
        )
        assert loaded.config.enabled is False
        assert loaded.config.max_iterations >= 1

    def test_no_market_rule_literal_appears_in_domain_code(self) -> None:
        """The stale figures from docs/research must not have leaked into code."""
        src = Path(__file__).resolve().parent.parent / "src" / "trading" / "domain"
        stale = ("lot_size = 75", "0.50", "min_cash_fraction", "elm")
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in stale:
                assert needle not in text, f"{path.name} contains {needle!r}"


class TestUnverifiedValuesFailClosed:
    def test_reading_an_unverified_value_raises(self) -> None:
        rule = VerifiedValue[int](value=None, source="exchange circular")
        with pytest.raises(ConfigNotVerifiedError, match="exchange circular"):
            rule.require("exchanges[NSE].max_orders_per_second")

    def test_the_error_names_the_path_and_the_authority(self) -> None:
        rule = VerifiedValue[int](value=None, source="broker docs")
        with pytest.raises(ConfigNotVerifiedError) as caught:
            rule.require("instruments[NFO/NIFTY].lot_size")
        assert caught.value.path == "instruments[NFO/NIFTY].lot_size"
        assert caught.value.source == "broker docs"

    def test_the_error_carries_a_machine_readable_reason(self) -> None:
        assert ConfigNotVerifiedError.reason_code is ReasonCode.CONFIG_UNVERIFIED

    def test_a_verified_value_reads_normally(self) -> None:
        rule = VerifiedValue[int](
            value=75, source="broker instrument master", verified_at=date(2026, 9, 13)
        )
        assert rule.require("instruments[NFO/NIFTY].lot_size") == 75
        assert rule.is_verified

    def test_a_value_without_a_verification_date_is_rejected(self) -> None:
        """An unattributed number is indistinguishable from a guess."""
        with pytest.raises(ValueError, match="must record verified_at"):
            VerifiedValue[int](value=75, source="somewhere")

    def test_a_verification_date_without_a_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="meaningless"):
            VerifiedValue[int](source="somewhere", verified_at=date(2026, 9, 13))

    def test_an_unconfigured_timeframe_fails_closed(self) -> None:
        freshness = load_config(BASE_CONFIG).config.freshness
        with pytest.raises(ConfigNotVerifiedError, match="max_age_ms_by_timeframe"):
            freshness.max_age_ms("5m")

    def test_an_unknown_instrument_fails_closed(self) -> None:
        config = load_config(BASE_CONFIG).config
        with pytest.raises(ConfigNotVerifiedError, match="instrument master"):
            config.instrument_rule(Exchange.NFO, "NIFTY")

    def test_an_unconfigured_exchange_fails_closed(self) -> None:
        config = load_config(BASE_CONFIG).config
        with pytest.raises(ConfigNotVerifiedError, match="MCX"):
            config.exchange_rules(Exchange.MCX)

    def test_a_configured_exchange_resolves(self) -> None:
        config = load_config(BASE_CONFIG).config
        assert config.exchange_rules(Exchange.NSE).zone.key == "Asia/Kolkata"


class TestLoaderRejectsBadInput:
    def test_strategy_allocations_cannot_exceed_equity(self) -> None:
        raw = RISK_CONFIG.read_text(encoding="utf-8")
        payload = yaml.safe_load(raw)
        payload["strategy_allocations"]["overflow"] = {"allocation_fraction": "0.50"}
        with pytest.raises(ValueError, match="cannot exceed account equity"):
            load_risk_policy_text(yaml.safe_dump(payload))

    def test_unknown_key_is_rejected(self) -> None:
        payload = base_payload()
        payload["unexpected_section"] = {}
        with pytest.raises(ValueError, match="unexpected_section"):
            load_config_text(yaml.safe_dump(payload))

    def test_missing_required_section_is_rejected(self) -> None:
        payload = base_payload()
        del payload["risk"]
        with pytest.raises(ValueError, match="risk"):
            load_config_text(yaml.safe_dump(payload))

    def test_malformed_yaml_is_rejected(self) -> None:
        with pytest.raises(ConfigLoadError, match="not valid YAML"):
            load_config_text("environment: [unclosed")

    def test_non_mapping_top_level_is_rejected(self) -> None:
        with pytest.raises(ConfigLoadError, match="expected a mapping"):
            load_config_text("- just\n- a\n- list\n")

    def test_missing_file_is_rejected(self) -> None:
        with pytest.raises(ConfigLoadError, match="cannot read"):
            load_config(Path("/nonexistent/config.yaml"))

    def test_environment_mismatch_is_fatal(self) -> None:
        """Loading the wrong environment is how paper credentials reach live."""
        with pytest.raises(ConfigLoadError, match="was expected"):
            load_config(BASE_CONFIG, expect_environment=Environment.LIVE)

    def test_a_live_config_with_unverified_rules_will_not_load(self) -> None:
        payload = base_payload()
        payload["environment"] = "LIVE"
        with pytest.raises(ConfigNotVerifiedError):
            load_config_text(yaml.safe_dump(payload))

    def test_duplicate_exchange_is_rejected(self) -> None:
        payload = base_payload()
        exchanges = payload["exchanges"]
        assert isinstance(exchanges, list)
        payload["exchanges"] = [*exchanges, exchanges[0]]
        with pytest.raises(ValueError, match="only once"):
            load_config_text(yaml.safe_dump(payload))

    def test_at_least_one_exchange_is_required(self) -> None:
        payload = base_payload()
        payload["exchanges"] = []
        with pytest.raises(ValueError, match="at least one exchange"):
            load_config_text(yaml.safe_dump(payload))

    def test_unknown_timezone_is_rejected(self) -> None:
        payload = base_payload()
        exchanges = payload["exchanges"]
        assert isinstance(exchanges, list)
        exchanges[0]["timezone"] = "Mars/Olympus"
        with pytest.raises(ValueError, match="unknown timezone"):
            load_config_text(yaml.safe_dump(payload))


class TestChecksumAndLineage:
    def test_checksum_is_stable_across_loads(self) -> None:
        """Invariant 21: reproducible decisions need a stable config identity."""
        assert load_config(BASE_CONFIG).checksum == load_config(BASE_CONFIG).checksum

    def test_checksum_changes_when_any_byte_changes(self) -> None:
        original = load_config_text(base_text())
        edited = load_config_text(base_text() + "\n# a trailing comment\n")
        assert original.checksum != edited.checksum

    def test_lineage_pairs_version_with_checksum(self) -> None:
        """Invariant 23: a config change carries both a version and a checksum."""
        loaded = load_config(BASE_CONFIG)
        assert loaded.lineage == (loaded.version, loaded.checksum)
        assert len(loaded.checksum) == 64

    def test_semantically_equal_but_differently_formatted_files_differ(self) -> None:
        """The checksum covers bytes, so a human edit is always attributable."""
        reformatted = yaml.safe_dump(base_payload())
        assert (
            load_config_text(reformatted).checksum != load_config(BASE_CONFIG).checksum
        )


class TestRiskLimitsNest:
    def test_per_trade_cap_above_the_daily_cap_is_rejected(self) -> None:
        """One trade must not be able to breach the day limit in a single go."""
        with pytest.raises(ValueError, match="exceeds the daily cap"):
            RiskLimits(
                max_loss_per_trade_fraction=Decimal("0.05"),
                daily_loss_cap_fraction=Decimal("0.03"),
                max_portfolio_risk_fraction=Decimal("0.10"),
                max_concurrent_trades=3,
                max_margin_utilisation_fraction=Decimal("0.5"),
            )

    def test_daily_cap_above_the_portfolio_budget_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds total portfolio risk"):
            RiskLimits(
                max_loss_per_trade_fraction=Decimal("0.01"),
                daily_loss_cap_fraction=Decimal("0.15"),
                max_portfolio_risk_fraction=Decimal("0.10"),
                max_concurrent_trades=3,
                max_margin_utilisation_fraction=Decimal("0.5"),
            )

    def test_zero_concurrent_trades_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            RiskLimits(
                max_loss_per_trade_fraction=Decimal("0.01"),
                daily_loss_cap_fraction=Decimal("0.03"),
                max_portfolio_risk_fraction=Decimal("0.10"),
                max_concurrent_trades=0,
                max_margin_utilisation_fraction=Decimal("0.5"),
            )

    def test_float_limits_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="float is not permitted"):
            RiskLimits.model_validate(
                {
                    "max_loss_per_trade_fraction": 0.01,
                    "daily_loss_cap_fraction": "0.03",
                    "max_portfolio_risk_fraction": "0.10",
                    "max_concurrent_trades": 3,
                    "max_margin_utilisation_fraction": "0.5",
                }
            )


class TestStorageAndDrift:
    def test_audit_retention_must_outlive_raw_retention(self) -> None:
        with pytest.raises(ValueError, match="audit lineage must outlive"):
            StorageRules(raw_retention_days=365, audit_retention_days=30)

    def test_durable_write_before_submit_is_the_default(self) -> None:
        """Invariant 10: storage uncertainty blocks new exposure."""
        assert StorageRules(
            raw_retention_days=30, audit_retention_days=30
        ).durable_write_required_before_submit

    def test_clock_drift_threshold_must_be_positive(self) -> None:
        payload = base_payload()
        freshness = payload["freshness"]
        assert isinstance(freshness, dict)
        freshness["max_clock_drift_ms"] = 0
        with pytest.raises(ValueError):
            load_config_text(yaml.safe_dump(payload))


class TestConfigIsAContract:
    def test_config_is_frozen(self) -> None:
        assert AppConfig.model_config.get("frozen") is True

    def test_config_forbids_unknown_fields(self) -> None:
        assert AppConfig.model_config.get("extra") == "forbid"

    def test_config_round_trips_losslessly(self) -> None:
        config = load_config(BASE_CONFIG).config
        assert config.round_trip() == config
