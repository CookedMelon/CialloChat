import json
import hashlib
import importlib.util
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from streamctl.accounts import matches
from streamctl.config import Store, atomic_write, dump
from streamctl.controladmin import Administration
from streamctl.controlclient import call
from streamctl.controlprotocol import encode, load_config, receive, sign_request, verify_request
from streamctl.controlserver import ControlServer
from streamctl.leases import send_account_notice, send_notice

PASSWORD = 'a-long-control-password-at-least-32-characters'
SID = '12345678-1234-1234-1234-123456789abc'


class ControlOperations(unittest.TestCase):
    def test_queued_notification_uses_current_domain_after_migration(self):
        self.send.side_effect = OSError('mail offline')
        self.admin.command(['add','alice','alice@example.com'])
        settings = self.store.read('settings.json')
        settings.update(service_backend='systemd',rtsp_buffer_ms=1000,mode='production',
                        hostname='chat.v50to.cc',test_video_enabled=True)
        atomic_write(self.store.path/'settings.json',dump(settings))
        self.send.side_effect = None
        with self.admin.leases.connection() as db: db.execute('UPDATE mail_outbox SET retry_at=0')
        self.admin.retry_notifications()
        self.assertEqual(self.send.call_args.args[4], 'rtmps://chat.v50to.cc:1936')
        self.assertEqual(self.send.call_args.args[5], 'rtsp://chat.v50to.cc:8554')
        self.assertEqual(self.send.call_args.kwargs['test_url'], 'rtsp://chat.v50to.cc:8554/test')

    def test_creation_installs_domain_test_url_in_notification_payload(self):
        settings = self.store.read('settings.json')
        settings.update(service_backend='systemd', rtsp_buffer_ms=1000, mode='production',
                        hostname='chat.v50to.cc', test_video_enabled=True)
        atomic_write(self.store.path/'settings.json', dump(settings))
        self.add()
        args = self.send.call_args
        self.assertEqual(args.args[4], 'rtmps://chat.v50to.cc:1936')
        self.assertEqual(args.args[5], 'rtsp://chat.v50to.cc:8554')
        self.assertEqual(args.kwargs['test_url'], 'rtsp://chat.v50to.cc:8554/test')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name); self.store.initialize()
        self.admin = Administration(self.tmp.name)
        atomic_write(self.store.path/'notifications/smtp.json', dump(dict(
            host='smtp.example.com', port=465, security='ssl',
            **{'from': 'sender@example.com', 'recipients': {'cc': 'cc@example.com'}})))
        self.running = patch('streamctl.service.Service.running', return_value=False)
        self.running.start(); self.addCleanup(self.running.stop)
        self.mail = patch('streamctl.controladmin.send_account_notice')
        self.send = self.mail.start(); self.addCleanup(self.mail.stop)

    def add(self):
        result = self.admin.command(['add', 'alice', 'alice@example.com'])
        self.assertEqual(result['email_status'], 'sent')
        return self.admin.command(['list'])['users'][0]

    def test_creation_listing_and_deletion_preserve_hash_only_account_storage(self):
        row = self.add()
        self.assertEqual(row['email'], 'alice@example.com')
        self.assertEqual(row['push_state'], 'not_started')
        self.assertIsNone(row['last_login'])
        self.assertFalse(row['is_streaming'])
        self.assertEqual(row['push_seconds_remaining'], 7200)
        self.assertNotEqual(row['push_password'], row['pull_password'])
        account = self.store.load()[1]['users'][0]
        self.assertTrue(matches(account['publish_key_hash'], row['push_password']))
        self.assertTrue(matches(account['read_key_hash'], row['pull_password']))
        self.assertNotIn(row['push_password'], (self.store.path/'accounts.json').read_text())
        args = self.send.call_args.args
        self.assertEqual(args[6], 'CialloChat用户创建')
        self.assertEqual(args[8], row['email'])
        self.assertEqual(self.admin.leases.path.stat().st_mode & 0o777, 0o600)
        self.admin.command(['del', 'alice'])
        self.assertEqual(self.admin.command(['list'])['users'], [])
        with self.admin.leases.connection() as db:
            for table in ('credential_vault', 'leases', 'sessions', 'mail_outbox', 'login_history'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_recent_login_survives_password_refresh_restart_and_resets_for_recreated_user(self):
        self.add(); now = time.time()
        hashed = self.store.load()[1]['users'][0]['publish_key_hash']
        self.assertTrue(self.admin.leases.admit('alice', hashed, hashed, SID, now))
        self.assertEqual(self.admin.command(['list'])['users'][0]['last_login'], now)
        for kind in ('push', 'pull', 'all'):
            self.admin.command(['refresh', 'alice', kind])
            self.assertEqual(Administration(self.tmp.name).command(['list'])['users'][0]['last_login'], now)
        new_hash = self.store.load()[1]['users'][0]['publish_key_hash']
        self.assertTrue(self.admin.leases.admit('alice', new_hash, new_hash, SID, now+60))
        self.assertEqual(self.admin.command(['list'])['users'][0]['last_login'], now+60)
        self.admin.command(['del', 'alice'])
        self.assertIsNone(self.add()['last_login'])

    def test_refresh_push_resets_usage_pull_preserves_usage_and_all_rotates_both(self):
        before = self.add()
        now = time.time()
        account = self.store.load()[1]['users'][0]
        self.assertTrue(self.admin.leases.admit('alice', account['publish_key_hash'],
                                              account['publish_key_hash'], SID, now))
        item = dict(id=SID, state='publish')
        policy = {'alice': account['publish_key_hash']}
        self.admin.leases.invalid_sessions([item], policy, now=now, meter_now=now)
        self.admin.leases.invalid_sessions([item], policy, now=now+100, meter_now=now+100)
        with patch('streamctl.leases.time.time', return_value=now+100), \
                patch('streamctl.leases.time.monotonic', return_value=now+100):
            self.admin.command(['refresh', 'alice', 'pull'])
            pulled = self.admin.command(['list'])['users'][0]
        self.assertEqual(pulled['push_password'], before['push_password'])
        self.assertNotEqual(pulled['pull_password'], before['pull_password'])
        self.assertEqual(pulled['push_seconds_remaining'], 7100)
        with patch('streamctl.leases.time.time', return_value=now+200):
            self.admin.command(['refresh', 'alice', 'push'])
            pushed = self.admin.command(['list'])['users'][0]
        self.assertNotEqual(pushed['push_password'], pulled['push_password'])
        self.assertEqual(pushed['pull_password'], pulled['pull_password'])
        self.assertEqual(pushed['push_seconds_remaining'], 7200)
        with patch('streamctl.leases.time.time', return_value=now+86400):
            self.assertEqual(self.admin.command(['list'])['users'][0]['push_seconds_remaining'], 7200)
            self.assertIsNone(self.admin.leases.prepare_notice('alice',
                              self.store.load()[1]['users'][0]['publish_key_hash']))
        self.admin.command(['refresh', 'alice', 'all'])
        final = self.admin.command(['list'])['users'][0]
        self.assertNotEqual(final['push_password'], pushed['push_password'])
        self.assertNotEqual(final['pull_password'], pushed['pull_password'])
        self.assertEqual(self.send.call_count, 4)

    def test_refresh_time_preserves_credentials_active_session_and_metering(self):
        before = self.add(); accounts_before = self.store.read('accounts.json'); now = time.time()
        hashed = accounts_before['users'][0]['publish_key_hash']
        self.assertTrue(self.admin.leases.admit('alice', hashed, hashed, SID, now))
        item = dict(id=SID,state='publish'); policy = {'alice':hashed}
        self.admin.leases.invalid_sessions([item],policy,now=now,meter_now=now)
        self.admin.leases.invalid_sessions([item],policy,now=now+6600,meter_now=now+6600)
        with patch('streamctl.leases.time.monotonic',return_value=now+6600):
            notice = self.admin.leases.prepare_notice('alice',hashed,now+6600)
        self.admin.leases.delivered(notice)
        with patch('streamctl.leases.time.time',return_value=now+6601), \
                patch('streamctl.leases.time.monotonic',return_value=now+6601), \
                patch('streamctl.controladmin.commit') as commit:
            result = self.admin.command(['refresh','alice','time'])
            after = self.admin.command(['list'])['users'][0]
            commit.assert_not_called()
        self.assertEqual(result['email_status'],'sent')
        self.assertEqual(after['push_password'],before['push_password'])
        self.assertEqual(after['pull_password'],before['pull_password'])
        self.assertEqual(after['push_seconds_remaining'],7200)
        self.assertEqual(self.store.read('accounts.json'),accounts_before)
        self.assertEqual(self.send.call_args.kwargs['reason'],'time')
        with self.admin.leases.connection() as db:
            self.assertIsNotNone(db.execute('SELECT * FROM sessions WHERE id=?',(SID,)).fetchone())
            self.assertIsNone(db.execute('SELECT pending_hash FROM leases').fetchone()[0])
        self.assertEqual(self.admin.leases.invalid_sessions([item],policy,now=now+6611,meter_now=now+6611),[])
        with patch('streamctl.leases.time.monotonic',return_value=now+6611):
            self.assertEqual(self.admin.leases.statuses()[0]['remaining_seconds'],7190)
        self.assertFalse(self.admin.leases.admit('alice',hashed,notice['hash'],'old-pending',now+6611))

    def test_refresh_time_restores_expired_key_or_preserves_current_renewal_key(self):
        before=self.add(); hashed=self.store.read('accounts.json')['users'][0]['publish_key_hash']
        for delivered in (False,True):
            with self.subTest(delivered=delivered):
                now=time.time()
                self.admin.leases.admit('alice',hashed,hashed,SID,now)
                self.admin.leases.invalid_sessions([dict(id=SID,state='publish')],{'alice':hashed},now=now,meter_now=now)
                self.admin.leases.invalid_sessions([dict(id=SID,state='publish')],{'alice':hashed},now=now+6600,meter_now=now+6600)
                with patch('streamctl.leases.time.monotonic',return_value=now+6600):
                    notice=self.admin.leases.prepare_notice('alice',hashed,now+6600)
                if delivered:self.admin.leases.delivered(notice)
                self.admin.leases.invalid_sessions([dict(id=SID,state='publish')],{'alice':hashed},now=now+7200,meter_now=now+7200)
                with patch('streamctl.leases.time.monotonic',return_value=now+7200):
                    self.admin.command(['refresh','alice','time'])
                    current=self.admin.command(['list'])['users'][0]
                expected=notice['key'] if delivered else before['push_password']
                self.assertEqual(current['push_password'],expected)
                self.assertEqual(current['pull_password'],before['pull_password'])
                self.assertEqual(current['push_seconds_remaining'],7200)
                self.assertTrue(self.admin.leases.admit('alice',hashed,notice['hash'] if delivered else hashed,SID))

    def test_refresh_time_mail_retry_is_durable_and_passwords_stay_fixed(self):
        before=self.add()
        self.send.side_effect=OSError('offline')
        result=self.admin.command(['refresh','alice','time'])
        self.assertEqual(result['email_status'],'queued')
        self.send.side_effect=None
        with self.admin.leases.connection() as db:db.execute('UPDATE mail_outbox SET retry_at=0')
        Administration(self.tmp.name).retry_notifications()
        after=self.admin.command(['list'])['users'][0]
        self.assertEqual((after['push_password'],after['pull_password']),
                         (before['push_password'],before['pull_password']))
        self.assertEqual(self.send.call_args.kwargs['reason'],'time')

    def test_manual_and_automatic_notices_explain_the_trigger_before_username(self):
        config=self.admin.mail_config()
        for reason,text in [('manual','密码已被管理员更新。'),
                            ('automatic','当前密钥接近使用上限，自动刷新。'),
                            ('time','推流密钥使用时长已被管理员重置为两小时。')]:
            with self.subTest(reason=reason), patch('streamctl.leases.send_message') as send:
                if reason=='automatic':
                    send_notice(config,dict(username='cc',key='new-push-key',read_key='pull-key',hash='id'),
                                'rtmps://chat.v50to.cc:1936','rtsp://chat.v50to.cc:8554')
                else:
                    send_account_notice(config,'cc','push-key','pull-key','rtmps://chat.v50to.cc:1936',
                                        'rtsp://chat.v50to.cc:8554','CialloChat密码刷新','id',reason=reason)
                body=send.call_args.args[1].get_content()
                self.assertEqual(body.splitlines()[:3],['CialloChat密码刷新',text,'用户名：cc'])
                self.assertIn('OBS推流URL：',body); self.assertIn('播放器输入URL：',body)

    def test_list_tracks_automatic_renewal_after_expiry_and_manual_promotion(self):
        before = self.add(); now = time.time()
        hashed = self.store.load()[1]['users'][0]['publish_key_hash']
        self.admin.leases.admit('alice', hashed, hashed, SID, now)
        item = dict(id=SID, state='publish'); policy = {'alice': hashed}
        self.admin.leases.invalid_sessions([item], policy, now=now, meter_now=now)
        self.admin.leases.invalid_sessions([item], policy, now=now+6600, meter_now=now+6600)
        with patch('streamctl.leases.time.monotonic', return_value=now+6600):
            notice = self.admin.leases.prepare_notice('alice', hashed, now+6600)
        self.admin.leases.delivered(notice)
        self.admin.leases.invalid_sessions([item], policy, now=now+7200, meter_now=now+7200)
        with patch('streamctl.leases.time.time', return_value=now+7200):
            row = self.admin.command(['list'])['users'][0]
            self.assertEqual(row['push_state'], 'renewal_ready')
            self.assertEqual(row['push_password'], notice['key'])
            self.assertTrue(self.admin.leases.admit('alice', hashed, notice['hash'], SID))
            row = Administration(self.tmp.name).command(['list'])['users'][0]
            self.assertEqual(row['push_state'], 'active')
            self.assertEqual(row['push_password'], notice['key'])
            self.assertEqual(row['pull_password'], before['pull_password'])
            self.admin.command(['refresh', 'alice', 'pull'])
            self.assertEqual(self.admin.command(['list'])['users'][0]['push_password'], notice['key'])

    def test_email_failure_is_durable_and_retried_without_changing_credentials(self):
        self.send.side_effect = OSError('mail offline')
        result = self.admin.command(['add', 'alice', 'alice@example.com'])
        self.assertEqual(result['email_status'], 'queued')
        original = self.admin.command(['list'])['users'][0]
        with self.admin.leases.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM mail_outbox').fetchone()[0], 1)
            db.execute('UPDATE mail_outbox SET retry_at=0')
        self.send.side_effect = None
        Administration(self.tmp.name).retry_notifications()
        self.assertEqual(self.admin.command(['list'])['users'][0], original)
        with self.admin.leases.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM mail_outbox').fetchone()[0], 0)

    def test_invalid_commands_and_duplicate_users_do_not_modify_accounts(self):
        self.add(); before = self.store.load()[1]
        for command in (['add','alice','new@example.com'], ['add','bob','invalid'],
                        ['add','../bad','a@example.com'], ['del','missing'],
                        ['refresh','alice','invalid'], ['arbitrary-shell','alice']):
            with self.assertRaises(ValueError): self.admin.command(command)
            self.assertEqual(self.store.load()[1], before)

    def test_all_notification_fields_use_rtsp_and_configured_host(self):
        config = self.admin.mail_config()
        with patch('streamctl.leases.smtplib.SMTP_SSL') as smtp:
            send_account_notice(config, 'alice', 'push-password-123', 'pull-password-123',
                                'rtmps://chat.v50to.cc:1936', 'rtsp://chat.v50to.cc:8554',
                                'CialloChat用户创建', 'job', 'alice@example.com',
                                test_url='rtsp://chat.v50to.cc:8554/test')
            message = smtp.return_value.send_message.call_args.args[0]
            body = message.get_content()
            for text in ('CialloChat用户创建', '用户名：alice', '推流密码：push-password-123',
                         'OBS推流URL：rtmps://chat.v50to.cc:1936/live/alice?user=alice&pass=push-password-123',
                         '观看密码：pull-password-123',
                         '播放器输入URL：rtsp://chat.v50to.cc:8554/live/alice?read_key=pull-password-123',
                         '测试频道URL：rtsp://chat.v50to.cc:8554/test',
                         '推流密钥可用时长2小时'):
                self.assertIn(text, body)
            self.assertEqual(message['To'], 'alice@example.com')
            self.assertEqual(len([line for line in body.splitlines() if line.strip()]), 8)
            self.assertNotIn('120.55.', body)


class ControlTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name); self.store.initialize()
        cert, key = self.store.path/'certs/server.crt', self.store.path/'certs/server.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
        self.config = self.store.path/'control-server.json'
        atomic_write(self.config, dump(dict(password=PASSWORD)))
        self.admin = Administration(self.tmp.name)
        self.server = ControlServer(('127.0.0.1', 0), self.config, self.admin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.stop)
        self.client = dict(host='127.0.0.1', port=self.server.server_address[1],
                           password=PASSWORD, ca_file=str(cert))

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def exchange(self, request):
        context = ssl.create_default_context(cafile=self.client['ca_file'])
        with socket.create_connection((self.client['host'], self.client['port']), timeout=3) as sock:
            with context.wrap_socket(sock, server_hostname='127.0.0.1') as tls:
                tls.settimeout(3); tls.sendall(encode(request)+b'\n')
                return tls.recv(16384)

    def test_invalid_password_and_stale_request_receive_no_application_response(self):
        with patch.object(self.admin, 'command') as command:
            self.assertEqual(self.exchange(sign_request('wrong-password', ['list'])), b'')
            self.assertEqual(self.exchange(sign_request(PASSWORD, ['list'], int(time.time())-70)), b'')
            command.assert_not_called()

    def test_valid_tls_request_and_replay_protection_survive_database_reopen(self):
        with patch('streamctl.service.Service.running', return_value=False):
            self.assertEqual(call(self.client, ['list'])['users'], [])
            request = sign_request(PASSWORD, ['list'])
            self.assertTrue(json.loads(self.exchange(request))['ok'])
            self.server.administration = Administration(self.tmp.name)
            self.assertEqual(self.exchange(request), b'')

    def test_oversized_frame_is_silent_and_certificate_verification_is_required(self):
        with patch.object(self.admin, 'command') as command:
            self.assertEqual(self.exchange({'padding': 'x'*9000}), b'')
            command.assert_not_called()
        with self.assertRaises(ssl.SSLCertVerificationError):
            call(dict(self.client, ca_file=None), ['list'])

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is client-only')
    def test_authenticated_bounded_chart_upload_and_email(self):
        from streamctl.trafficchart import render
        atomic_write(self.store.path/'notifications/smtp.json', dump(dict(
            host='smtp.example.com',port=465,security='ssl',
            **{'from':'sender@example.com','recipients':{'cc':'cc@example.com'},
               'admin_recipient':'admin@example.com'})))
        report = call(self.client, ['traffic'])
        image = render(report['report'], self.store.path/'reports/test.png').read_bytes()
        command = ['traffic-mail',report['report_id'],f'{len(image)}:{hashlib.sha256(image).hexdigest()}']
        with patch('streamctl.trafficreport.send_message') as send:
            self.assertEqual(call(self.client, command, image)['email_status'], 'sent')
            self.assertEqual(send.call_args.args[1]['To'], 'admin@example.com')
            self.assertEqual(call(self.client, command, image)['email_status'], 'sent')
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.exchange(sign_request('wrong-password', command)), b'')
            bad = ['traffic-mail', report['report_id'], '262145:'+'a'*64]
            with self.assertRaises(ValueError): call(self.client, bad, image)

    def test_private_configuration_and_request_shape_are_validated(self):
        self.assertEqual(load_config(self.config)['password'], PASSWORD)
        self.config.chmod(0o644)
        with self.assertRaises(ValueError): load_config(self.config)
        request = sign_request(PASSWORD, ['list'])
        request['command'] = ['del', 'cc']
        self.assertFalse(verify_request(PASSWORD, request))


if __name__ == '__main__':
    unittest.main()
