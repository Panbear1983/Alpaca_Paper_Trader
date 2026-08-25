"""
ISR Alpha Loader — Parse Global_100k_Investment_Database.csv
=============================================================
Loads the ISR 2026 CSV, filters US-listed companies, extracts structured fields.
"""
import csv
import os
import re
from dataclasses import dataclass
from typing import Optional

# Path to ISR database
ISR_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "Investment_Strategy_Research_2026", "datasets",
    "Global_100k_Investment_Database.csv"
)

# Column indices from CSV header (0-based)
COLS = {
    "country": 0,
    "timeframe": 1,
    "sub_sector": 2,
    "industry": 3,
    "tier": 4,
    "company": 5,           # "公司名稱與代號 (Company)" - e.g., "NVIDIA (NVDA)"
    "market_cap": 6,        # "資本��/市值 (Capital/Market Cap)"
    "core_business": 7,
    "clients_orders": 8,
    "technical_moat": 9,
    "revenue_breakdown": 10,
    "gross_margin": 11,
    "competitors": 12,
    "catalysts_12m": 13,    # "未來1年關���催化�� (12M Catalysts)"
    "key_risks": 14,
    "ceo_management": 15,
    "patents_ip": 16,
    "mna_3y": 17,
    "capex_expansion": 18,
    "geopolitical": 19,
    "mna_potential": 20,
    "insider_trading": 21,
}


@dataclass
class ISRCompany:
    """Structured company data from ISR database."""
    ticker: str
    name: str
    country: str
    timeframe: str
    sub_sector: str
    industry: str
    tier: str
    market_cap_raw: str
    market_cap_usd: float
    core_business: str
    clients_orders: str
    technical_moat: str
    revenue_breakdown: str
    gross_margin: str
    competitors: str
    catalysts_12m: str
    key_risks: str
    ceo_management: str
    patents_ip: str
    mna_3y: str
    capex_expansion: str
    geopolitical: str
    mna_potential: str
    insider_trading: str
    
    # Computed signals (filled by signals.py)
    catalyst_density: float = 0.0
    moat_score: float = 0.0
    margin_inflection: float = 0.0
    capex_acceleration: float = 0.0
    geo_resilience: float = 0.0
    mna_optionality: float = 0.0
    insider_conviction: float = 0.0
    composite: float = 0.0


def parse_market_cap(mcap_str: str) -> float:
    """Convert market cap string to USD float."""
    if not mcap_str:
        return 0.0
    s = mcap_str.strip()
    
    # Handle ranges like '約 4540億至4770億美元' - take the midpoint
    if '至' in s:
        parts = s.split('至')
        try:
            val1 = parse_market_cap(parts[0])
            val2 = parse_market_cap(parts[1])
            return (val1 + val2) / 2
        except Exception:
            pass
    
    # Remove '約' and spaces
    s = s.replace('約', '').replace(' ', '')
    
    # Remove parenthetical content (Chinese and English) - handle unclosed parentheses
    # First: normal case with full-width or half-width parentheses
    s = re.sub(r'[（(][^）)]*[）)]', '', s)
    # Second: unclosed parentheses at end of string
    s = re.sub(r'[（(][^）)]*$', '', s)
    # Third: handle URLs like [text](url) - remove everything from ( to end if no closing )
    s = re.sub(r'\([^)]*$', '', s)
    
    # Chinese multipliers - order matters! Check ���� (trillion) first
    # ���� = 10^12 (trillion), ���� = 10^8 (100 million), 万 = 10^4
    if '\u5146' in s:  # ����
        num_str = s.replace('\u5146', '').replace('美元', '').replace('台\u5146', '').replace(',', '')
        try:
            return float(num_str) * 1_000_000_000_000
        except Exception:
            pass
    if '\u5104' in s:  # ����
        num_str = s.replace('\u5104', '').replace('美元', '').replace('台\u5146', '').replace(',', '')
        try:
            return float(num_str) * 100_000_000
        except Exception:
            pass
    if '\u4e07' in s:  # 万
        num_str = s.replace('\u4e07', '').replace('美元', '').replace('台\u5146', '').replace(',', '')
        try:
            return float(num_str) * 10_000
        except Exception:
            pass
    
    # English multipliers
    multipliers = {'T': 1_000_000_000_000, 'B': 1_000_000_000, 'M': 1_000_000, 'K': 1_000}
    for unit, mult in multipliers.items():
        if unit in s:
            num_str = s.replace(unit, '').replace('$', '').replace(',', '').strip()
            try:
                return float(num_str) * mult
            except Exception:
                pass
    
    # Plain number
    try:
        return float(s.replace('$', '').replace(',', '').strip())
    except Exception:
        pass
    
    # Last resort: extract number-multiplier pairs from the string
        # This handles cases like "截至2026年7月17日市值約1,399億美元"
        matches = re.findall(r'([\d,]+(?:\.\d+)?)\s*([\u5104\u5146])', s)
        for num_str, multiplier in matches:
            try:
                num = float(num_str.replace(',', ''))
                if multiplier == '\u5104':  # ����
                    return num * 100_000_000
                elif multiplier == '\u5146':  # ����
                    return num * 1_000_000_000_000
            except Exception:
                pass
    
        # Also check for English multipliers (B, T)
        matches_en = re.findall(r'([\d,]+(?:\.\d+)?)\s*([BT])', s)
        for num_str, multiplier in matches_en:
            try:
                num = float(num_str.replace(',', ''))
                if multiplier == 'B':
                    return num * 1_000_000_000
                elif multiplier == 'T':
                    return num * 1_000_000_000_000
            except Exception:
                pass
    
        return 0.0


def extract_ticker(company_str: str) -> tuple[str, str]:
    """Extract ticker and name from 'Company Name (TICKER)' format."""
    match = re.search(r'\(([A-Z.]+)\)$', company_str.strip())
    if match:
        ticker = match.group(1)
        name = company_str[:match.start()].strip()
        return ticker, name
    return "", company_str.strip()


# Suffixes that indicate non-US listings
NON_US_SUFFIXES = (
    '.TW', '.HK', '.AS', '.L', '.TO', '.V', '.AX', '.PA', '.DE', 
    '.MI', '.ST', '.OL', '.CO', '.JK', '.KS', '.KQ', '.TWO', '.TPE'
)


def load_isr_database(csv_path: str | None = None) -> list[ISRCompany]:
    """Load and parse the ISR CSV database."""
    path = csv_path or ISR_CSV_PATH
    companies = []
    
    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader)  # Skip header
        
        for row in reader:
            if len(row) < 22:
                continue
            
            # Extract ticker first
            company_str = row[COLS["company"]].strip()
            ticker, name = extract_ticker(company_str)
            if not ticker:
                continue
            
            # Filter: US-listed companies (exclude non-US suffixes)
            if ticker.endswith(NON_US_SUFFIXES):
                continue
            
            # Parse market cap
            mcap_raw = row[COLS["market_cap"]].strip()
            mcap_usd = parse_market_cap(mcap_raw)
            
            # Filter: market cap > $2B for liquidity
            if mcap_usd < 2_000_000_000:
                continue
            
            # Filter: tier must be leader (龍頭股), second-tier (二線龍頭), or emerging (��力股)
            tier = row[COLS["tier"]].strip()
            if tier not in ("龍頭股", "二線龍頭", "\u6f5b\u529b\u80a1"):
                continue
            
            # Skip financials/REITs
            industry = row[COLS["industry"]].strip()
            if any(x in industry for x in ("銀行", "保��", "不動��", "金融")):
                continue
            
            country = row[COLS["country"]].strip()
            # FORBID: China-based companies (regardless of listing)
            if country == "China":
                continue
            
            comp = ISRCompany(
                ticker=ticker,
                name=name,
                country=country,
                timeframe=row[COLS["timeframe"]].strip(),
                sub_sector=row[COLS["sub_sector"]].strip(),
                industry=industry,
                tier=tier,
                market_cap_raw=mcap_raw,
                market_cap_usd=mcap_usd,
                core_business=row[COLS["core_business"]].strip(),
                clients_orders=row[COLS["clients_orders"]].strip(),
                technical_moat=row[COLS["technical_moat"]].strip(),
                revenue_breakdown=row[COLS["revenue_breakdown"]].strip(),
                gross_margin=row[COLS["gross_margin"]].strip(),
                competitors=row[COLS["competitors"]].strip(),
                catalysts_12m=row[COLS["catalysts_12m"]].strip(),
                key_risks=row[COLS["key_risks"]].strip(),
                ceo_management=row[COLS["ceo_management"]].strip(),
                patents_ip=row[COLS["patents_ip"]].strip(),
                mna_3y=row[COLS["mna_3y"]].strip(),
                capex_expansion=row[COLS["capex_expansion"]].strip(),
                geopolitical=row[COLS["geopolitical"]].strip(),
                mna_potential=row[COLS["mna_potential"]].strip(),
                insider_trading=row[COLS["insider_trading"]].strip(),
            )
            companies.append(comp)
    
    return companies


def get_us_universe(csv_path: str | None = None) -> list[ISRCompany]:
    """Get filtered US universe (alias for load_isr_database)."""
    return load_isr_database(csv_path)


if __name__ == "__main__":
    # Quick test
    companies = load_isr_database()
    print(f"Loaded {len(companies)} US companies from ISR database")
    for c in companies[:15]:
        print(f"  {c.ticker:6} | {c.name[:30]:30} | ${c.market_cap_usd/1e9:.1f}B | {c.sub_sector} | {c.tier}")