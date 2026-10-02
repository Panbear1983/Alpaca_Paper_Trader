"""Local learning evidence; no broker access or automatic strategy rewrites."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path


EVENT_KINDS = frozenset({'decision', 'order', 'fill', 'equity', 'policy_evaluation',
                         'policy_rejection', 'risk_rejection'})
_SECRET_FIELDS = frozenset({'api_key', 'secret', 'secret_key', 'api_secret',
                            'token', 'access_token', 'bot_token', 'password',
                            'authorization', 'headers'})


def _validate_payload(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.lower().replace('-', '_') in _SECRET_FIELDS:
                raise ValueError('credential fields are not permitted in learning evidence')
            _validate_payload(child)
    elif isinstance(value, list):
        for child in value:
            _validate_payload(child)


class LearningLedger:
    """Append-only, idempotent evidence store for decisions and outcomes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            st = os.fstat(fd)
            if st.st_nlink != 1 or st.st_uid != os.getuid():
                raise ValueError('learning ledger must be owned by the current user with one link')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            'CREATE TABLE IF NOT EXISTS events ('
            'event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, '
            'asof TEXT NOT NULL, payload TEXT NOT NULL)'
        )
        self._db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._db.close()

    def record(self, event_id: str, kind: str, asof: str, payload: dict) -> bool:
        """Return False for an identical replay; never overwrite prior evidence."""
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError('event ID must be a nonempty string')
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise ValueError('unknown evidence kind')
        if not isinstance(asof, str):
            raise ValueError('evidence needs an aware timestamp')
        stamp = datetime.fromisoformat(asof.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('evidence needs an aware timestamp')
        if not isinstance(payload, dict):
            raise ValueError('evidence payload must be a dictionary')
        _validate_payload(payload)
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        row = (event_id, kind, asof, encoded)
        with self._db:
            cursor = self._db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)', row)
            inserted = cursor.rowcount == 1
            existing = self._db.execute(
                'SELECT event_id, kind, asof, payload FROM events WHERE event_id = ?',
                (event_id,),
            ).fetchone()
            if existing != row:
                raise ValueError('event ID already records different evidence')
        return inserted

    def events(self) -> list[dict]:
        """Read a chronological evidence snapshot without mutating it."""
        rows = self._db.execute(
            'SELECT event_id, kind, asof, payload FROM events ORDER BY julianday(asof), event_id'
        ).fetchall()
        return [{'event_id': row[0], 'kind': row[1], 'asof': row[2],
                 'payload': json.loads(row[3])} for row in rows]
