"""Core-hold flag — no network.

A core hold is a position Peter wants left to compound: the five-up-days rule
and the take-profit tiers skip it. Caps and the trailing stop do not.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_fields as cf  # noqa: E402
import sell_stop  # noqa: E402

CORE = cf.by_path("anchor.core_holds")


# ── the config field ─────────────────────────────────────────────────────────

def test_field_lives_in_the_anchor_section_and_may_be_blank():
    assert CORE is not None and CORE.section == "ANCHOR"
    assert cf.validate(CORE, "") == (True, [])
    assert cf.validate(CORE, "   ") == (True, [])


def test_typing_is_normalised_like_the_allow_list():
    ok, val = cf.validate(CORE, " nvda, tsla nvda ")
    assert ok is True and val == ["NVDA", "TSLA"]


def test_garbage_still_refused():
    ok, msg = cf.validate(CORE, "NV$DA")
    assert ok is False and "NV$DA" in msg


def test_row_reads_none_when_blank_and_the_names_otherwise():
    assert cf.fmt_value(CORE, [], cfg={}, compact=True) == "none"
    assert cf.fmt_value(CORE, ["NVDA"], cfg={}, compact=True) == "NVDA"
    assert cf.fmt_value(CORE, []) == ""          # edit box: blank to type into
    assert cf.fmt_value(CORE, ["NVDA", "TSLA"]) == "NVDA, TSLA"


def test_allow_list_behaviour_unchanged_by_the_new_type():
    uni = cf.by_path("anchor.universe")
    assert cf.validate(uni, "")[0] is False     # the allow list still refuses blank


# ── the five-up-days decision ────────────────────────────────────────────────

def test_rule_only_acts_on_day_five():
    assert sell_stop.decide(4, 20.0, False) == "none"


def test_day_five_sells_all_above_15_half_above_5_else_holds():
    assert sell_stop.decide(5, 15.1, False) == "sell_all"
    assert sell_stop.decide(5, 7.7, False) == "sell_half"
    assert sell_stop.decide(5, 5.0, False) == "hold"


def test_core_hold_is_never_sold_by_the_rule():
    assert sell_stop.decide(5, 7.7, True) == "core_hold"
    assert sell_stop.decide(5, 40.0, True) == "core_hold"
