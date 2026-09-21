from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.universe.contracts import MarketRegime, StockScore, TradeDirection
from trading.universe.sector import classify_sector, compute_sector_rs


def compute_stock_score(
    symbol: str,
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]],
    nifty_bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]],
    sector_bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]],
    oi_data: list[tuple[datetime, int, Decimal]],
    as_of: datetime,
) -> StockScore:
    """Compute composite score for a stock."""
    # Defaults in case of missing data
    rs = Decimal("0")
    sector_rs = Decimal("0")
    vol_surge = Decimal("0")
    vwap_dist = Decimal("0")
    oi_score = Decimal("0")
    price_score = Decimal("0")
    
    # Needs at least 60 bars for good RS
    if len(bars) >= 60 and len(nifty_bars) >= 60:
        stock_ret = (bars[-1][4] - bars[-60][4]) / bars[-60][4]
        nifty_ret = (nifty_bars[-1][4] - nifty_bars[-60][4]) / nifty_bars[-60][4]
        rs = stock_ret - nifty_ret

    sector = classify_sector(symbol)
    if sector_bars:
        computed_srs = compute_sector_rs(
            bars[-60:] if len(bars) >= 60 else bars,
            sector_bars[-60:] if len(sector_bars) >= 60 else sector_bars
        )
        if computed_srs is not None:
            sector_rs = computed_srs

    if len(bars) >= 20:
        avg_vol = sum(b[5] for b in bars[-20:]) / Decimal("20")
        if avg_vol > Decimal("0"):
            vol_surge = bars[-1][5] / avg_vol
            
    if len(bars) >= 14:
        tr_sum = Decimal("0")
        for i in range(1, min(15, len(bars))):
            h, l, pc = bars[-i][2], bars[-i][3], bars[-i-1][4]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_sum += tr
        atr = tr_sum / min(Decimal("14"), Decimal(str(len(bars) - 1)))
        
        if atr > Decimal("0"):
            # vwap approximation
            vwap = (bars[-1][2] + bars[-1][3] + bars[-1][4]) / Decimal("3") 
            vwap_dist = (bars[-1][4] - vwap) / atr
            vwap_dist = max(Decimal("-1"), min(Decimal("1"), vwap_dist))

    if len(oi_data) >= 2:
        price_diff = oi_data[-1][2] - oi_data[-2][2]
        oi_diff = Decimal(oi_data[-1][1] - oi_data[-2][1])
        if price_diff > 0 and oi_diff > 0:
            oi_score = Decimal("1")
        elif price_diff < 0 and oi_diff > 0:
            oi_score = Decimal("-1")
            
    if len(bars) >= 4:
        hh = sum(1 for i in range(1, 4) if bars[-i][2] > bars[-i-1][2])
        hl = sum(1 for i in range(1, 4) if bars[-i][3] > bars[-i-1][3])
        price_score = Decimal(hh + hl) / Decimal("6") * Decimal("100")

    # Weights for calculation (config values would be used in a real scenario to inject these)
    w_rs = Decimal("0.25")
    w_sec = Decimal("0.15")
    w_vol = Decimal("0.15")
    w_vwap = Decimal("0.15")
    w_oi = Decimal("0.15")
    w_pr = Decimal("0.15")
    
    # Normalize roughly to 0-100 range
    norm_rs = max(Decimal("0"), min(Decimal("100"), (rs + Decimal("0.1")) * Decimal("500")))
    norm_sec = max(Decimal("0"), min(Decimal("100"), (sector_rs + Decimal("0.1")) * Decimal("500")))
    norm_vol = max(Decimal("0"), min(Decimal("100"), vol_surge * Decimal("20")))
    norm_vwap = max(Decimal("0"), min(Decimal("100"), (vwap_dist + Decimal("1")) * Decimal("50")))
    norm_oi = max(Decimal("0"), min(Decimal("100"), (oi_score + Decimal("1")) * Decimal("50")))
    
    composite = (
        norm_rs * w_rs +
        norm_sec * w_sec +
        norm_vol * w_vol +
        norm_vwap * w_vwap +
        norm_oi * w_oi +
        price_score * w_pr
    )
    
    direction = TradeDirection.LONG if composite >= Decimal("50") else TradeDirection.SHORT

    return StockScore(
        symbol=symbol,
        underlying=symbol,
        direction=direction,
        composite_score=composite,
        relative_strength=rs,
        sector_rs=sector_rs,
        volume_surge_ratio=vol_surge,
        vwap_distance=vwap_dist,
        oi_buildup_score=oi_score,
        price_structure_score=price_score,
        sector=sector,
        calculated_at=as_of
    )


def rank_universe(
    scores: list[StockScore],
    regime: MarketRegime,
    top_n: int = 5
) -> tuple[list[StockScore], list[StockScore]]:
    """Rank and filter top N longs and shorts based on regime."""
    longs = []
    shorts = []
    
    for s in scores:
        if s.direction == TradeDirection.LONG:
            longs.append(s)
        else:
            shorts.append(s)
            
    longs.sort(key=lambda x: x.composite_score, reverse=True)
    shorts.sort(key=lambda x: x.composite_score, reverse=False)

    if regime == MarketRegime.STRONG_BEAR:
        longs = []
    elif regime == MarketRegime.STRONG_BULL:
        shorts = []
        
    return longs[:top_n], shorts[:top_n]

__all__ = ["compute_stock_score", "rank_universe"]
