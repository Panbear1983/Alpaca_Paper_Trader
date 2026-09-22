"""Allow-list ('anchor.universe') editing in the strategy screen — no network.

The list is what the entry fence checks on every buy, manual ones included,
so these pin the three things that matter: what the row reads, what typing
gets accepted, and that the fence can never be switched on over an empty list.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_fields as cf  # noqa: E402

UNI = cf.by_path("anchor.universe")
FENCE = cf.by_path("anchor.enabled")
FIFTEEN = ["AAPL", "MSFT", "NVDA", "JPM", "CAT", "GOOGL", "V", "HOOD",
           "ALM", "TSLA", "SPCX", "SKHY", "MRVL", "AVGO", "AMD"]


def _cfg(enabled, names):
    return {"anchor": {"enabled": enabled, "universe": list(names)}}


# ── registration ────────────────────────────────────────────────────────────

def test_field_exists_and_sits_right_under_the_fence_switch():
    assert UNI is not None and UNI.ftype == "csv_tickers" and UNI.section == "ANCHOR"
    paths = [f.path for f in cf.FIELDS]
    assert paths.index("anchor.universe") == paths.index("anchor.enabled") + 1


def test_range_column_says_tickers():
    assert cf.fmt_range(UNI) == "tickers"


# ── what the table row reads ────────────────────────────────────────────────

def test_fence_off_reads_unrestricted_whatever_the_list_holds():
    assert cf.fmt_value(UNI, [], cfg=_cfg(False, []), compact=True) == cf.UNRESTRICTED
    assert cf.fmt_value(UNI, FIFTEEN, cfg=_cfg(False, FIFTEEN), compact=True) == cf.UNRESTRICTED


def test_no_anchor_block_at_all_reads_unrestricted():
    """Low Risk / Photonic before any fence block exists."""
    assert cf.fmt_value(UNI, None, cfg={}, compact=True) == cf.UNRESTRICTED


def test_fence_on_long_list_is_compacted_to_count_plus_first_five():
    s = cf.fmt_value(UNI, FIFTEEN, cfg=_cfg(True, FIFTEEN), compact=True)
    assert s.startswith("15 names: AAPL MSFT NVDA JPM CAT")
    assert s.endswith("…")
    assert len(s) < 45                       # must fit the 96-col dialog's value column


def test_fence_on_short_list_shows_every_name():
    s = cf.fmt_value(UNI, ["AAPL", "TSLA"], cfg=_cfg(True, ["AAPL", "TSLA"]), compact=True)
    assert s == "2 names: AAPL TSLA"


def test_fence_on_empty_list_is_flagged_not_hidden():
    s = cf.fmt_value(UNI, [], cfg=_cfg(True, []), compact=True)
    assert "empty" in s and "blocked" in s


def test_edit_dialog_gets_the_full_editable_list():
    assert cf.fmt_value(UNI, FIFTEEN) == ", ".join(FIFTEEN)
    assert cf.fmt_value(UNI, []) == ""      # empty box to type into, not a dash


# ── what typing gets accepted ───────────────────────────────────────────────

def test_commas_spaces_case_and_duplicates_are_all_tolerated():
    ok, val = cf.validate(UNI, " aapl, msft NVDA;tsla  aapl ")
    assert ok is True
    assert val == ["AAPL", "MSFT", "NVDA", "TSLA"]


def test_dotted_and_hyphenated_share_classes_are_valid():
    ok, val = cf.validate(UNI, "BRK.B BF-B")
    assert ok is True and val == ["BRK.B", "BF-B"]


def test_garbage_is_named_in_the_error():
    ok, msg = cf.validate(UNI, "AAPL, 123, NV$DA")
    assert ok is False
    assert "123" in msg and "NV$DA" in msg


def test_empty_list_is_refused_and_points_at_the_fence_switch():
    ok, msg = cf.validate(UNI, "   ")
    assert ok is False
    assert "fence" in msg.lower() and cf.UNRESTRICTED in msg


# ── the one hard refusal ────────────────────────────────────────────────────

def test_cannot_switch_fence_on_over_an_empty_list():
    why = cf.enable_blocked_reason(_cfg(False, []), FENCE, True)
    assert why and "empty" in why


def test_cannot_switch_fence_on_when_no_anchor_block_exists_yet():
    assert cf.enable_blocked_reason({}, FENCE, True)


def test_switching_on_with_names_switching_off_and_other_fields_are_fine():
    assert cf.enable_blocked_reason(_cfg(False, ["AAPL"]), FENCE, True) is None
    assert cf.enable_blocked_reason(_cfg(True, []), FENCE, False) is None
    other = cf.by_path("swing.enabled")
    assert cf.enable_blocked_reason(_cfg(False, []), other, True) is None


# ── nothing else changed ────────────────────────────────────────────────────

def test_existing_field_types_render_exactly_as_before():
    assert cf.fmt_value(cf.by_path("swing.enabled"), True) == "ON"
    assert cf.fmt_value(cf.by_path("swing.enabled"), None) == "—"
    assert cf.fmt_value(cf.by_path("dynamic_exits.stop_loss_pct"), 0.05) == "0.05"
    assert cf.fmt_value(cf.by_path("capitol_copier.target_sectors"), ["tech", "energy"]) == "tech,energy"
