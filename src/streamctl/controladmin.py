"""Serialized account operations and a durable notification outbox."""
import json
import math
import re
import secrets
import time
import threading

from .accounts import (credentials, hash_password, matches, new_account, new_password,
                       timestamp, validate_username)
from .config import Store, public_urls
from .leases import Leases, SESSION_SECONDS, send_account_notice, validate_mail
from .service import Service, commit, recover


def validate_email(email):
    if (not isinstance(email, str) or len(email) > 254
            or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email)):
        raise ValueError('非法邮箱地址')
    return email


class Administration:
    def __init__(self, runtime=None):
        self.store = Store(runtime)
        self.leases = Leases(self.store.path/'leases/leases.sqlite3')
        self._traffic_reports = None
        self._traffic_init_lock = threading.Lock()

    def mail_config(self):
        path = self.store.path/'notifications/smtp.json'
        if path.stat().st_mode & 0o077:
            raise ValueError('SMTP 文件须为权限 600')
        return validate_mail(json.loads(path.read_text()))

    def register_credentials(self, account, push, pull, email, reset_timer=False):
        # Used for migration as well: never record an unverified handoff key.
        validate_email(email)
        if not matches(account['publish_key_hash'], push) or not matches(account['read_key_hash'], pull):
            raise ValueError('凭据与账号不一致')
        with self.leases.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO credential_vault VALUES (?,?,?,?,?,?,?)',
                       (account['username'], account['publish_key_hash'], account['publish_key_hash'],
                        push, account['read_key_hash'], pull, email))
            if reset_timer:
                now = self.leases.now(db)
                self.leases.reset(db, account['username'], account['publish_key_hash'],
                                  account['publish_key_hash'], now)

    def users(self, accounts):
        try:
            fallback = self.mail_config()['recipients']
        except (OSError, ValueError):
            fallback = {}
        with self.leases.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.leases.now(db)
            vault = {r['username']: dict(r) for r in db.execute('SELECT * FROM credential_vault')}
            leases = {r['username']: dict(r) for r in db.execute('SELECT * FROM leases')}
            logins = {r['username']: r['last_login'] for r in db.execute('SELECT * FROM login_history')}
        result = []
        for user in accounts['users']:
            record, lease = vault.get(user['username']), leases.get(user['username'])
            push, pull, remaining, state = None, None, None, 'unavailable'
            if record:
                if record['read_hash'] == user['read_key_hash']:
                    pull = record['read_key'] or None
                if record['policy_hash'] == user['publish_key_hash']:
                    if lease and lease['policy_hash'] == user['publish_key_hash']:
                        remaining = max(0, math.ceil(self.leases.remaining(lease)))
                        if remaining:
                            state = 'active'
                            if record['publish_hash'] == lease['active_hash']:
                                push = record['publish_key'] or None
                        elif lease['delivered'] and lease['pending_key']:
                            push, remaining, state = lease['pending_key'], SESSION_SECONDS, 'renewal_ready'
                        else:
                            state = 'expired'
                    else:
                        push, remaining, state = record['publish_key'] or None, SESSION_SECONDS, 'not_started'
            if not user['enabled']:
                push, remaining, state = None, 0, 'disabled'
            result.append(dict(username=user['username'],
                               email=record['email'] if record else fallback.get(user['username']),
                               push_password=push, push_seconds_remaining=remaining,
                               push_state=state, pull_password=pull,
                               last_login=logins.get(user['username'])))
        return result

    @property
    def traffic_reports(self):
        with self._traffic_init_lock:
            if self._traffic_reports is None:
                from .trafficreport import TrafficReports
                self._traffic_reports = TrafficReports(self)
            return self._traffic_reports

    def command(self, command):
        if command and command[0] == 'traffic':
            return self.traffic_reports.create(command)
        job = None
        with self.store.lock():
            recover(self.store)
            settings, accounts, control = self.store.load()
            if command and command[0] == 'info':
                if len(command) != 2:
                    raise ValueError('用法：info <user>')
                name = validate_username(command[1])
                account = next((u for u in accounts['users'] if u['username'] == name), None)
                if account is None:
                    raise ValueError('账号不存在')
                current = self.users({'users': [account]})[0]
                urls = credentials(settings, name, current['push_password'], current['pull_password'])
                return dict(ok=True, username=name, obs_publish_url=urls.get('publish_url'),
                            player_input_url=urls.get('read_url'), push_state=current['push_state'])
            if command == ['list']:
                users = self.users(accounts)
                streaming = {}
                default = None
                try:
                    service = Service(self.store, settings, control)
                    if service.running():
                        paths = service.api('paths/list?itemsPerPage=10000')['items']
                        streaming = {p['name']: p['ready'] for p in paths}
                    default = False
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    pass
                for user in users:
                    user['is_streaming'] = streaming.get('live/'+user['username'], default)
                return dict(ok=True, users=users)
            if (not command or command[0] not in ('add', 'del', 'refresh')
                    or len(command) != (2 if command[0] == 'del' else 3)):
                raise ValueError('支持 list、info <user>、del <user>、add <user> <email>、refresh <user> all|push|pull|time')
            action, name = command[:2]
            validate_username(name)
            user = next((u for u in accounts['users'] if u['username'] == name), None)
            service = Service(self.store, settings, control)
            if action == 'del':
                if user is None: raise ValueError('账号不存在')
                accounts['users'].remove(user)
                commit(self.store, accounts, revoke=[user['stream_path']], service=service,
                       revoke_states=('publish', 'read'))
                with self.leases.connection() as db:
                    for table in ('credential_vault', 'leases', 'sessions', 'mail_outbox', 'login_history'):
                        db.execute(f'DELETE FROM {table} WHERE username=?', (name,))
                return dict(ok=True, message=f'已删除用户 {name}。')
            self.mail_config()  # Validate SMTP before changing any account.
            if action == 'add':
                if user is not None: raise ValueError('用户名已存在')
                email = validate_email(command[2])
                push, pull = new_password(), new_password()
                user = new_account(name, push, pull)
                accounts['users'].append(user)
                commit(self.store, accounts, service=service)
                self.register_credentials(user, push, pull, email)
                event, scope = 'CialloChat用户创建', 'all'
            else:
                scope = command[2]
                if scope not in ('all', 'push', 'pull', 'time'): raise ValueError('刷新类型须为 all、push、pull 或 time')
                if user is None: raise ValueError('账号不存在')
                current = next(u for u in self.users(accounts) if u['username'] == name)
                email = validate_email(current['email'])
                if scope == 'time':
                    self.leases.refresh_time(name, user['publish_key_hash'])
                else:
                    # A push-only operation preserves the permanent read key, and
                    # a pull-only operation preserves the cumulative publishing allowance.
                    if scope in ('all', 'push'):
                        push = new_password()
                        user['publish_key_hash'] = hash_password(push)
                    else:
                        push = current['push_password']
                    if scope in ('all', 'pull'):
                        pull = new_password()
                        user['read_key_hash'] = hash_password(pull)
                    else:
                        pull = current['pull_password']
                    if scope == 'push' and pull is None:
                        raise ValueError('观看密码未存档，请先使用 refresh all 完成凭据迁移')
                    user['updated_at'] = timestamp()
                    states = ('publish', 'read') if scope == 'all' else (('publish',) if scope == 'push' else ('read',))
                    commit(self.store, accounts, revoke=[user['stream_path']], service=service, revoke_states=states)
                    if scope in ('all', 'push'):
                        self.register_credentials(user, push, pull, email, reset_timer=True)
                    else:
                        with self.leases.connection() as db:
                            # Do not change publisher generation or remaining time.
                            db.execute('UPDATE credential_vault SET read_hash=?,read_key=? WHERE username=?',
                                       (user['read_key_hash'], pull, name))
                            if db.execute('SELECT changes()').fetchone()[0] == 0:
                                db.execute('INSERT INTO credential_vault VALUES (?,?,?,?,?,?,?)',
                                           (name, user['publish_key_hash'], user['publish_key_hash'], '',
                                            user['read_key_hash'], pull, email))
                event = 'CialloChat密码刷新'
            with self.leases.connection() as db:
                record = db.execute('SELECT * FROM credential_vault WHERE username=?', (name,)).fetchone()
                job = secrets.token_hex(24)
                payload = dict(event=event, scope=scope, email=email,
                               policy_hash=user['publish_key_hash'], publish_hash=record['publish_hash'],
                               read_hash=user['read_key_hash'],
                               **public_urls(settings))
                # Superseded notifications are checked against current hashes
                # at delivery; keep independent push/pull changes in the queue.
                db.execute('INSERT INTO mail_outbox VALUES (?,?,?,0)', (job, name, json.dumps(payload)))
        sent = self.deliver_job(job)
        message = (f'用户 {name} 的推流密码使用时长已恢复为两小时。' if scope == 'time'
                   else f'用户 {name} 已' + ('创建' if action == 'add' else '刷新密码') + '。')
        return dict(ok=True, message=message,
                    email_status='sent' if sent else 'queued')

    def deliver_job(self, job):
        with self.store.lock():
            recover(self.store)
            settings, accounts, _ = self.store.load()
            with self.leases.connection() as db:
                row = db.execute('SELECT * FROM mail_outbox WHERE id=?', (job,)).fetchone()
                if not row: return True
                payload = json.loads(row['payload'])
                record = db.execute('SELECT * FROM credential_vault WHERE username=?', (row['username'],)).fetchone()
            user = next((u for u in accounts['users'] if u['username'] == row['username']), None)
            stale = user is None or record is None
            if not stale and payload['scope'] in ('all', 'push', 'time'):
                stale |= (user['publish_key_hash'] != payload['policy_hash']
                          or record['publish_hash'] != payload['publish_hash'])
            if not stale and payload['scope'] in ('all', 'pull'):
                stale |= user['read_key_hash'] != payload['read_hash']
            if stale:
                with self.leases.connection() as db:
                    db.execute('DELETE FROM mail_outbox WHERE id=?', (job,))
                return True
            with self.leases.connection() as db:
                db.execute('UPDATE mail_outbox SET retry_at=? WHERE id=?', (time.time()+60, job))
            try:
                config = self.mail_config()
                config['recipients'][row['username']] = payload['email']
                keys = next(u for u in self.users(accounts) if u['username'] == row['username'])
                # A queued message must follow the current public domain after migration.
                urls = public_urls(settings)
                send_account_notice(config, row['username'], keys['push_password'], keys['pull_password'],
                                    urls['publish_base'], urls['read_base'], payload['event'], job,
                                    payload['email'], test_url=urls['test_url'],
                                    reason='time' if payload['scope'] == 'time' else 'manual')
            except Exception:
                print('control notification failed; retry queued; credentials omitted', flush=True)
                return False
            with self.leases.connection() as db:
                db.execute('DELETE FROM mail_outbox WHERE id=?', (job,))
            return True

    def retry_notifications(self):
        with self.leases.connection() as db:
            jobs = [r['id'] for r in db.execute('SELECT id FROM mail_outbox WHERE retry_at<=?', (time.time(),))]
        for job in jobs:
            self.deliver_job(job)
