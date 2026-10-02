"""Offline ledger and policy-evaluation tests; no broker or Telegram calls."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_decisions_are_durable_and_replays_are_idempotent(tmp_path):
    assert importlib.util.find_spec('wb_learning') is not None, 'learning ledger is missing'
    from wb_learning import LearningLedger

    path = tmp_path / 'learning.sqlite'
    event = {'policy': 'trend', 'reason': 'verified trend', 'symbol': 'NVDA'}
    with LearningLedger(path) as ledger:
        assert ledger.record('decision-1', 'decision', '2026-10-01T14:00:00Z', event)
        assert not ledger.record('decision-1', 'decision', '2026-10-01T14:00:00Z', event)
    with LearningLedger(path) as ledger:
        assert ledger.events() == [{'event_id': 'decision-1', 'kind': 'decision',
                                    'asof': '2026-10-01T14:00:00Z', 'payload': event}]


def test_conflicting_replay_cannot_rewrite_evidence(tmp_path):
    from wb_learning import LearningLedger
    with LearningLedger(tmp_path / 'ledger.sqlite') as ledger:
        ledger.record('one', 'decision', '2026-10-01T14:00:00Z', {'reason': 'original'})
        with pytest.raises(ValueError, match='different evidence'):
            ledger.record('one', 'decision', '2026-10-01T14:00:00Z', {'reason': 'rewrite'})
        assert ledger.events()[0]['payload'] == {'reason': 'original'}


@pytest.mark.parametrize('event_id,kind,asof,payload', [
    ('', 'decision', '2026-10-01T14:00:00Z', {}),
    ('one', 'unknown', '2026-10-01T14:00:00Z', {}),
    ('one', 'decision', '2026-10-01T14:00:00', {}),
    ('one', 'decision', 'not-a-time', {}),
    ('one', 'decision', '2026-10-01T14:00:00Z', {'nested': {'api_key': 'do-not-store'}}),
    ('one', 'decision', '2026-10-01T14:00:00Z', {'value': float('nan')}),
    ('one', 'decision', '2026-10-01T14:00:00Z', []),
])
def test_invalid_or_secret_evidence_is_rejected(tmp_path, event_id, kind, asof, payload):
    from wb_learning import LearningLedger
    with LearningLedger(tmp_path / 'ledger.sqlite') as ledger:
        with pytest.raises(ValueError):
            ledger.record(event_id, kind, asof, payload)
        assert ledger.events() == []


def test_ledger_file_is_owner_only(tmp_path):
    from wb_learning import LearningLedger
    path = tmp_path / 'ledger.sqlite'
    with LearningLedger(path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_event_order_uses_absolute_time(tmp_path):
    from wb_learning import LearningLedger
    with LearningLedger(tmp_path / 'ledger.sqlite') as ledger:
        ledger.record('later', 'equity', '2026-10-01T09:00:00-04:00', {})
        ledger.record('earlier', 'equity', '2026-10-01T14:00:00+08:00', {})
        assert [row['event_id'] for row in ledger.events()] == ['earlier', 'later']
