"""Offline delivery state machine. No network, real recipients or production SLA."""

import json
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager


CHANNEL_WAIT = {'push': 3, 'sms': 10, 'email': 30}


def validate_response(value):
    """Strict schema for the deliberately small, mocked provider contract."""
    if not isinstance(value, dict):
        raise ValueError('response must be an object')
    if set(value) == {'timeout'} and value['timeout'] is True:
        return
    if type(value.get('status')) is int:
        if (value['status'] == 200 and set(value) == {'status', 'receipt'}
                and value['receipt'] in ('accepted', 'delivered')):
            return
        if (value['status'] in (400, 500) and set(value) == {'status', 'error'}
                and isinstance(value['error'], str) and value['error']):
            return
    raise ValueError('invalid provider response')


class Engine:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL, user_id TEXT NOT NULL,
                preferred TEXT NOT NULL, channels TEXT NOT NULL,
                deadline REAL NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                UNIQUE(event_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, notification_id TEXT NOT NULL
                    REFERENCES notifications(id),
                channel TEXT NOT NULL, position INTEGER NOT NULL,
                expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'SENDING',
                UNIQUE(notification_id, channel), UNIQUE(notification_id, position)
            );
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()

    def _notification(self, notification_id):
        row = self.db.execute('SELECT * FROM notifications WHERE id=?',
                              (notification_id,)).fetchone()
        if row is None:
            raise KeyError(notification_id)
        return row

    def _attempt(self, attempt_id):
        row = self.db.execute('SELECT * FROM attempts WHERE id=?',
                              (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return row

    def submit(self, event_id, user_id, preferred='push', now=0):
        if (not all(type(value) is str and value.strip() for value in (event_id, user_id))
                or preferred not in CHANNEL_WAIT):
            raise ValueError('event/user required; channel must be push, sms or email')
        channels = [preferred] + [c for c in CHANNEL_WAIT if c != preferred]
        with self.transaction():
            row = self.db.execute('''SELECT * FROM notifications
                WHERE event_id=? AND user_id=?''', (event_id, user_id)).fetchone()
            if row:
                if row['preferred'] != preferred:
                    raise ValueError('idempotency key reused with different preferences')
                return row['id']
            notification_id = str(uuid.uuid4())
            self.db.execute('''INSERT INTO notifications
                (id, event_id, user_id, preferred, channels, deadline)
                VALUES (?, ?, ?, ?, ?, ?)''',
                (notification_id, event_id, user_id, preferred,
                 json.dumps(channels), now + 60))
            return notification_id

    def status(self, notification_id):
        return self._notification(notification_id)['state']

    def attempts(self, notification_id):
        self._notification(notification_id)
        return [dict(row) for row in self.db.execute('''SELECT * FROM attempts
            WHERE notification_id=? ORDER BY position''', (notification_id,))]

    def next_attempt(self, notification_id, now):
        """Persist a send intent before returning it to the hypothetical adapter."""
        with self.transaction():
            notification = self._notification(notification_id)
            if notification['state'] != 'PENDING':
                return None
            attempts = self.attempts(notification_id)
            channels = json.loads(notification['channels'])
            if now >= notification['deadline']:
                self.db.execute("UPDATE notifications SET state='ESCALATED' WHERE id=?",
                                (notification_id,))
                return None
            if attempts:
                last = attempts[-1]
                if last['state'] != 'FAILED' and now < last['expires']:
                    return None
                if last['state'] in ('SENDING', 'WAITING'):
                    # A crash or missing ACK does not prove the remote send failed.
                    self.db.execute("UPDATE attempts SET state='UNKNOWN' WHERE id=?",
                                    (last['id'],))
            if len(attempts) == len(channels):
                self.db.execute("UPDATE notifications SET state='ESCALATED' WHERE id=?",
                                (notification_id,))
                return None
            channel = channels[len(attempts)]
            attempt_id = notification_id + '/' + channel
            self.db.execute('''INSERT INTO attempts
                (id, notification_id, channel, position, expires) VALUES (?, ?, ?, ?, ?)''',
                (attempt_id, notification_id, channel, len(attempts),
                 min(now + CHANNEL_WAIT[channel], notification['deadline'])))
            return dict(self._attempt(attempt_id))

    def _delivered(self, attempt):
        self.db.execute("UPDATE attempts SET state='DELIVERED' WHERE id=?", (attempt['id'],))
        self.db.execute("UPDATE notifications SET state='DELIVERED' WHERE id=?",
                        (attempt['notification_id'],))

    def receipt(self, attempt_id):
        """Simulates an already authenticated, correlated delivery callback."""
        with self.transaction():
            self._delivered(self._attempt(attempt_id))

    def response(self, attempt_id, payload, now):
        validate_response(payload)
        with self.transaction():
            attempt = self._attempt(attempt_id)
            if payload.get('receipt') == 'delivered':
                self._delivered(attempt)
                return
            if (self.status(attempt['notification_id']) == 'DELIVERED'
                    or attempt['state'] in ('DELIVERED', 'FAILED', 'UNKNOWN')):
                return
            if payload.get('status') == 400:
                state = 'FAILED'
            elif payload.get('receipt') == 'accepted' and now < attempt['expires']:
                state = 'WAITING'
            else:
                state = 'UNKNOWN'
            self.db.execute('UPDATE attempts SET state=? WHERE id=?', (state, attempt_id))


def main():
    with tempfile.TemporaryDirectory() as directory:
        engine = Engine(directory + '/demo.sqlite3')
        try:
            nid = engine.submit('transfer-demo', 'synthetic-user', now=0)
            push = engine.next_attempt(nid, now=0)
            engine.response(push['id'], {'timeout': True}, now=1)
            print('t=1 push: UNKNOWN; delivery not confirmed')
            sms = engine.next_attempt(nid, now=3)
            engine.response(sms['id'], {'status': 200, 'receipt': 'delivered'}, now=4)
            print('t=4 sms: DELIVERED; notification:', engine.status(nid))
            same = engine.submit('transfer-demo', 'synthetic-user', now=5)
            print('Event replay: same notification =', same == nid,
                  '; attempts =', len(engine.attempts(nid)))
        finally:
            engine.close()


if __name__ == '__main__':
    main()
