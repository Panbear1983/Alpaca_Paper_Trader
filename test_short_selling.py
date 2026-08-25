import ast
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from short_selling import (
    AccountInfo,
    ApprovalError,
    ApprovalGate,
    AssetInfo,
    ExposureLimits,
    PreflightRejected,
    build_short_plan,
    preflight_short,
    size_short_position,
)


PAPER_URL = "https://paper-api.alpaca.markets"


class ShortSellingDomainTests(unittest.TestCase):
    def setUp(self):
        self.asset = AssetInfo(
            tradable=True,
            shortable=True,
            easy_to_borrow=True,
        )
        self.account = AccountInfo(
            shorting_enabled=True,
            buying_power=10_000.0,
            equity=10_000.0,
        )
        self.limits = ExposureLimits(
            per_trade_notional=5_000.0,
            gross_short_exposure=10_000.0,
            current_gross_short_exposure=2_000.0,
        )
        self.valid = {
            "base_url": PAPER_URL,
            "symbol": "AAPL",
            "qty": 100,
            "entry_price": 50.0,
            "stop_price": 51.0,
            "risk_per_trade_pct": 0.01,
            "asset": self.asset,
            "account": self.account,
            "limits": self.limits,
        }

    def decision(self, **overrides):
        values = dict(self.valid)
        values.update(overrides)
        return preflight_short(**values)

    def test_accepted_paper_plan_is_intent_only_with_mandatory_cover_stop(self):
        plan = build_short_plan(**self.valid)

        self.assertEqual(plan.base_url, PAPER_URL)
        self.assertTrue(plan.paper_only)
        self.assertEqual(plan.symbol, "AAPL")
        self.assertEqual(plan.qty, 100)

        self.assertEqual(plan.entry.action, "sell_short")
        self.assertEqual(plan.entry.side, "sell")
        self.assertEqual(plan.entry.order_type, "limit")
        self.assertEqual(plan.entry.limit_price, 50.0)
        self.assertFalse(plan.entry.execute)

        self.assertEqual(plan.protective_stop.action, "buy_to_cover")
        self.assertEqual(plan.protective_stop.side, "buy")
        self.assertEqual(plan.protective_stop.order_type, "stop")
        self.assertEqual(plan.protective_stop.stop_price, 51.0)
        self.assertFalse(plan.protective_stop.execute)

        self.assertEqual(plan.close_action.action, "buy_to_cover")
        self.assertEqual(plan.close_action.qty, 100)
        self.assertFalse(plan.close_action.execute)

        audit = asdict(plan.audit_event)
        self.assertEqual(audit["decision"], "approved")
        self.assertTrue(audit["reasons"])
        self.assertFalse(
            {"secret", "token", "credential", "order", "execution"}
            & set(audit)
        )

    def test_rejects_live_or_deceptive_nonpaper_urls(self):
        rejected_urls = (
            "https://api.alpaca.markets",
            "https://paper-api.alpaca.markets.evil.example",
            "http://paper-api.alpaca.markets",
            "https://example.com/paper-api.alpaca.markets",
        )
        for url in rejected_urls:
            with self.subTest(url=url):
                decision = self.decision(base_url=url)
                self.assertFalse(decision.approved)
                self.assertTrue(any("paper endpoint" in reason for reason in decision.reasons))

    def test_rejects_unshortable_asset(self):
        asset = AssetInfo(tradable=True, shortable=False, easy_to_borrow=True)
        decision = self.decision(asset=asset)
        self.assertFalse(decision.approved)
        self.assertTrue(any("not shortable" in reason for reason in decision.reasons))

    def test_rejects_non_etb_without_claiming_locate_availability(self):
        asset = AssetInfo(tradable=True, shortable=True, easy_to_borrow=False)
        decision = self.decision(asset=asset)
        joined = " ".join(decision.reasons).lower()
        self.assertFalse(decision.approved)
        self.assertIn("explicit broker locate evidence", joined)
        self.assertIn("unavailable", joined)
        self.assertNotIn("locate available", joined)

    def test_rejects_missing_account_shorting_permission(self):
        account = AccountInfo(
            shorting_enabled=False,
            buying_power=10_000.0,
            equity=10_000.0,
        )
        decision = self.decision(account=account)
        self.assertFalse(decision.approved)
        self.assertTrue(any("shorting is not enabled" in reason for reason in decision.reasons))

    def test_rejects_insufficient_buying_power(self):
        account = AccountInfo(
            shorting_enabled=True,
            buying_power=4_999.99,
            equity=10_000.0,
        )
        decision = self.decision(account=account)
        self.assertFalse(decision.approved)
        self.assertTrue(any("buying power" in reason for reason in decision.reasons))

    def test_rejects_per_trade_and_gross_exposure_caps(self):
        per_trade = ExposureLimits(
            per_trade_notional=4_999.99,
            gross_short_exposure=10_000.0,
            current_gross_short_exposure=0.0,
        )
        gross = ExposureLimits(
            per_trade_notional=5_000.0,
            gross_short_exposure=6_999.99,
            current_gross_short_exposure=2_000.0,
        )

        per_trade_decision = self.decision(limits=per_trade)
        gross_decision = self.decision(limits=gross)

        self.assertFalse(per_trade_decision.approved)
        self.assertTrue(any("per-trade" in reason for reason in per_trade_decision.reasons))
        self.assertFalse(gross_decision.approved)
        self.assertTrue(any("gross short-exposure" in reason for reason in gross_decision.reasons))

    def test_rejects_nonprotective_stop(self):
        for stop_price in (50.0, 49.99):
            with self.subTest(stop_price=stop_price):
                decision = self.decision(stop_price=stop_price)
                self.assertFalse(decision.approved)
                self.assertTrue(any("above entry" in reason for reason in decision.reasons))

        with self.assertRaises(PreflightRejected):
            build_short_plan(**{**self.valid, "stop_price": 50.0})

    def test_normalized_preflight_rejects_invalid_core_values(self):
        invalid_cases = (
            {"symbol": "aapl"},
            {"symbol": " AAPL "},
            {"qty": 0},
            {"qty": 1.5},
            {"entry_price": float("inf")},
            {"entry_price": 0.0},
            {
                "account": AccountInfo(
                    shorting_enabled=True,
                    buying_power=10_000.0,
                    equity=float("nan"),
                )
            },
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                self.assertFalse(self.decision(**overrides).approved)

    def test_deterministic_sizing_uses_whole_shares_and_risk_bound(self):
        self.assertEqual(
            size_short_position(
                equity=10_000.0,
                risk_per_trade_pct=0.01,
                entry_price=50.0,
                stop_price=51.0,
            ),
            100,
        )
        self.assertEqual(
            size_short_position(
                equity=10_000.0,
                risk_per_trade_pct=0.01,
                entry_price=50.0,
                stop_price=51.50,
            ),
            66,
        )
        oversized = self.decision(qty=101)
        self.assertFalse(oversized.approved)
        self.assertTrue(any("risk-sized maximum" in reason for reason in oversized.reasons))

    def test_sizing_rejects_invalid_or_nonprotective_stop(self):
        for stop_price in (50.0, 49.0, float("inf")):
            with self.subTest(stop_price=stop_price):
                with self.assertRaises(ValueError):
                    size_short_position(
                        equity=10_000.0,
                        risk_per_trade_pct=0.01,
                        entry_price=50.0,
                        stop_price=stop_price,
                    )

    def test_approval_requires_exact_phrase_and_paper_flag(self):
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        gate = ApprovalGate(
            symbol="AAPL",
            qty=100,
            token="single-use-secret",
            expires_at=now + timedelta(minutes=5),
        )

        with self.assertRaisesRegex(ApprovalError, "exact phrase"):
            gate.approve(
                phrase="PAPER-SHORT AAPL 100 ",
                token="single-use-secret",
                now=now,
                paper_only=True,
            )
        with self.assertRaisesRegex(ApprovalError, "paper-only"):
            gate.approve(
                phrase="PAPER-SHORT AAPL 100",
                token="single-use-secret",
                now=now,
                paper_only=False,
            )

        receipt = gate.approve(
            phrase="PAPER-SHORT AAPL 100",
            token="single-use-secret",
            now=now,
            paper_only=True,
        )
        self.assertTrue(receipt.approved)
        self.assertEqual(receipt.symbol, "AAPL")
        self.assertEqual(receipt.qty, 100)
        self.assertNotIn("token", asdict(receipt))

    def test_approval_rejects_expired_token(self):
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        gate = ApprovalGate(
            symbol="AAPL",
            qty=100,
            token="single-use-secret",
            expires_at=now,
        )
        with self.assertRaisesRegex(ApprovalError, "expired"):
            gate.approve(
                phrase="PAPER-SHORT AAPL 100",
                token="single-use-secret",
                now=now,
                paper_only=True,
            )

    def test_approval_token_is_single_use(self):
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        gate = ApprovalGate(
            symbol="AAPL",
            qty=100,
            token="single-use-secret",
            expires_at=now + timedelta(minutes=5),
        )
        approval = {
            "phrase": "PAPER-SHORT AAPL 100",
            "token": "single-use-secret",
            "now": now,
            "paper_only": True,
        }
        gate.approve(**approval)
        with self.assertRaisesRegex(ApprovalError, "already used"):
            gate.approve(**approval)

    def test_production_module_has_no_network_process_or_eval_imports(self):
        source_path = Path(__file__).with_name("short_selling.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        forbidden = {"requests", "subprocess", "eval"}
        imported = set()
        eval_calls = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
                imported.update(alias.name for alias in node.names)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "eval"
            ):
                eval_calls.append(node)

        self.assertFalse(forbidden & imported)
        self.assertFalse(eval_calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
