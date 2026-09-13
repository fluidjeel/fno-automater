"""Layer 1 data pipeline: normalize, quality, store, replay.

Invariant 6: stale or invalid critical state blocks new exposure.
Invariant 19: canonical events carry lineage and raw references.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from trading.config import load_config
from trading.data.config import (
    QualityConfig,
    SessionConfig,
    UnderlyingConfig,
    load_data_pipeline_config,
)
from trading.data.events import RawMarketCapture
from trading.data.fyers.client import FyersApiError
from trading.data.fyers.ws import FyersTickStream
from trading.data.macro_news import MacroNewsItem, score_macro_news
from trading.data.normalize import (
    normalize_fyers_depth,
    normalize_fyers_history,
    normalize_fyers_instrument_reference,
    normalize_fyers_market_status,
    normalize_fyers_option_chain,
    normalize_fyers_quotes,
    normalize_fyers_ws_tick,
)
from trading.data.pipeline import DataPipeline
from trading.data.quality import (
    assess_combined_snapshot,
    assess_option_chain,
    assess_quote_snapshot,
)
from trading.data.storage.catalog import CatalogWriter
from trading.data.replay import ReplayEngine
from trading.data.settings import FyersSettings
from trading.data.storage.parquet_store import JsonlEventStore
from trading.domain.clock import FrozenClock
from trading.domain.enums import (
    AssetClass,
    DataQuality,
    Exchange,
    InstrumentKind,
    ReasonCode,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fyers_option_chain_nifty.json"
QUOTES_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "fyers_quotes_nifty.json"
)
HISTORY_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "fyers_history_nifty.json"
)
DEPTH_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fyers_depth_nifty.json"
STATUS_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "fyers_market_status.json"
)
REPO = Path(__file__).resolve().parent.parent
PIPELINE_CONFIG = REPO / "config" / "data_pipeline.yaml"
BASE_CONFIG = REPO / "config" / "base.yaml"


def _underlying() -> UnderlyingConfig:
    return UnderlyingConfig(
        symbol="NSE:NIFTY50-INDEX",
        feature_set_version="nifty_index_v1",
        exchange=Exchange.NSE,
        underlying="NIFTY",
        instrument_kind=InstrumentKind.INDEX,
        asset_class=AssetClass.EQUITY_INDEX,
    )


def _capture(now: datetime) -> RawMarketCapture:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data = payload.get("data")
    if isinstance(data, dict):
        data["timestamp"] = int(now.timestamp())
    return RawMarketCapture(
        capture_id="cap-test-1",
        provider="fyers",
        endpoint="/data/options-chain-v3",
        received_at=now,
        payload=payload,
        http_status=200,
    )


def _quotes_capture(now: datetime) -> RawMarketCapture:
    payload = json.loads(QUOTES_FIXTURE.read_text(encoding="utf-8"))
    return RawMarketCapture(
        capture_id="cap-quotes-1",
        provider="fyers",
        endpoint="quotes",
        received_at=now,
        payload=payload,
        http_status=200,
    )


def _depth_capture(now: datetime) -> RawMarketCapture:
    payload = json.loads(DEPTH_FIXTURE.read_text(encoding="utf-8"))
    return RawMarketCapture(
        capture_id="cap-depth-1",
        provider="fyers",
        endpoint="depth",
        received_at=now,
        payload=payload,
        http_status=200,
    )


def _status_capture(now: datetime, status: str = "OPEN") -> RawMarketCapture:
    payload = json.loads(STATUS_FIXTURE.read_text(encoding="utf-8"))
    rows = payload.get("marketStatus")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        rows[0]["status"] = status
    return RawMarketCapture(
        capture_id=f"cap-status-{status}",
        provider="fyers",
        endpoint="marketStatus",
        received_at=now,
        payload=payload,
        http_status=200,
    )


def _session(*, verified: bool = True) -> SessionConfig:
    return SessionConfig(
        timezone="Asia/Kolkata",
        open_local="09:15",
        close_local="15:30",
        verified=verified,
        segment="NSE_FO",
    )


def _history_capture(now: datetime, resolution: str = "5") -> RawMarketCapture:
    payload = json.loads(HISTORY_FIXTURE.read_text(encoding="utf-8"))
    candles = payload.get("candles")
    if isinstance(candles, list) and candles and isinstance(candles[0], list):
        candles[0][0] = int(now.timestamp())
    return RawMarketCapture(
        capture_id=f"cap-history-{resolution}",
        provider="fyers",
        endpoint="history",
        received_at=now,
        payload=payload,
        http_status=200,
    )


class FakeFeed:
    def __init__(
        self,
        chain_capture: RawMarketCapture,
        *,
        quotes_capture: RawMarketCapture | None = None,
        history_capture: RawMarketCapture | None = None,
    ) -> None:
        self._chain_capture = chain_capture
        received = chain_capture.received_at
        self._quotes_capture = quotes_capture or _quotes_capture(received)
        self._history_capture = history_capture or _history_capture(
            chain_capture.received_at
        )
        self._depth_capture = _depth_capture(received)
        self._status_capture = _status_capture(received)

    def fetch_option_chain(self, symbol: str) -> RawMarketCapture:
        assert symbol == "NSE:NIFTY50-INDEX"
        return self._chain_capture

    def fetch_quotes(self, symbols: tuple[str, ...]) -> RawMarketCapture:
        assert symbols == ("NSE:NIFTY50-INDEX",)
        return self._quotes_capture

    def fetch_history(
        self,
        symbol: str,
        *,
        resolution: str,
        range_from: str,
        range_to: str,
    ) -> RawMarketCapture:
        assert symbol == "NSE:NIFTY50-INDEX"
        return self._history_capture

    def fetch_depth(self, symbol: str) -> RawMarketCapture:
        assert symbol == "NSE:NIFTY50-INDEX"
        return self._depth_capture

    def fetch_market_status(self) -> RawMarketCapture:
        return self._status_capture

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture:
        raise FyersApiError("expiry endpoint unavailable")


class TestNormalizeAndQuality:
    def test_normalize_maps_fyers_chain_to_canonical_event(self) -> None:
        """Invariant 19: every event records raw ref and normalization version."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        capture = _capture(now)
        event = normalize_fyers_option_chain(
            capture,
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="data/raw/fyers/test.json",
        )
        assert event.event_type == "OPTION_CHAIN_SNAPSHOT"
        assert event.payload["strike_count"] == 2
        assert event.raw_ref == "data/raw/fyers/test.json"

    def test_normalize_maps_quotes_bars_and_reference(self) -> None:
        """Invariant 19: REST feeds normalize to typed canonical events."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        chain = _capture(now)
        quotes = _quotes_capture(now)
        history = _history_capture(now)
        quote_event = normalize_fyers_quotes(
            quotes,
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="quotes.json",
        )
        bar_event = normalize_fyers_history(
            history,
            symbol="NSE:NIFTY50-INDEX",
            resolution="5",
            normalization_version="1",
            raw_ref="history.json",
        )
        ref_event = normalize_fyers_instrument_reference(
            chain,
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="chain.json",
            quote_capture=quotes,
        )
        assert quote_event.event_type == "QUOTE_SNAPSHOT"
        assert quote_event.payload["quote_count"] == 1
        assert bar_event.event_type == "BAR_SNAPSHOT"
        assert bar_event.payload["bar_count"] == 1
        assert ref_event.event_type == "INSTRUMENT_REFERENCE"
        assert ref_event.payload["expiry_count"] == 1
        assert ref_event.payload["tick_size"] == "0.05"
        depth_event = normalize_fyers_depth(
            _depth_capture(now),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="depth.json",
        )
        status_event = normalize_fyers_market_status(
            _status_capture(now),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="status.json",
            segment="NSE_FO",
        )
        assert depth_event.event_type == "DEPTH_SNAPSHOT"
        assert depth_event.payload["bid_count"] == 1
        assert status_event.event_type == "MARKET_STATUS"
        assert status_event.payload["status"] == "OPEN"

    def test_empty_chain_is_invalid(self) -> None:
        """Invariant 6: invalid data must not permit new exposure."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        capture = RawMarketCapture(
            capture_id="empty",
            provider="fyers",
            endpoint="/data/options-chain-v3",
            received_at=now,
            payload={"data": {"optionsChain": []}},
            http_status=200,
        )
        event = normalize_fyers_option_chain(
            capture,
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="raw.json",
        )
        report = assess_option_chain(event, now=now, max_age_ms=120_000)
        assert report.state is DataQuality.INVALID
        assert not report.permits_new_exposure


class TestMacroNewsFactor:
    def test_factor_is_weighted_bounded_and_evidence_linked(self) -> None:
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        items = (
            MacroNewsItem(
                event_id="NEWS-1",
                source="official-source",
                scope="NIFTY",
                published_at=now,
                received_at=now,
                sentiment=1,
                impact=Decimal("0.8"),
                confidence=Decimal("0.9"),
                evidence_ref="data/raw/news-1.json",
            ),
            MacroNewsItem(
                event_id="NEWS-FUTURE",
                source="official-source",
                scope="NIFTY",
                published_at=now.replace(hour=7),
                received_at=now.replace(hour=7, minute=1),
                sentiment=-1,
                impact=Decimal("1"),
                confidence=Decimal("1"),
                evidence_ref="data/raw/future.json",
            ),
        )
        factor = score_macro_news(
            items,
            scope="NIFTY",
            as_of=now,
            max_age_seconds=3600,
            half_life_seconds=1800,
            calculation_version="test-v1",
        )
        assert factor is not None
        assert factor.sentiment == 1
        assert factor.event_count == 1
        assert factor.evidence_ids == ("NEWS-1",)
        assert factor.evidence_refs == ("data/raw/news-1.json",)

    def test_stale_wrong_scope_and_not_yet_received_items_are_excluded(self) -> None:
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        one = Decimal("1")
        items = (
            MacroNewsItem(
                event_id="wrong-scope",
                source="official-source",
                scope="OTHER",
                published_at=now,
                received_at=now,
                sentiment=-1,
                impact=one,
                confidence=one,
                evidence_ref="evidence.json",
            ),
            MacroNewsItem(
                event_id="not-yet-received",
                source="official-source",
                scope="NIFTY",
                published_at=now,
                received_at=now.replace(hour=7),
                sentiment=-1,
                impact=one,
                confidence=one,
                evidence_ref="evidence.json",
            ),
            MacroNewsItem(
                event_id="stale",
                source="official-source",
                scope="NIFTY",
                published_at=now.replace(hour=3),
                received_at=now,
                sentiment=-1,
                impact=one,
                confidence=one,
                evidence_ref="evidence.json",
            ),
        )
        assert (
            score_macro_news(
                items,
                scope="NIFTY",
                as_of=now,
                max_age_seconds=3600,
                half_life_seconds=1800,
                calculation_version="test-v1",
            )
            is None
        )

    def test_stale_chain_blocks_exposure(self) -> None:
        """Invariant 6: stale option chain must not permit new exposure."""
        source = datetime(2024, 9, 13, 4, 0, tzinfo=UTC)
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        capture = _capture(source)
        event = normalize_fyers_option_chain(
            capture,
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="raw.json",
        )
        report = assess_option_chain(event, now=now, max_age_ms=60_000)
        assert report.state is DataQuality.STALE
        assert not report.permits_new_exposure

    def test_outside_session_blocks_exposure(self) -> None:
        """Invariant 6: outside a verified session window blocks new exposure."""
        now = datetime(2024, 9, 13, 3, 0, tzinfo=UTC)
        chain = normalize_fyers_option_chain(
            _capture(now),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="c.json",
        )
        report = assess_combined_snapshot(
            chain=chain,
            quote=None,
            bar=None,
            now=now,
            chain_max_age_ms=120_000,
            quote_max_age_ms=60_000,
            bar_max_age_ms=300_000,
            session=_session(),
        )
        assert report.state is DataQuality.INVALID
        assert ReasonCode.OUTSIDE_SESSION in report.reason_codes
        assert not report.permits_new_exposure

    def test_incomplete_warmup_blocks_exposure(self) -> None:
        """Invariant 6: incomplete warmup blocks new exposure."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        chain = normalize_fyers_option_chain(
            _capture(now),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="c.json",
        )
        report = assess_combined_snapshot(
            chain=chain,
            quote=None,
            bar=None,
            now=now,
            chain_max_age_ms=120_000,
            quote_max_age_ms=60_000,
            bar_max_age_ms=300_000,
            session=_session(),
            quality_config=QualityConfig(min_strike_count=50),
        )
        assert report.state is DataQuality.INVALID
        assert ReasonCode.WARMUP_INCOMPLETE in report.reason_codes
        assert not report.permits_new_exposure

    def test_clock_drift_and_cross_source_degrade(self) -> None:
        """Invariant 6: drift and mismatched feeds are marked, not hidden."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        old = datetime(2024, 9, 13, 5, 0, tzinfo=UTC)
        chain_capture = _capture(old)
        chain = normalize_fyers_option_chain(
            RawMarketCapture(
                capture_id="drift",
                provider="fyers",
                endpoint="options-chain-v3",
                received_at=now,
                payload=chain_capture.payload,
                http_status=200,
            ),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="c.json",
        )
        quote_payload = json.loads(QUOTES_FIXTURE.read_text(encoding="utf-8"))
        quote_payload["d"][0]["v"]["lp"] = 100.0
        quote = normalize_fyers_quotes(
            RawMarketCapture(
                capture_id="mismatch",
                provider="fyers",
                endpoint="quotes",
                received_at=now,
                payload=quote_payload,
                http_status=200,
            ),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="q.json",
        )
        report = assess_combined_snapshot(
            chain=chain,
            quote=quote,
            bar=None,
            now=now,
            chain_max_age_ms=10_000_000,
            quote_max_age_ms=60_000,
            bar_max_age_ms=300_000,
            session=_session(),
            quality_config=QualityConfig(max_clock_drift_ms=1_000),
        )
        assert report.state is DataQuality.DEGRADED
        assert ReasonCode.CLOCK_DRIFT in report.reason_codes
        assert ReasonCode.SNAPSHOT_MISMATCH in report.reason_codes


class TestPipelineAndReplay:
    def test_pipeline_persists_raw_and_canonical(self, tmp_path: Path) -> None:
        """Invariant 19: raw and canonical storage are preserved separately."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        store = JsonlEventStore(tmp_path / "data")
        pipeline_config = load_data_pipeline_config(PIPELINE_CONFIG)
        pipeline = DataPipeline(
            pipeline_config=pipeline_config,
            feed=FakeFeed(_capture(now)),
            store=store,
            app_config_path=BASE_CONFIG,
            repo_root=tmp_path,
            clock=FrozenClock(now),
        )
        result = pipeline.run_once(_underlying(), now=now)
        assert result.snapshot is not None
        types = {event.event_type for event in result.events}
        assert types >= {
            "OPTION_CHAIN_SNAPSHOT",
            "QUOTE_SNAPSHOT",
            "INSTRUMENT_REFERENCE",
            "BAR_SNAPSHOT",
            "DEPTH_SNAPSHOT",
            "MARKET_STATUS",
        }
        assert (tmp_path / result.raw_refs[0]).is_file()
        assert result.snapshot.market.bid is not None
        assert result.snapshot.market.ask is not None
        assert result.snapshot.market.open is not None
        assert result.snapshot.market.bid_size == 150
        assert result.snapshot.features["atm_delta"] == Decimal("0.52")
        assert result.snapshot.features["call_oi"] == Decimal(2000)
        canonical = list((tmp_path / "data" / "canonical").glob("*.jsonl"))
        assert canonical

    def test_macro_factor_enters_snapshot_and_replays_with_lineage(
        self, tmp_path: Path
    ) -> None:
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        input_file = tmp_path / "data" / "macro_news.jsonl"
        input_file.parent.mkdir(parents=True)
        item = MacroNewsItem(
            event_id="NEWS-REPLAY-1",
            source="official-source",
            scope="NIFTY",
            published_at=now,
            received_at=now,
            sentiment=-1,
            impact=Decimal("0.75"),
            confidence=Decimal("0.8"),
            evidence_ref="data/raw/news-replay-1.json",
        )
        input_file.write_text(item.model_dump_json() + "\n", encoding="utf-8")
        store = JsonlEventStore(tmp_path / "data-store")
        pipeline_config = load_data_pipeline_config(PIPELINE_CONFIG)
        pipeline = DataPipeline(
            pipeline_config=pipeline_config,
            feed=FakeFeed(_capture(now)),
            store=store,
            app_config_path=BASE_CONFIG,
            repo_root=tmp_path,
            clock=FrozenClock(now),
        )
        run = pipeline.run_once(_underlying(), now=now)
        assert run.snapshot is not None
        assert run.snapshot.features["macro_news_sentiment"] == -1
        assert run.snapshot.features["macro_news_coverage"] > 0
        assert run.snapshot.features["macro_news_event_count"] == 1
        assert run.macro_news_parse_errors == ()
        assert "NEWS-REPLAY-1" in run.snapshot.lineage.source_ids
        assert "data/raw/news-replay-1.json" in run.snapshot.lineage.raw_event_refs

        loaded = load_config(BASE_CONFIG)
        replay = ReplayEngine(
            pipeline_config=pipeline_config,
            store=store,
            config_version=loaded.version,
            config_checksum=loaded.checksum,
        ).replay(
            _underlying(),
            start=now.replace(hour=0),
            end=now.replace(hour=23),
        )
        assert replay.snapshots[0].features == run.snapshot.features

    def test_bad_macro_line_does_not_break_fetch(self, tmp_path: Path) -> None:
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        input_file = tmp_path / "data" / "macro_news.jsonl"
        input_file.parent.mkdir(parents=True)
        good = MacroNewsItem(
            event_id="NEWS-OK",
            source="official-source",
            scope="NIFTY",
            published_at=now,
            received_at=now,
            sentiment=1,
            impact=Decimal("0.5"),
            confidence=Decimal("0.5"),
            evidence_ref="data/raw/news-ok.json",
        ).model_dump_json()
        input_file.write_text(f"{good}\n{{bad json\n", encoding="utf-8")
        store = JsonlEventStore(tmp_path / "data-store")
        pipeline = DataPipeline(
            pipeline_config=load_data_pipeline_config(PIPELINE_CONFIG),
            feed=FakeFeed(_capture(now)),
            store=store,
            app_config_path=BASE_CONFIG,
            repo_root=tmp_path,
            clock=FrozenClock(now),
        )
        run = pipeline.run_once(_underlying(), now=now)
        assert run.snapshot is not None
        assert len(run.macro_news_parse_errors) == 1

    def test_replay_rebuilds_snapshots_from_storage(self, tmp_path: Path) -> None:
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        store = JsonlEventStore(tmp_path / "data")
        pipeline_config = load_data_pipeline_config(PIPELINE_CONFIG)
        pipeline = DataPipeline(
            pipeline_config=pipeline_config,
            feed=FakeFeed(_capture(now)),
            store=store,
            app_config_path=BASE_CONFIG,
            repo_root=tmp_path,
            clock=FrozenClock(now),
        )
        pipeline.run_once(_underlying(), now=now)
        loaded = load_config(BASE_CONFIG)
        engine = ReplayEngine(
            pipeline_config=pipeline_config,
            store=store,
            config_version=loaded.version,
            config_checksum=loaded.checksum,
        )
        result = engine.replay(
            _underlying(),
            start=now.replace(hour=0),
            end=now.replace(hour=23),
        )
        assert len(result.snapshots) == 1
        assert result.snapshots[0].permits_new_exposure

    def test_catalog_writes_parquet_and_duckdb(self, tmp_path: Path) -> None:
        """Invariant 19: derived catalog matches JSONL event ids."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        store = JsonlEventStore(tmp_path / "data")
        catalog = CatalogWriter(
            tmp_path / "data",
            duckdb_path=tmp_path / "data" / "catalog.duckdb",
        )
        pipeline = DataPipeline(
            pipeline_config=load_data_pipeline_config(PIPELINE_CONFIG),
            feed=FakeFeed(_capture(now)),
            store=store,
            app_config_path=BASE_CONFIG,
            repo_root=tmp_path,
            clock=FrozenClock(now),
            catalog=catalog,
        )
        run = pipeline.run_once(_underlying(), now=now)
        parquet_files = list((tmp_path / "data" / "parquet").glob("*.parquet"))
        assert parquet_files
        connection = duckdb.connect(str(tmp_path / "data" / "catalog.duckdb"))
        count = connection.execute("select count(*) from canonical_events").fetchone()
        connection.close()
        assert count is not None
        assert count[0] == len(run.events)


class TestWebSocketTicks:
    def test_ws_tick_normalizes_and_collects_offline(self, tmp_path: Path) -> None:
        """Invariant 19: websocket ticks become canonical TICK events with lineage."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        store = JsonlEventStore(tmp_path / "data")
        tick_message: dict[str, object] = {
            "ltp": 24500.5,
            "bid_price": 24500.0,
            "ask_price": 24501.0,
        }

        class FakeSocket:
            def connect(self) -> None:
                return None

            def subscribe(
                self,
                symbols: list[str],
                data_type: str = "SymbolUpdate",
                channel: int = 11,
            ) -> None:
                assert symbols == ["NSE:NIFTY50-INDEX"]

            def keep_running(self) -> None:
                return None

            def close_connection(self) -> None:
                return None

        captured: list[dict[str, object]] = []

        def socket_factory(**kwargs: object) -> FakeSocket:
            captured.append(kwargs)
            socket = FakeSocket()

            def on_message(message: dict[str, object]) -> None:
                handler = kwargs.get("on_message")
                if callable(handler):
                    handler(message)

            socket.connect = lambda: on_message(tick_message)  # type: ignore[method-assign]
            return socket

        settings = FyersSettings.model_construct(
            fyers_app_id="app",
            fyers_secret_key="test-secret",
            fyers_access_token="test-token",
        )
        stream = FyersTickStream(
            settings,
            FrozenClock(now),
            store,
            repo_root=tmp_path,
            normalization_version="1",
            socket_factory=socket_factory,
        )
        result = stream.collect(
            "NSE:NIFTY50-INDEX",
            max_ticks=1,
            duration_seconds=1,
        )
        assert len(result.ticks) == 1
        assert result.stopped_reason == "max_ticks"
        event = normalize_fyers_ws_tick(
            tick_message,
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            receive_time=now,
        )
        assert event.event_type == "TICK"
        assert event.payload["ltp"] == 24500.5
        quote_quality = assess_quote_snapshot(
            normalize_fyers_quotes(
                _quotes_capture(now),
                symbol="NSE:NIFTY50-INDEX",
                normalization_version="1",
                raw_ref="q.json",
            ),
            now=now,
            max_age_ms=60_000,
        )
        chain = normalize_fyers_option_chain(
            _capture(now),
            symbol="NSE:NIFTY50-INDEX",
            normalization_version="1",
            raw_ref="c.json",
        )
        combined = assess_combined_snapshot(
            chain=chain,
            quote=normalize_fyers_quotes(
                _quotes_capture(now),
                symbol="NSE:NIFTY50-INDEX",
                normalization_version="1",
                raw_ref="q.json",
            ),
            bar=normalize_fyers_history(
                _history_capture(now),
                symbol="NSE:NIFTY50-INDEX",
                resolution="5",
                normalization_version="1",
                raw_ref="h.json",
            ),
            now=now,
            chain_max_age_ms=120_000,
            quote_max_age_ms=60_000,
            bar_max_age_ms=300_000,
        )
        assert quote_quality.state is DataQuality.VALID
        assert combined.state is DataQuality.VALID
        assert captured

    def test_ws_daemon_reconnects_once(self, tmp_path: Path) -> None:
        """Websocket daemon retries once after a failed connect."""
        now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
        store = JsonlEventStore(tmp_path / "data")
        attempts = {"n": 0}

        class FakeSocket:
            def connect(self) -> None:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("socket down")

            def subscribe(
                self,
                symbols: list[str],
                data_type: str = "SymbolUpdate",
                channel: int = 11,
            ) -> None:
                return None

            def keep_running(self) -> None:
                return None

            def close_connection(self) -> None:
                return None

        def socket_factory(**kwargs: object) -> FakeSocket:
            socket = FakeSocket()
            handler = kwargs.get("on_message")

            def connect() -> None:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("socket down")
                if callable(handler):
                    handler({"ltp": 24500.5})

            socket.connect = connect  # type: ignore[method-assign]
            return socket

        settings = FyersSettings.model_construct(
            fyers_app_id="app",
            fyers_secret_key="test-secret",
            fyers_access_token="test-token",
        )
        stream = FyersTickStream(
            settings,
            FrozenClock(now),
            store,
            repo_root=tmp_path,
            normalization_version="1",
            socket_factory=socket_factory,
            reconnect=True,
            reconnect_attempts=2,
            reconnect_backoff_seconds=0.0,
            sleep=lambda _: None,
        )
        result = stream.collect(
            "NSE:NIFTY50-INDEX",
            max_ticks=1,
            duration_seconds=1,
        )
        assert result.reconnects == 1
        assert len(result.ticks) == 1
