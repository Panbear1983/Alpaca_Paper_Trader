"""
ISR Alpha Portfolio — Risk-Aware Position Sizing & Allocation
==============================================================
Builds target portfolio from ranked companies with position sizing,
sector caps, correlation limits, and risk management.
"""
from dataclasses import dataclass
from typing import List, Dict, Optional
from .loader import ISRCompany
from .signals import composite_score


@dataclass
class Position:
    """Target position for a ticker."""
    ticker: str
    company_name: str
    target_usd: float
    signal_score: float
    catalyst_density: float
    moat_score: float
    sub_sector: str
    tier: str
    engine: str  # "catalyst", "compounder", "capex"


# Engine assignment rules
CATALYST_THRESHOLD = 0.6      # High catalyst density -> Engine A
MOAT_THRESHOLD = 7.0          # High moat -> Engine B
CAPEX_THRESHOLD = 0.7         # High capex acceleration -> Engine C


def assign_engine(company: ISRCompany) -> str:
    """Assign company to trading engine based on signals."""
    if company.catalyst_density >= CATALYST_THRESHOLD:
        return "catalyst"      # Engine A: Event-driven (intraday/swing hybrid)
    elif company.moat_score >= MOAT_THRESHOLD:
        return "compounder"    # Engine B: Moat compounder (4-12 week hold)
    elif company.capex_acceleration >= CAPEX_THRESHOLD:
        return "capex"         # Engine C: Capex cycle (8-20 week hold)
    else:
        return "compounder"    # Default to compounder


def position_size(
    company: ISRCompany,
    equity: float,
    max_position_pct: float = 0.16,      # Max 16% per position
    base_size_usd: float = 5000.0,       # Base position size
) -> float:
    """Calculate position size based on signals and risk factors."""
    # Base size scales with composite score
    score_mult = max(0.5, min(2.0, company.composite / 0.4))
    
    # Moat quality multiplier
    moat_mult = max(0.5, min(1.5, company.moat_score / 8.0))
    
    # Geo resilience multiplier (penalize concentrated risk)
    geo_mult = max(0.5, min(1.2, 0.8 + company.geo_resilience * 0.4))
    
    # Catalyst density can increase size for event plays
    catalyst_mult = 1.0 + company.catalyst_density * 0.5
    
    size = base_size_usd * score_mult * moat_mult * geo_mult * catalyst_mult
    
    # Cap at max position % of equity
    max_size = equity * max_position_pct
    return min(size, max_size)


def build_target_portfolio(
    companies: List[ISRCompany],
    equity: float,
    max_positions: int = 15,
    max_sector_pct: float = 0.30,
    max_gross_leverage: float = 1.5,
) -> List[Position]:
    """Build target portfolio from ranked companies."""
    
    # Filter: only companies with decent composite score
    eligible = [c for c in companies if c.composite >= 0.25]
    
    # Sort by composite score descending
    eligible.sort(key=lambda x: x.composite, reverse=True)
    
    positions = []
    sector_exposure = {}  # sub_sector -> total USD
    total_allocated = 0.0
    
    for company in eligible:
        if len(positions) >= max_positions:
            break
        
        # Check sector cap
        sector = company.sub_sector
        sector_allocated = sector_exposure.get(sector, 0.0)
        proposed_size = position_size(company, equity)
        
        if sector_allocated + proposed_size > equity * max_sector_pct:
            # Reduce size to fit sector cap
            proposed_size = max(0, equity * max_sector_pct - sector_allocated)
            if proposed_size < 1000:  # Too small, skip
                continue
        
        # Check gross leverage
        if total_allocated + proposed_size > equity * max_gross_leverage:
            remaining = equity * max_gross_leverage - total_allocated
            if remaining < 1000:
                break
            proposed_size = remaining
        
        engine = assign_engine(company)
        
        pos = Position(
            ticker=company.ticker,
            company_name=company.name,
            target_usd=proposed_size,
            signal_score=company.composite,
            catalyst_density=company.catalyst_density,
            moat_score=company.moat_score,
            sub_sector=sector,
            tier=company.tier,
            engine=engine,
        )
        positions.append(pos)
        sector_exposure[sector] = sector_allocated + proposed_size
        total_allocated += proposed_size
    
    return positions


def rebalance_portfolio(
    current_positions: Dict[str, float],  # ticker -> current USD value
    target_positions: List[Position],
    equity: float,
    max_turnover_pct: float = 0.50,  # Max 50% portfolio turnover per rebalance
    min_order_usd: float = 100.0,
    min_drift_pct: float = 0.25,     # ignore drift smaller than this share of the position
) -> Dict[str, Dict]:
    """Generate rebalance orders from current to target.

    `min_drift_pct` is what stops the churn. A flat $100 floor meant a $5,000
    position drifting 2% produced a full round trip, and ISR recomputes its
    target every 10-15 minutes — so tiny ranking wobbles were turning over the
    book all day (2026-08-27: SH traded 8x, VFS 7x, $319k gross on a $72k
    account, realized -$260). Requiring the gap to be a meaningful share of the
    position before acting removes that.
    """
    
    current = {k.upper(): v for k, v in current_positions.items()}
    target = {p.ticker.upper(): p for p in target_positions}
    
    all_tickers = set(current.keys()) | set(target.keys())
    orders = {}
    
    total_turnover = 0.0
    
    for ticker in all_tickers:
        cur_val = current.get(ticker, 0.0)
        tgt_pos = target.get(ticker)
        tgt_val = tgt_pos.target_usd if tgt_pos else 0.0
        
        diff = tgt_val - cur_val

        # Absolute floor, plus a relative one for positions we already hold.
        # Opening a new position or closing one entirely is never suppressed.
        floor = min_order_usd
        if cur_val > 0 and tgt_val > 0:
            floor = max(floor, cur_val * min_drift_pct)
        if abs(diff) < floor:
            continue
        
        side = "buy" if diff > 0 else "sell"
        notional = abs(diff)
        
        orders[ticker] = {
            "side": side,
            "notional": notional,
            "current_value": cur_val,
            "target_value": tgt_val,
            "signal_score": tgt_pos.signal_score if tgt_pos else 0,
            "engine": tgt_pos.engine if tgt_pos else "exit",
            "reason": f"Rebalance to {tgt_val:.0f} from {cur_val:.0f}",
        }
        total_turnover += notional
    
    # Cap turnover
    if total_turnover > equity * max_turnover_pct:
        # Scale down all orders proportionally
        scale = equity * max_turnover_pct / total_turnover
        for o in orders.values():
            o["notional"] *= scale
    
    return orders


def risk_checks(positions: List[Position], equity: float) -> List[str]:
    """Run risk checks on portfolio, return list of warnings."""
    warnings = []
    
    total_gross = sum(p.target_usd for p in positions)
    if total_gross > equity * 1.5:
        warnings.append(f"Gross leverage {total_gross/equity:.1f}x exceeds 1.5x limit")
    
    # Sector concentration
    sector_exp = {}
    for p in positions:
        sector_exp[p.sub_sector] = sector_exp.get(p.sub_sector, 0) + p.target_usd
    
    for sector, exp in sector_exp.items():
        if exp > equity * 0.30:
            warnings.append(f"Sector {sector}: {exp/equity:.1%} exceeds 30% limit")
    
    # Single position concentration
    for p in positions:
        if p.target_usd > equity * 0.16:
            warnings.append(f"{p.ticker}: {p.target_usd/equity:.1%} exceeds 16% limit")
    
    # Too many catalyst positions (event risk)
    catalyst_count = sum(1 for p in positions if p.engine == "catalyst")
    if catalyst_count > 6:
        warnings.append(f"Too many catalyst positions ({catalyst_count}), max 6")
    
    return warnings


if __name__ == "__main__":
    from .loader import load_isr_database
    from .signals import rank_universe
    
    companies = load_isr_database()
    ranked = rank_universe(companies)
    
    equity = 100_000  # Example equity
    portfolio = build_target_portfolio(ranked, equity)
    
    print(f"Target Portfolio (Equity: ${equity:,.0f})")
    print(f"{'#':>2} {'Ticker':<6} {'Engine':<10} {'Target':>10} {'Score':>6} {'Cat':>4} {'Moat':>4} {'Sector'}")
    print("-" * 80)
    
    for i, p in enumerate(portfolio, 1):
        print(f"{i:2}. {p.ticker:<6} {p.engine:<10} ${p.target_usd:>9,.0f} {p.signal_score:.3f} "
              f"{p.catalyst_density:.2f} {p.moat_score:.1f} {p.sub_sector}")
    
    print(f"\nTotal allocated: ${sum(p.target_usd for p in portfolio):,.0f} "
          f"({sum(p.target_usd for p in portfolio)/equity:.1%} gross)")
    
    warnings = risk_checks(portfolio, equity)
    if warnings:
        print("\n������ Risk Warnings:")
        for w in warnings:
            print(f"  - {w}")