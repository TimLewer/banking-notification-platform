import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from notification_demo import Engine, validate_response


FIXTURES = json.loads((Path(__file__).parent / 'fixtures/providers_sample.json').read_text())['cases']


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'delivery.sqlite3')
        self.engine = Engine(self.path)
        self.nid = self.engine.submit('transfer-1', 'user-1', now=0)

    def tearDown(self):
        self.engine.close()
        self.tmp.cleanup()

    def attempt(self, now=0):
        return self.engine.next_attempt(self.nid, now=now)

    def test_happy_path_requires_delivery_receipt(self):
        attempt = self.attempt()
        self.engine.response(attempt['id'], FIXTURES['delivered'], now=1)
        self.assertEqual(self.engine.status(self.nid), 'DELIVERED')
        self.assertIsNone(self.attempt(now=20))

    def test_acceptance_is_not_delivery(self):
        attempt = self.attempt()
        self.engine.response(attempt['id'], FIXTURES['accepted'], now=1)
        self.assertEqual(self.engine.status(self.nid), 'PENDING')
        self.assertIsNone(self.attempt(now=2))

    def test_timeout_waits_then_fails_over_to_sms(self):
        attempt = self.attempt()
        self.engine.response(attempt['id'], FIXTURES['timeout'], now=1)
        self.assertIsNone(self.attempt(now=2))
        sms = self.attempt(now=3)
        self.assertEqual(sms['channel'], 'sms')
        self.engine.response(sms['id'], FIXTURES['delivered'], now=4)
        self.assertEqual(self.engine.status(self.nid), 'DELIVERED')

    def test_400_does_not_retry_bad_recipient(self):
        attempt = self.attempt()
        self.engine.response(attempt['id'], FIXTURES['invalid_recipient'], now=1)
        self.assertEqual(self.attempt(now=1)['channel'], 'sms')

    def test_500_is_ambiguous_and_waits_for_receipt(self):
        attempt = self.attempt()
        self.engine.response(attempt['id'], FIXTURES['unavailable'], now=1)
        self.assertIsNone(self.attempt(now=2))
        self.assertEqual(self.attempt(now=3)['channel'], 'sms')

    def test_preferred_channel_is_first_without_repetition(self):
        nid = self.engine.submit('transfer-2', 'user-1', preferred='email', now=0)
        attempt = self.engine.next_attempt(nid, now=0)
        self.assertEqual(attempt['channel'], 'email')
        self.engine.response(attempt['id'], FIXTURES['invalid_recipient'], now=1)
        self.assertEqual(self.engine.next_attempt(nid, now=1)['channel'], 'push')

    def test_replayed_event_has_no_extra_attempts(self):
        first = self.attempt()
        self.assertEqual(self.engine.submit('transfer-1', 'user-1', now=1), self.nid)
        self.assertIsNone(self.attempt(now=1))
        self.assertEqual(len(self.engine.attempts(self.nid)), 1)
        self.engine.receipt(first['id'])
        self.engine.submit('transfer-1', 'user-1', now=20)
        self.assertIsNone(self.attempt(now=20))

    def test_conflicting_replay_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.submit('transfer-1', 'user-1', preferred='sms', now=1)

    def test_same_event_for_different_users_is_separate(self):
        other = self.engine.submit('transfer-1', 'user-2', now=0)
        self.assertNotEqual(other, self.nid)

    def test_late_push_ack_stops_further_failover(self):
        push = self.attempt()
        sms = self.attempt(now=3)
        self.assertEqual(sms['channel'], 'sms')
        self.engine.receipt(push['id'])
        self.engine.response(sms['id'], FIXTURES['timeout'], now=4)
        self.assertEqual(self.engine.status(self.nid), 'DELIVERED')
        self.assertIsNone(self.attempt(now=30))

    def test_duplicate_receipt_is_idempotent(self):
        attempt = self.attempt()
        self.engine.receipt(attempt['id'])
        self.engine.receipt(attempt['id'])
        self.assertEqual(self.engine.status(self.nid), 'DELIVERED')
        self.assertEqual(len(self.engine.attempts(self.nid)), 1)

    def test_stale_response_cannot_regress_delivery(self):
        attempt = self.attempt()
        self.engine.receipt(attempt['id'])
        self.engine.response(attempt['id'], FIXTURES['unavailable'], now=2)
        self.assertEqual(self.engine.attempts(self.nid)[0]['state'], 'DELIVERED')

    def test_all_channels_exhausted_raise_escalation(self):
        for now, channel in [(0, 'push'), (3, 'sms'), (13, 'email')]:
            attempt = self.attempt(now=now)
            self.assertEqual(attempt['channel'], channel)
            self.engine.response(attempt['id'], FIXTURES['timeout'], now=now + 1)
        self.assertIsNone(self.attempt(now=43))
        self.assertEqual(self.engine.status(self.nid), 'ESCALATED')
        self.assertEqual(len(self.engine.attempts(self.nid)), 3)

    def test_deadline_escalates_without_new_send(self):
        self.assertIsNone(self.attempt(now=60))
        self.assertEqual(self.engine.status(self.nid), 'ESCALATED')
        self.assertEqual(self.engine.attempts(self.nid), [])

    def test_late_receipt_resolves_escalation(self):
        attempt = self.attempt()
        self.attempt(now=60)
        self.engine.receipt(attempt['id'])
        self.assertEqual(self.engine.status(self.nid), 'DELIVERED')

    def test_recovery_after_crash_preserves_intent_and_deadline(self):
        attempt = self.attempt()
        self.engine.close()
        self.engine = Engine(self.path)
        self.assertIsNone(self.attempt(now=2))
        self.assertEqual(self.engine.attempts(self.nid)[0]['id'], attempt['id'])
        self.assertEqual(self.attempt(now=3)['channel'], 'sms')

    def test_two_connections_do_not_claim_duplicate(self):
        second = Engine(self.path)
        try:
            self.attempt()
            self.assertIsNone(second.next_attempt(self.nid, now=1))
            self.assertEqual(len(second.attempts(self.nid)), 1)
        finally:
            second.close()

    def test_unknown_ids_are_rejected(self):
        for action in [lambda: self.engine.receipt('missing'),
                       lambda: self.engine.status('missing'),
                       lambda: self.engine.next_attempt('missing', now=0)]:
            with self.assertRaises(KeyError):
                action()

    def test_bad_input_is_rejected_before_persistence(self):
        for event, user, channel in [('', 'u', 'push'), ('x', '', 'push'),
                                      ('x', 'u', 'fax'), (True, 'u', 'push')]:
            with self.assertRaises(ValueError):
                self.engine.submit(event, user, preferred=channel, now=0)

    def test_invalid_response_does_not_change_state(self):
        attempt = self.attempt()
        with self.assertRaises(ValueError):
            self.engine.response(attempt['id'], {'status': 200}, now=1)
        self.assertEqual(self.engine.attempts(self.nid)[0]['state'], 'SENDING')


class SchemaTests(unittest.TestCase):
    def test_all_mock_fixtures_validate(self):
        for response in FIXTURES.values():
            validate_response(response)

    def test_schema_rejects_invalid_or_extra_fields(self):
        for value in [None, [], {}, {'timeout': False}, {'timeout': 1},
                      {'timeout': True, 'status': 200},
                      {'status': 200, 'receipt': 'read'},
                      {'status': 200, 'receipt': 'delivered', 'extra': 1},
                      {'status': True, 'error': 'bad'},
                      {'status': 500, 'error': ''}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_response(value)


if __name__ == '__main__':
    unittest.main()
