import os
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPTS = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS)
import generate_daily_report_v2 as report


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DailyHighRiskReportTests(unittest.TestCase):
    def test_format_fills_preserves_each_fill(self):
        rendered = report.format_fills([
            {'time_et': '09:31', 'side': 'BUY', 'symbol': 'GLW', 'qty': 5, 'price': 100.25},
            {'time_et': '09:46', 'side': 'SELL', 'symbol': 'MRK', 'qty': 2, 'price': 90},
        ])
        self.assertIn('09:31 BUY GLW 5 @ $100.25', rendered)
        self.assertIn('09:46 SELL MRK 2 @ $90.00', rendered)

    def test_fetch_high_risk_fills_follows_pages_and_keeps_all_rows(self):
        fake_wallet_report = SimpleNamespace(ET=ZoneInfo('America/New_York'))
        fake_wallets = SimpleNamespace(resolve=lambda name: ('key', 'secret', 'https://paper.example'))
        pages = [
            {'activities': [
                {'id': 'a', 'transaction_time': '2026-08-26T13:31:00Z', 'side': 'buy',
                 'symbol': 'GLW', 'qty': '5', 'price': '100.25'},
            ], 'next_page_token': 'page-2'},
            {'activities': [
                {'id': 'b', 'transaction_time': '2026-08-26T13:46:00Z', 'side': 'sell',
                 'symbol': 'MRK', 'qty': '2', 'price': '90'},
            ]},
        ]
        calls = []

        def fake_get(url, **kwargs):
            calls.append(kwargs['params'])
            return _Response(pages.pop(0))

        with patch.dict(sys.modules, {'wallet_report': fake_wallet_report, 'wallets': fake_wallets}):
            with patch.object(report.requests, 'get', side_effect=fake_get):
                fills, error = report.fetch_high_risk_fills()

        self.assertIsNone(error)
        self.assertEqual([(f['side'], f['symbol']) for f in fills], [('BUY', 'GLW'), ('SELL', 'MRK')])
        self.assertEqual(calls[1]['page_token'], 'page-2')
    def test_fetch_high_risk_fills_uses_last_activity_id_for_bare_list_pagination(self):
        fake_wallet_report = SimpleNamespace(ET=ZoneInfo('America/New_York'))
        fake_wallets = SimpleNamespace(resolve=lambda name: ('key', 'secret', 'https://paper.example'))
        first_page = [
            {'id': f'a-{index}', 'transaction_time': '2026-08-26T13:31:00Z', 'side': 'buy',
             'symbol': 'GLW', 'qty': '1', 'price': '100'}
            for index in range(100)
        ]
        second_page = [
            {'id': 'b-1', 'transaction_time': '2026-08-26T13:46:00Z', 'side': 'sell',
             'symbol': 'MRK', 'qty': '2', 'price': '90'},
        ]
        pages, calls = [first_page, second_page], []

        def fake_get(url, **kwargs):
            calls.append(kwargs['params'])
            return _Response(pages.pop(0))

        with patch.dict(sys.modules, {'wallet_report': fake_wallet_report, 'wallets': fake_wallets}):
            with patch.object(report.requests, 'get', side_effect=fake_get):
                fills, error = report.fetch_high_risk_fills()

        self.assertIsNone(error)
        self.assertEqual(len(fills), 101)
        self.assertEqual(calls[1]['page_token'], 'a-99')
    def test_summary_uses_complete_fill_count_without_raw_dumping_every_fill(self):
        summary = report.summarize_fills([
            {'time_et': '09:31', 'side': 'BUY', 'symbol': 'GLW', 'qty': 5, 'price': 100.25},
            {'time_et': '09:46', 'side': 'SELL', 'symbol': 'MRK', 'qty': 2, 'price': 90},
        ])
        self.assertIn('2 fills', summary)
        self.assertIn('BUY GLW 5', summary)
        self.assertIn('09:31–09:46 ET', summary)


if __name__ == '__main__':
    unittest.main()
