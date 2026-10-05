import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml

from streamctl.accounts import new_account
from streamctl.authserver import Policy
from streamctl.config import Store, atomic_write, dump, render, write_watchdog_config
from streamctl.leases import Leases, send_notice, validate_mail
from streamctl.watchdog import Watchdog

SID = '12345678-1234-1234-1234-123456789abc'
NEW_SID = '87654321-1234-1234-1234-123456789abc'


class PublisherLeases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.initialize()
        self.s, self.a, self.c = self.store.load()
        self.secret = 'publisher-secret-12345'
        self.a['users'] = [new_account('alice', self.secret)]
        self.policy_file = self.store.path/'mediamtx/mediamtx.yml'
        atomic_write(self.policy_file, render(self.s, self.a, self.c))
        write_watchdog_config(self.store, self.s, self.c)
        self.policy = Policy(self.policy_file)
        self.leases = self.policy.leases
        self.hashed = self.a['users'][0]['publish_key_hash']
        self.start = time.time()

    def request(self, value=None, sid=SID):
        return dict(action='publish', path='live/alice', protocol='rtmp',
                    user='alice', password=value or self.secret, query='',
                    ip='127.0.0.1', id=sid)

    def authorize_at(self, now, value=None, sid=SID, policy=None):
        with patch('streamctl.leases.time.time', return_value=now), \
                patch('streamctl.leases.time.monotonic', return_value=now):
            return (policy or self.policy).authorize(self.request(value, sid))

    def item(self, sid=SID):
        return dict(id=sid, path='live/alice', state='publish', inboundBytes=100,
                    kind='rtmps/conns')

    def test_wrong_password_never_starts_timer_and_missing_id_is_denied(self):
        self.assertFalse(self.authorize_at(self.start, 'incorrect-secret-123'))
        self.assertEqual(self.leases.statuses(), [])
        with self.leases.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM login_history').fetchone()[0], 0)
        request = self.request(); request.pop('id')
        self.assertFalse(self.policy.authorize(request))

    def sample(self, seconds, items=None, leases=None):
        when = self.start+seconds
        with patch('streamctl.leases.time.time', return_value=when), \
                patch('streamctl.leases.time.monotonic', return_value=when):
            return (leases or self.leases).invalid_sessions(
                [self.item()] if items is None else items, {'alice': self.hashed})

    def notice(self, seconds):
        when = self.start+seconds
        with patch('streamctl.leases.time.time', return_value=when), \
                patch('streamctl.leases.time.monotonic', return_value=when):
            return self.leases.prepare_notice('alice', self.hashed)

    def test_idle_authentication_offline_days_and_readers_do_not_consume_budget_or_send_email(self):
        self.assertTrue(self.authorize_at(self.start))
        self.sample(86400, [dict(self.item(), state='read')])
        self.assertIsNone(self.notice(86400))
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 0)
        self.assertEqual(self.leases.statuses()[0]['remaining_seconds'], 7200)
        self.assertTrue(self.authorize_at(self.start+86401, policy=Policy(self.policy_file)))

    def test_separate_sessions_reconnect_and_restart_keep_cumulative_usage(self):
        self.assertTrue(self.authorize_at(self.start))
        self.sample(0); self.sample(100)
        self.sample(101, [])
        self.sample(86400, [])
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 101)
        self.assertTrue(self.authorize_at(self.start+86401, sid=NEW_SID))
        restarted = Leases(self.leases.path)
        self.sample(86401, [self.item(NEW_SID)], restarted)
        self.sample(86601, [self.item(NEW_SID)], restarted)
        self.assertEqual(restarted.statuses()[0]['used_seconds'], 301)

    def test_two_simultaneous_publishers_count_elapsed_time_once(self):
        self.assertTrue(self.authorize_at(self.start))
        self.assertTrue(self.authorize_at(self.start, sid=NEW_SID))
        self.sample(0, [self.item(), self.item(NEW_SID)])
        self.sample(120, [self.item(), self.item(NEW_SID)])
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 120)

    def test_watchdog_kicks_expired_live_connection_without_waiting_for_reauth(self):
        self.assertTrue(self.authorize_at(self.start))
        self.sample(0)
        watchdog = Watchdog(self.store.path/'watchdog/config.json')
        calls = []
        def api(config, endpoint, method='GET'):
            calls.append((endpoint, method))
            return {'items': [self.item()]} if endpoint.startswith('rtmps/') else {'items': []}
        watchdog.api = api
        with patch('streamctl.leases.time.time', return_value=self.start+86400), \
                patch('streamctl.leases.time.monotonic', return_value=self.start+7200):
            watchdog.tick()
        self.assertIn(('rtmps/conns/kick/'+SID, 'POST'), calls)
        self.assertFalse(self.authorize_at(self.start+100000))
        self.assertFalse(self.authorize_at(self.start-100000, policy=Policy(self.policy_file)))

    def test_usage_uses_monotonic_clock_and_does_not_follow_calendar_adjustments(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0)
        self.leases.invalid_sessions([self.item()], {'alice': self.hashed},
                                     now=self.start+86400, meter_now=self.start+60)
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 60)
        self.leases.invalid_sessions([self.item()], {'alice': self.hashed},
                                     now=self.start-86400, meter_now=self.start+120)
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 120)

    def test_machine_reboot_does_not_charge_offline_time_or_reset_used_budget(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0); self.sample(300)
        with patch('streamctl.leases.BOOT_ID', 'new-machine-boot'):
            self.leases.invalid_sessions([], {'alice': self.hashed},
                                         now=self.start+86400, meter_now=10)
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 300)

    def test_stale_last_observation_cannot_trigger_offline_email_or_charge_hours(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0); self.sample(6600)
        self.assertIsNone(self.notice(86400))
        self.sample(86400, [])
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 6605)
        self.assertIsNone(self.notice(86400))

    def test_exhaustion_seen_during_auth_is_persisted_and_cannot_revive_on_reboot(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0); self.sample(7199)
        self.assertFalse(self.authorize_at(self.start+7200))
        with patch('streamctl.leases.BOOT_ID', 'new-machine-boot'):
            self.assertEqual(self.leases.candidates('alice', self.hashed), [])
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 7200)

    def test_renewal_is_stable_on_retry_and_requires_delivery_and_manual_new_key(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0); self.sample(6599)
        self.assertIsNone(self.notice(6599))
        self.sample(6600); first = self.notice(6600)
        self.sample(6610); self.assertIsNone(self.notice(6610))
        self.sample(6660); retry = self.notice(6660)
        self.assertEqual(first['key'], retry['key'])
        self.assertFalse(self.authorize_at(self.start+6660, first['key'], NEW_SID))
        self.assertTrue(self.authorize_at(self.start+6660))
        self.leases.delivered(first)
        self.assertTrue(self.authorize_at(self.start+6700, first['key'], NEW_SID))
        self.assertFalse(self.authorize_at(self.start+6701))
        invalid = self.sample(6701, [self.item(), self.item(NEW_SID)])
        self.assertEqual([i['id'] for i in invalid], [SID])
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 0)
        self.assertIsNone(self.notice(6701))
        # Leaving the renewed password unused/idle does not generate more mail.
        self.sample(6702, []); self.sample(1000000, [])
        self.assertIsNone(self.notice(1000000))

    def test_notice_generation_rechecks_online_state_before_persisting_secret(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0); self.sample(6600)
        original = __import__('streamctl.leases', fromlist=['PasswordHasher']).PasswordHasher
        def disconnect_during_hash():
            self.sample(6601, [])
            return original()
        with patch('streamctl.leases.PasswordHasher', side_effect=disconnect_during_hash):
            self.assertIsNone(self.notice(6600))
        with self.leases.connection() as db:
            self.assertIsNone(db.execute('SELECT pending_key FROM leases').fetchone()[0])

    def test_reset_disable_and_pending_renewal_remain_scoped_to_current_policy(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0); self.sample(6600)
        notice = self.notice(6600); self.leases.delivered(notice)
        self.a['users'][0]['enabled'] = False
        atomic_write(self.policy_file, render(self.s, self.a, self.c))
        self.assertFalse(self.authorize_at(self.start+6601, notice['key'], NEW_SID))
        self.a['users'][0] = new_account('alice', 'reset-publisher-secret-123')
        atomic_write(self.policy_file, render(self.s, self.a, self.c))
        self.assertFalse(self.authorize_at(self.start+6602, notice['key'], NEW_SID))
        self.assertTrue(self.authorize_at(self.start+6603, 'reset-publisher-secret-123', NEW_SID))
        self.assertEqual(self.leases.statuses()[0]['used_seconds'], 0)

    def test_failed_email_pauses_retries_when_offline_and_never_extends_budget(self):
        self.assertTrue(self.authorize_at(self.start)); self.sample(0); self.sample(6600)
        config = dict(host='smtp.example.com', port=465, security='ssl',
                      username='sender@example.com', password='secret',
                      **{'from': 'sender@example.com', 'recipients': {'alice': 'a@example.com'}})
        atomic_write(self.store.path/'notifications/smtp.json', dump(config))
        worker = Watchdog(self.store.path/'watchdog/config.json')
        with patch('streamctl.leases.time.time', return_value=self.start+6600), \
                patch('streamctl.leases.time.monotonic', return_value=self.start+6600), \
                patch('streamctl.watchdog.send_notice', side_effect=OSError('smtp failed')):
            with self.assertRaises(OSError): worker.notifications()
        self.assertFalse(self.leases.statuses()[0]['renewal_email_sent'])
        self.sample(6601, []); self.assertIsNone(self.notice(86400))
        self.assertTrue(self.authorize_at(self.start+86401, sid=NEW_SID))
        self.sample(86401, [self.item(NEW_SID)])
        retried = self.notice(86401); self.assertIsNotNone(retried)
        self.sample(87000, [self.item(NEW_SID)])
        self.assertFalse(self.authorize_at(self.start+87000, sid=NEW_SID))
        self.assertEqual(self.leases.path.stat().st_mode & 0o777, 0o600)

    def test_old_schema_migration_preserves_remaining_and_permanent_expiry(self):
        path = Path(self.tmp.name)/'old.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE leases (username TEXT PRIMARY KEY, policy_hash TEXT NOT NULL, '
                       'active_hash TEXT NOT NULL, started REAL NOT NULL, expires REAL NOT NULL, '
                       'pending_hash TEXT, pending_key TEXT, delivered INTEGER NOT NULL DEFAULT 0, '
                       'retry_at REAL NOT NULL DEFAULT 0)')
            for name, remaining in [('live', 3600), ('expired', -10)]:
                db.execute('INSERT INTO leases (username,policy_hash,active_hash,started,expires) '
                           'VALUES (?,?,?,?,?)', (name, self.hashed, self.hashed,
                                                 self.start-3600, self.start+remaining))
        with patch('streamctl.leases.time.time', return_value=self.start):
            migrated = Leases(path)
        rows = {r['username']: r for r in migrated.statuses()}
        self.assertEqual(rows['live']['remaining_seconds'], 3600)
        self.assertEqual(rows['expired']['remaining_seconds'], 0)
        self.assertEqual(migrated.candidates('expired', self.hashed), [])
        self.assertEqual(Leases(path).statuses(), migrated.statuses())

    def test_failed_credentials_have_bounded_ip_attempts(self):
        for _ in range(5):
            self.assertFalse(self.policy.authorize(self.request('incorrect-secret-123')))
        with patch('streamctl.authserver.PasswordHasher.verify') as verify:
            self.assertFalse(self.policy.authorize(self.request()))
            verify.assert_not_called()

    def test_cached_valid_credentials_survive_bad_guesses_from_same_ip(self):
        self.assertTrue(self.policy.authorize(self.request()))
        for _ in range(6):
            self.assertFalse(self.policy.authorize(self.request('incorrect-secret-123')))
        self.assertTrue(self.policy.authorize(self.request()))

    def test_mail_requires_tls_and_valid_recipients(self):
        config = dict(host='smtp.example.com', port=465, security='ssl',
                      **{'from': 's@example.com', 'recipients': {'alice': 'a@example.com'}})
        self.assertEqual(validate_mail(config), config)
        for changes in ({'security': 'none'}, {'port': True},
                        {'from': 's@example.com\r\nBcc: stolen@example.com'}, {'recipients': {}}):
            with self.assertRaises(ValueError): validate_mail(dict(config, **changes))

    def test_smtp_aliases_and_pop3_fields_do_not_change_sending_transport(self):
        config = dict(smtp_host='smtp.example.com', smtp_port=465, smtp_ssl=True,
                      pop3_host='pop.example.com', pop3_port=995,
                      **{'from': 's@example.com', 'recipients': {'cc': 'a@example.com'}})
        normalized = validate_mail(config)
        self.assertEqual((normalized['host'], normalized['port'], normalized['security']),
                         ('smtp.example.com', 465, 'ssl'))
        self.assertEqual(validate_mail(dict(config, smtp_ssl=False, smtp_port=587))['security'], 'starttls')
        for changes in ({'smtp_ssl': 'false'}, {'host': 'other.example.com'}, {'security': 'starttls'}):
            with self.assertRaises(ValueError): validate_mail(dict(config, **changes))

    def test_renewal_message_uses_tls_correct_recipient_and_encoded_obs_url(self):
        config = dict(smtp_host='smtp.example.com', smtp_port=465, smtp_ssl=True,
                      username='s@example.com', password='smtp-authorization-code',
                      **{'from': 's@example.com', 'recipients': {'cc': 'r@example.com'}})
        notice = dict(username='cc', key='new-key-with&special#characters', hash='fingerprint',
                      expires=self.start+7200)
        with patch('streamctl.leases.smtplib.SMTP_SSL') as smtp:
            send_notice(config, notice, 'rtmps://stream.example.com:1936')
            client = smtp.return_value
            client.login.assert_called_once_with('s@example.com', 'smtp-authorization-code')
            message = client.send_message.call_args.args[0]
            self.assertEqual(message['To'], 'r@example.com')
            self.assertEqual(message.get_content().splitlines()[:3], ['CialloChat密码刷新',
                             '当前密钥接近使用上限，自动刷新。', '用户名：cc'])
            self.assertIn('pass=new-key-with%26special%23characters', message.get_content())
            self.assertNotIn('smtp-authorization-code', message.get_content())


if __name__ == '__main__':
    unittest.main()
