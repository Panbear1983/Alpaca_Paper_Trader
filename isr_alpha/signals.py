"""
ISR Alpha Signals — Compute 6 Alpha Signals from ISR Data
==========================================================
Computes: catalyst_density, moat_score, margin_inflection, 
capex_acceleration, geo_resilience, mna_optionality, insider_conviction
"""
import re
from typing import List
from .loader import ISRCompany


# Signal weights for composite score
SIGNAL_WEIGHTS = {
    "catalyst_density": 0.25,
    "moat_score": 0.20,
    "margin_inflection": 0.15,
    "capex_acceleration": 0.15,
    "geo_resilience": 0.10,
    "mna_optionality": 0.10,
    "insider_conviction": 0.05,
}


# Keywords for signal extraction
CATALYST_KEYWORDS = {
    "product_launch": ["新��品", "新平台", "推出", "量��", "出��", "放量", "��動", "部署"],
    "capacity_expansion": ["����", "新��", "��能", "建��", "投��", "��建"],
    "contract_win": ["��單", "合約", "獲單", "��署", "合作", "��伴"],
    "regulatory": ["認��", "批准", "��准", "FDA", "監管", "合規"],
    "earnings_inflection": ["上修", "超預期", "創新高", "����", "獲利增", "��收增"],
    "tech_milestone": ["良率", "突破", "����", "認��", "技術", "專利"],
}

MOAT_KEYWORDS = {
    "high": ["����", "無可取代", "��對優勢", "��家", "��心供應商", "關���供應商", "��定", "����成本��高", "生態系", "專利保護", "技術����"],
    "medium": ["龍頭", "領先", "市��率", "優勢", "護城河", "品牌", "規模", "成本優勢"],
    "low": ["競爭激烈", "��格戰", "同質化", "替代", "易進入"],
}

MARGIN_KEYWORDS = {
    "positive": ["毛利率提升", "毛利率��大", "毛利改善", "獲利結構優化", "高毛利��品��比提升", "定��權", "成本下降", "規模效應"],
    "negative": ["毛利率下��", "毛利率壓��", "成本上��", "��格競爭", "折��增加", "����"],
}

CAPEX_KEYWORDS = {
    "accelerating": ["上修", "大幅增加", "加��", "��大", "創紀錄", "歷史高位", "加速", "追加"],
    "steady": ["維持", "��定", "持續投入", "按計畫"],
    "decelerating": ["下修", "減少", "��減", "延��", "����", "��結"],
}

GEO_KEYWORDS = {
    "resilient": ["中國加一", "越南", "墨西哥", "美國本土", "多元化", "分散", "備援", "��性��能", "在地化", "供應���重組"],
    "concentrated": ["高度集中", "單一", "依��中國", "依��台灣", "地��風��", "關����擊"],
}

MNA_KEYWORDS = {
    "acquirer": ["收��", "����", "投資", "入股", "策略��伴", "整合"],
    "target": ["被����", "分��", "上市", "��在��家", "收��標的"],
    "none": ["無����", "無計畫", "專注內生"],
}

INSIDER_KEYWORDS = {
    "buy": ["��進", "增持", "��入", "認��"],
    "sell": ["��出", "減持", "獲利了結", "��售"],
    "none": ["無資料", "無內部人交易", "不適用"],
}


def count_keywords(text: str, keywords: dict) -> dict:
    """Count keyword matches by category."""
    text_lower = text.lower()
    counts = {}
    for category, kw_list in keywords.items():
        count = sum(1 for kw in kw_list if kw in text)
        counts[category] = count
    return counts


def compute_catalyst_density(company: ISRCompany) -> float:
    """Compute catalyst density from 12M catalysts text (0-1 scale)."""
    text = company.catalysts_12m
    if not text or text.strip() in ("無", "無資料", ""):
        return 0.0
    
    counts = count_keywords(text, CATALYST_KEYWORDS)
    total = sum(counts.values())
    
    # Normalize: ~5+ catalysts = 1.0, scale logarithmically
    if total == 0:
        return 0.0
    return min(1.0, total / 5.0)


def compute_moat_score(company: ISRCompany) -> float:
    """Compute moat score from technical_moat text (0-10 scale)."""
    text = company.technical_moat
    if not text:
        return 0.0
    
    counts = count_keywords(text, MOAT_KEYWORDS)
    
    score = 0.0
    score += counts.get("high", 0) * 2.5
    score += counts.get("medium", 0) * 1.0
    score -= counts.get("low", 0) * 1.5
    
    # Bonus for specific moat types mentioned
    if any(kw in text for kw in ["CUDA", "NVLink", "生態系", "專利", "專有", "��家"]):
        score += 1.0
    if any(kw in text for kw in ["����成本", "��定", "��定"]):
        score += 1.0
    
    return max(0.0, min(10.0, score))


def compute_margin_inflection(company: ISRCompany) -> float:
    """Compute margin inflection signal from gross_margin text (0-1 scale)."""
    text = company.gross_margin
    if not text:
        return 0.5
    
    counts = count_keywords(text, MARGIN_KEYWORDS)
    
    pos = counts.get("positive", 0)
    neg = counts.get("negative", 0)
    
    if pos + neg == 0:
        return 0.5
    
    # Score: positive signals push toward 1, negative toward 0
    return max(0.0, min(1.0, 0.5 + (pos - neg) * 0.2))


def compute_capex_acceleration(company: ISRCompany) -> float:
    """Compute capex acceleration from capex_expansion text (0-1 scale)."""
    text = company.capex_expansion
    if not text:
        return 0.5
    
    counts = count_keywords(text, CAPEX_KEYWORDS)
    
    accel = counts.get("accelerating", 0)
    steady = counts.get("steady", 0)
    decel = counts.get("decelerating", 0)
    
    total = accel + steady + decel
    if total == 0:
        return 0.5
    
    # Weight: accelerating=1.0, steady=0.5, decelerating=0.0
    return (accel * 1.0 + steady * 0.5 + decel * 0.0) / total


def compute_geo_resilience(company: ISRCompany) -> float:
    """Compute geopolitical resilience from geopolitical text (0-1 scale)."""
    text = company.geopolitical
    if not text:
        return 0.5
    
    counts = count_keywords(text, GEO_KEYWORDS)
    
    resilient = counts.get("resilient", 0)
    concentrated = counts.get("concentrated", 0)
    
    if resilient + concentrated == 0:
        return 0.5
    
    return max(0.0, min(1.0, 0.5 + (resilient - concentrated) * 0.25))


def compute_mna_optionality(company: ISRCompany) -> float:
    """Compute M&A optionality from mna_potential and mna_3y (0-1 scale)."""
    text = company.mna_potential + " " + company.mna_3y
    if not text.strip():
        return 0.0
    
    counts = count_keywords(text, MNA_KEYWORDS)
    
    acquirer = counts.get("acquirer", 0)
    target = counts.get("target", 0)
    none = counts.get("none", 0)
    
    if acquirer + target + none == 0:
        return 0.3
    
    # Both acquirer and target have optionality value
    score = (acquirer * 0.7 + target * 0.8) / (acquirer + target + none)
    return max(0.0, min(1.0, score))


def compute_insider_conviction(company: ISRCompany) -> float:
    """Compute insider conviction from insider_trading (0-1 scale)."""
    text = company.insider_trading
    if not text or text.strip() in ("無資料", "不適用", "無內部人交易", ""):
        return 0.5  # Neutral
    
    counts = count_keywords(text, INSIDER_KEYWORDS)
    
    buy = counts.get("buy", 0)
    sell = counts.get("sell", 0)
    none = counts.get("none", 0)
    
    if buy + sell + none == 0:
        return 0.5
    
    # Net buying is positive
    return max(0.0, min(1.0, 0.5 + (buy - sell) * 0.2))


def compute_signals(company: ISRCompany) -> ISRCompany:
    """Compute all 6 signals for a company."""
    company.catalyst_density = compute_catalyst_density(company)
    company.moat_score = compute_moat_score(company)
    company.margin_inflection = compute_margin_inflection(company)
    company.capex_acceleration = compute_capex_acceleration(company)
    company.geo_resilience = compute_geo_resilience(company)
    company.mna_optionality = compute_mna_optionality(company)
    company.insider_conviction = compute_insider_conviction(company)
    return company


def composite_score(company: ISRCompany) -> float:
    """Compute weighted composite score (0-1 scale)."""
    score = (
        SIGNAL_WEIGHTS["catalyst_density"] * company.catalyst_density +
        SIGNAL_WEIGHTS["moat_score"] * (company.moat_score / 10.0) +
        SIGNAL_WEIGHTS["margin_inflection"] * company.margin_inflection +
        SIGNAL_WEIGHTS["capex_acceleration"] * company.capex_acceleration +
        SIGNAL_WEIGHTS["geo_resilience"] * company.geo_resilience +
        SIGNAL_WEIGHTS["mna_optionality"] * company.mna_optionality +
        SIGNAL_WEIGHTS["insider_conviction"] * company.insider_conviction
    )
    company.composite = score
    return score


def rank_universe(companies: List[ISRCompany]) -> List[ISRCompany]:
    """Compute signals and rank by composite score descending."""
    for c in companies:
        compute_signals(c)
        composite_score(c)
    return sorted(companies, key=lambda x: x.composite, reverse=True)


if __name__ == "__main__":
    from .loader import load_isr_database
    companies = load_isr_database()
    ranked = rank_universe(companies)
    print(f"Ranked {len(ranked)} companies by composite score")
    for i, c in enumerate(ranked[:20], 1):
        print(f"  {i:2}. {c.ticker:6} | composite={c.composite:.3f} | "
              f"cat={c.catalyst_density:.2f} moat={c.moat_score:.1f} "
              f"margin={c.margin_inflection:.2f} capex={c.capex_acceleration:.2f} "
              f"geo={c.geo_resilience:.2f} mna={c.mna_optionality:.2f} "
              f"insider={c.insider_conviction:.2f} | {c.sub_sector}")