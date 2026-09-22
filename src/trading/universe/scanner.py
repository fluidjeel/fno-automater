from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.universe.config import UniverseScannerConfig
from trading.universe.contracts import MarketRegime, UniverseScanResult
from trading.universe.ranking import compute_stock_score, rank_universe

Bar = tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]
OiRecord = tuple[datetime, int, Decimal]


_MIN_BARS = 20

def scan_universe(
    *,
    stock_bars: dict[str, list[Bar]],
    nifty_bars: list[Bar],
    sector_bars: dict[str, list[Bar]],
    oi_data: dict[str, list[OiRecord]],
    regime: MarketRegime,
    config: UniverseScannerConfig,
    as_of: datetime,
    scan_id: str,
) -> UniverseScanResult:
    """Scan the universe for top long/short candidates."""
    scores = []
    total_scanned = 0

    for symbol, bars in stock_bars.items():
        if len(bars) < _MIN_BARS:
            continue

        # Liquidity filter
        avg_vol = sum(b[5] for b in bars[-_MIN_BARS:]) / Decimal("20")
        if avg_vol < Decimal(config.min_avg_daily_volume):
            continue

        total_scanned += 1

        symbol_oi = oi_data.get(symbol, [])
        sym_sector_bars = sector_bars.get(symbol, [])

        score = compute_stock_score(
            symbol=symbol,
            bars=bars,
            nifty_bars=nifty_bars,
            sector_bars=sym_sector_bars,
            oi_data=symbol_oi,
            as_of=as_of,
        )
        scores.append(score)

    longs, shorts = rank_universe(scores, regime, top_n=config.top_candidates)

    return UniverseScanResult(
        scan_id=scan_id,
        scanned_at=as_of,
        regime=regime,
        total_scanned=total_scanned,
        long_candidates=tuple(longs),
        short_candidates=tuple(shorts),
        scan_version=config.scan_version,
    )

__all__ = ["Bar", "OiRecord", "scan_universe"]
