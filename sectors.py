"""
Shared ticker → sector map.
=============================
Single source of truth for sector classification, used by:
  - politician_vetter.py  (sector-diversification scoring)
  - capitol_copier.py     (target-sector whitelist eligibility)

Sectors of interest for the current aggressive, sector-concentrated regime:
  tech, robotics, energy, industrial  (see strategy_config.json target_sectors)

`sector_of(ticker)` returns the sector string, or "unknown" if unmapped.
"""

TICKER_SECTOR = {
    # ── Tech (incl. semiconductors) ────────────────────────────────────────
    "AAPL": "tech", "MSFT": "tech", "GOOGL": "tech", "GOOG": "tech",
    "META": "tech", "AMZN": "tech", "NVDA": "tech", "ADBE": "tech",
    "CRM": "tech", "ORCL": "tech", "CSCO": "tech", "INTC": "tech",
    "AMD": "tech", "MU": "tech", "QCOM": "tech", "TXN": "tech",
    "AVGO": "tech", "NOW": "tech", "PINS": "tech", "NFLX": "tech",
    "TSLA": "tech", "UBER": "tech", "SHOP": "tech", "SQ": "tech",
    "PYPL": "tech", "SNOW": "tech", "PLTR": "tech", "DDOG": "tech",
    "NET": "tech", "CRWD": "tech", "APP": "tech", "CVNA": "tech",
    # more semiconductors / semi-equipment
    "MRVL": "tech", "ASML": "tech", "LRCX": "tech", "KLAC": "tech",
    "AMAT": "tech", "ON": "tech", "MCHP": "tech", "NXPI": "tech",
    "ADI": "tech", "SWKS": "tech", "MPWR": "tech", "ARM": "tech",
    "SMCI": "tech", "DELL": "tech", "HPQ": "tech", "WDC": "tech",
    "STX": "tech", "ANET": "tech", "PANW": "tech", "FTNT": "tech",
    "SNPS": "tech", "CDNS": "tech", "INTU": "tech", "IBM": "tech",

    # ── Robotics / automation ──────────────────────────────────────────────
    "ISRG": "robotics", "ABB": "robotics", "ROK": "robotics",
    "TER": "robotics", "ZBRA": "robotics", "PATH": "robotics",
    "IRBT": "robotics", "FANUY": "robotics", "OMCL": "robotics",
    "CGNX": "robotics", "NDSN": "robotics", "AVAV": "robotics",
    "SYM": "robotics", "BRKS": "robotics",

    # ── Energy ─────────────────────────────────────────────────────────────
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy",
    "EOG": "energy", "PXD": "energy", "OXY": "energy", "KMI": "energy",
    "WMB": "energy", "PSX": "energy", "ET": "energy", "MPC": "energy",
    "VLO": "energy", "HAL": "energy", "DVN": "energy", "FANG": "energy",
    "HES": "energy", "BKR": "energy", "OKE": "energy", "TRGP": "energy",
    "CTRA": "energy", "MRO": "energy", "APA": "energy", "ENPH": "energy",
    "FSLR": "energy", "SEDG": "energy", "RUN": "energy", "NEE": "energy",

    # ── Industrial / manufacturing ─────────────────────────────────────────
    "CAT": "industrial", "BA": "industrial", "HON": "industrial",
    "UPS": "industrial", "LMT": "industrial", "RTX": "industrial",
    "GE": "industrial", "DE": "industrial", "NOC": "industrial",
    "GD": "industrial", "MMM": "industrial", "EMR": "industrial",
    "ETN": "industrial", "PH": "industrial", "ITW": "industrial",
    "CMI": "industrial", "PCAR": "industrial", "GEV": "industrial",
    "NVT": "industrial", "PWR": "industrial", "AME": "industrial",
    "DOV": "industrial", "ROP": "industrial", "FTV": "industrial",
    "TT": "industrial", "CARR": "industrial", "JCI": "industrial",
    "LHX": "industrial", "TDG": "industrial", "GEHC": "industrial",
    "URI": "industrial", "FDX": "industrial", "WM": "industrial",

    # ── Financials ─────────────────────────────────────────────────────────
    "JPM": "finance", "BAC": "finance", "WFC": "finance", "C": "finance",
    "MS": "finance", "GS": "finance", "BLK": "finance", "SCHW": "finance",
    "AXP": "finance", "V": "finance", "MA": "finance", "COF": "finance",
    "TFC": "finance", "USB": "finance", "PNC": "finance", "NDAQ": "finance",
    "CME": "finance", "ICE": "finance", "SPGI": "finance", "MCO": "finance",
    "BRK": "finance", "BRKB": "finance", "APO": "finance",

    # ── Healthcare ─────────────────────────────────────────────────────────
    "JNJ": "healthcare", "UNH": "healthcare", "PFE": "healthcare",
    "MRK": "healthcare", "ABBV": "healthcare", "LLY": "healthcare",
    "TMO": "healthcare", "DHR": "healthcare", "BMY": "healthcare",
    "ABT": "healthcare", "CVS": "healthcare", "CI": "healthcare",
    "HUM": "healthcare", "BAX": "healthcare", "SYK": "healthcare",
    "MDT": "healthcare", "BBIO": "healthcare", "ZBH": "healthcare",

    # ── Consumer ───────────────────────────────────────────────────────────
    "WMT": "consumer", "COST": "consumer", "HD": "consumer", "LOW": "consumer",
    "TGT": "consumer", "MCD": "consumer", "SBUX": "consumer", "NKE": "consumer",
    "DIS": "consumer", "KO": "consumer", "PEP": "consumer", "CMG": "consumer",
    "BKNG": "consumer", "LULU": "consumer",

    # ── Utilities / Telecom / Other ────────────────────────────────────────
    "DUK": "utilities", "SO": "utilities", "ARE": "reit", "ACN": "services",
    "ADP": "services", "BR": "services", "CTSH": "services", "CHTR": "telecom",
    "T": "telecom", "VZ": "telecom", "TMUS": "telecom",
}


def sector_of(ticker: str) -> str:
    """Return the sector for a ticker, or 'unknown' if unmapped."""
    return TICKER_SECTOR.get(ticker, "unknown")
