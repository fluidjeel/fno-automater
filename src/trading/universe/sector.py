from __future__ import annotations

from decimal import Decimal

NIFTY_SECTORS = {
    "HDFCBANK": "Banking",
    "ICICIBANK": "Banking",
    "SBI": "Banking",
    "KOTAKBANK": "Banking",
    "AXISBANK": "Banking",
    "TCS": "IT",
    "INFY": "IT",
    "HCLTECH": "IT",
    "WIPRO": "IT",
    "TECHM": "IT",
    "SUNPHARMA": "Pharma",
    "DRREDDY": "Pharma",
    "CIPLA": "Pharma",
    "DIVISLAB": "Pharma",
    "MARUTI": "Auto",
    "TATAMOTORS": "Auto",
    "M&M": "Auto",
    "BAJAJ-AUTO": "Auto",
    "HEROMOTOCO": "Auto",
    "ITC": "FMCG",
    "HUL": "FMCG",
    "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG",
    "TATASTEEL": "Metal",
    "HINDALCO": "Metal",
    "JSWSTEEL": "Metal",
    "RELIANCE": "Energy",
    "ONGC": "Energy",
    "NTPC": "Energy",
    "POWERGRID": "Energy",
    "BPCL": "Energy",
    "DLF": "Realty",
    "L&T": "Infra",
    "BAJFINANCE": "Financial Services",
    "BAJAJFINSV": "Financial Services",
    "ZEEL": "Media",
    "TITAN": "Consumer Durables",
    "ASIANPAINT": "Consumer Durables",
}

def classify_sector(symbol: str) -> str:
    """Classify a stock symbol into its sector."""
    return NIFTY_SECTORS.get(symbol, "Others")

_MIN_POINTS = 2

def compute_sector_rs(
    stock_bars: list[tuple], sector_bars: list[tuple]
) -> Decimal | None:
    """Compute relative strength of a stock vs its sector index."""
    if not stock_bars or not sector_bars:
        return None
    if len(stock_bars) < _MIN_POINTS or len(sector_bars) < _MIN_POINTS:
        return None

    stock_return = (stock_bars[-1][4] - stock_bars[0][4]) / stock_bars[0][4]
    sector_return = (sector_bars[-1][4] - sector_bars[0][4]) / sector_bars[0][4]

    return stock_return - sector_return

__all__ = ["NIFTY_SECTORS", "classify_sector", "compute_sector_rs"]
