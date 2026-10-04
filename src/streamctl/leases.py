"""Persistent publisher credentials with a two-hour cumulative publishing budget."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import smtplib
import sqlite3
import ssl
import time
import urllib.parse
from email.message import EmailMessage

from argon2 import PasswordHasher

SESSION_SECONDS = 7200
NOTICE_SECONDS = 600
# The watchdog samples once per second. Only a fresh observation authorizes an
# automatic renewal; stale observations never turn idle time into usage.
METER_GRACE_SECONDS = 5
BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()


class Leases:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Pre-create securely before SQLite opens it, including on first boot.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS leases (
                    username TEXT PRIMARY KEY, policy_hash TEXT NOT NULL,
                    active_hash TEXT NOT NULL, started REAL NOT NULL, expires REAL NOT NULL,
                    pending_hash TEXT, pending_key TEXT, delivered INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, token_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY, observed REAL NOT NULL);
                INSERT OR IGNORE INTO clock VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS credential_vault (
                    username TEXT PRIMARY KEY, policy_hash TEXT NOT NULL,
                    publish_hash TEXT NOT NULL, publish_key TEXT NOT NULL,
                    read_hash TEXT NOT NULL, read_key TEXT NOT NULL, email TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mail_outbox (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, payload TEXT NOT NULL,
                    retry_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS control_nonces (
                    nonce TEXT PRIMARY KEY, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_history (
                    username TEXT PRIMARY KEY, last_login REAL NOT NULL
                );
            ''')
            # Preserve the remaining allowance from the old wall-clock model.
            # Historical connected time cannot be reconstructed; never revive
            # an already expired generation when upgrading an existing store.
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(leases)')}
            if 'used_seconds' not in columns:
                db.execute('ALTER TABLE leases ADD COLUMN used_seconds REAL NOT NULL DEFAULT 0')
                observed = self.now(db)
                db.execute('UPDATE leases SET used_seconds=MIN(?, MAX(0, ?-MAX(0,expires-?)))',
                           (SESSION_SECONDS, SESSION_SECONDS, observed))
            for name, declaration in (
                    ('meter_at', 'REAL NOT NULL DEFAULT 0'),
                    ('meter_boot', "TEXT NOT NULL DEFAULT ''"),
                    ('online_ids', "TEXT NOT NULL DEFAULT '[]'")):
                if name not in columns:
                    db.execute(f'ALTER TABLE leases ADD COLUMN {name} {declaration}')

    @staticmethod
    def remaining(row, meter_now=None):
        # This value is checkpointed by the watchdog, not derived from a wall
        # deadline. Project only the small interval since a fresh observation.
        elapsed = 0
        if row['meter_boot'] == BOOT_ID and json.loads(row['online_ids']):
            gap = (time.monotonic() if meter_now is None else meter_now) - row['meter_at']
            elapsed = min(max(0, gap), METER_GRACE_SECONDS)
        return max(0, SESSION_SECONDS - row['used_seconds'] - elapsed)

    @staticmethod
    def online(row, meter_now=None):
        gap = (time.monotonic() if meter_now is None else meter_now) - row['meter_at']
        return (row['meter_boot'] == BOOT_ID and bool(json.loads(row['online_ids']))
                and 0 <= gap <= METER_GRACE_SECONDS)

    @staticmethod
    def reset(db, username, policy_hash, active_hash, now):
        # The retained expires column is legacy storage only, never authority.
        db.execute('INSERT OR REPLACE INTO leases '
                   '(username,policy_hash,active_hash,started,expires) VALUES (?,?,?,?,0)',
                   (username, policy_hash, active_hash, now))

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def now(db, supplied=None):
        # A backwards wall-clock correction must never revive an expired key.
        now = max(time.time() if supplied is None else supplied,
                  db.execute('SELECT observed FROM clock WHERE id=1').fetchone()[0])
        db.execute('UPDATE clock SET observed=? WHERE id=1', (now,))
        return now

    def candidates(self, username, policy_hash, now=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.now(db, now)
            row = db.execute('SELECT * FROM leases WHERE username=?', (username,)).fetchone()
            if row is None or row['policy_hash'] != policy_hash:
                return [policy_hash]
            remaining = self.remaining(row)
            if remaining <= 0:
                db.execute("UPDATE leases SET used_seconds=?,online_ids='[]' WHERE username=?",
                           (SESSION_SECONDS, username))
            result = [row['active_hash']] if remaining > 0 else []
            if row['pending_hash'] and row['delivered']:
                result.append(row['pending_hash'])
            return result

    def admit(self, username, policy_hash, verified_hash, session_id, now=None):
        """CAS after password verification; admission alone consumes no usage."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.now(db, now)
            row = db.execute('SELECT * FROM leases WHERE username=?', (username,)).fetchone()
            if row is None or row['policy_hash'] != policy_hash:
                if verified_hash != policy_hash:
                    return False
                self.reset(db, username, policy_hash, verified_hash, now)
            elif verified_hash == row['pending_hash'] and row['delivered']:
                db.execute('UPDATE credential_vault SET publish_hash=?,publish_key=? '
                           'WHERE username=? AND policy_hash=?',
                           (verified_hash, row['pending_key'] or '', username, policy_hash))
                self.reset(db, username, policy_hash, verified_hash, now)
            elif verified_hash != row['active_hash']:
                return False
            elif self.remaining(row) <= 0:
                db.execute("UPDATE leases SET used_seconds=?,online_ids='[]' WHERE username=?",
                           (SESSION_SECONDS, username))
                return False
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?,?)',
                       (session_id, username, verified_hash))
            db.execute('INSERT INTO login_history VALUES (?,?) '
                       'ON CONFLICT(username) DO UPDATE SET '
                       'last_login=MAX(last_login,excluded.last_login)', (username, now))
            return True

    def invalid_sessions(self, publishers, policies, now=None, meter_now=None):
        """Checkpoint the union of real publishing intervals, then enforce usage."""
        meter_now = time.monotonic() if meter_now is None else meter_now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.now(db, now)
            rows = {r['username']: r for r in db.execute('SELECT * FROM leases')}
            sessions = {r['id']: r for r in db.execute('SELECT * FROM sessions')}
            publishing = [i for i in publishers if i.get('state') == 'publish']
            online = {}
            invalid = []
            for item in publishing:
                session = sessions.get(item['id'])
                row = rows.get(session['username']) if session else None
                if (row is None or policies.get(session['username']) != row['policy_hash']
                        or session['token_hash'] != row['active_hash']):
                    invalid.append(item)
                else:
                    online.setdefault(session['username'], set()).add(item['id'])
            for username, row in rows.items():
                previous = set(json.loads(row['online_ids']))
                current = online.get(username, set())
                elapsed = 0
                if previous and row['meter_boot'] == BOOT_ID:
                    gap = max(0, meter_now - row['meter_at'])
                    # A session still present across a watchdog restart has
                    # genuinely continued publishing. For disappeared sessions,
                    # only charge a bounded final polling interval.
                    elapsed = gap if previous & current else min(gap, METER_GRACE_SECONDS)
                used = min(SESSION_SECONDS, row['used_seconds'] + elapsed)
                if used >= SESSION_SECONDS:
                    invalid.extend(i for i in publishing if i['id'] in current)
                    current = set()
                db.execute('UPDATE leases SET used_seconds=?,meter_at=?,meter_boot=?,online_ids=? '
                           'WHERE username=?',
                           (used, meter_now, BOOT_ID, json.dumps(sorted(current)), username))
            # Keep newly authenticated IDs until MediaMTX has entered publish
            # state. Previously observed, disconnected IDs can be discarded.
            active = {i['id'] for i in publishing}
            for sid in sessions.keys() - active:
                row = rows.get(sessions[sid]['username'])
                if (row is None or sid in json.loads(row['online_ids'])
                        or row['used_seconds'] >= SESSION_SECONDS
                        or sessions[sid]['token_hash'] != row['active_hash']):
                    db.execute('DELETE FROM sessions WHERE id=?', (sid,))
            return invalid

    def statuses(self):
        with self.connection() as db:
            return [dict(username=r['username'], started=r['started'],
                         used_seconds=r['used_seconds'], remaining_seconds=self.remaining(r),
                         is_streaming=self.online(r), renewal_email_sent=bool(r['delivered']))
                    for r in db.execute('SELECT * FROM leases')]

    def notice_due(self, row, policy_hash, now):
        return (row is not None and row['policy_hash'] == policy_hash and not row['delivered']
                and self.online(row) and 0 < self.remaining(row) <= NOTICE_SECONDS
                and now >= row['retry_at'])

    def prepare_notice(self, username, policy_hash, now=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = self.now(db, now)
            row = db.execute('SELECT * FROM leases WHERE username=?', (username,)).fetchone()
            if not self.notice_due(row, policy_hash, now):
                return None
            generation = row['active_hash']
            key, hashed = row['pending_key'], row['pending_hash']
        if not key:
            key = secrets.token_urlsafe(24)
            hashed = 'argon2:' + PasswordHasher().hash(key)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM leases WHERE username=?', (username,)).fetchone()
            now = self.now(db, now)
            if not self.notice_due(current, policy_hash, now) or current['active_hash'] != generation:
                return None
            # Other workers reuse the persisted password after a failed send.
            if current['pending_key']:
                key, hashed = current['pending_key'], current['pending_hash']
            db.execute('UPDATE leases SET pending_key=?,pending_hash=?,retry_at=? WHERE username=?',
                       (key, hashed, now + 60, username))
            return dict(username=username, key=key, hash=hashed, generation=generation,
                        policy_hash=policy_hash, remaining_seconds=self.remaining(current))

    def delivered(self, notice):
        with self.connection() as db:
            # Keep the next secret in this private database until manual use:
            # the control interface must be able to report the effective key.
            db.execute('UPDATE leases SET delivered=1 WHERE username=? '
                       'AND policy_hash=? AND active_hash=? AND pending_hash=?',
                       (notice['username'], notice['policy_hash'], notice['generation'], notice['hash']))


def publisher_policies(config):
    return {u['user']: u['pass'] for u in config['authInternalUsers']
            if any(p.get('action') == 'publish' for p in u.get('permissions', []))}


def validate_mail(config):
    if not isinstance(config, dict):
        raise ValueError('SMTP 配置须为 JSON 对象')
    config = dict(config)
    for canonical, alias in (('host', 'smtp_host'), ('port', 'smtp_port')):
        if canonical in config and alias in config and config[canonical] != config[alias]:
            raise ValueError('SMTP 字段冲突: '+canonical+' / '+alias)
        if canonical not in config and alias in config:
            config[canonical] = config[alias]
    if 'smtp_ssl' in config:
        if type(config['smtp_ssl']) is not bool:
            raise ValueError('smtp_ssl 须为布尔值')
        security = 'ssl' if config['smtp_ssl'] else 'starttls'
        if 'security' in config and config['security'] != security:
            raise ValueError('security 与 smtp_ssl 冲突')
        config['security'] = security
    if config.get('security') not in ('ssl', 'starttls'):
        raise ValueError('SMTP security 须为 ssl 或 starttls')
    if (not isinstance(config.get('host'), str) or not config['host']
            or type(config.get('port')) is not int or not 1 <= config['port'] <= 65535):
        raise ValueError('非法 SMTP 主机或端口')
    recipients = config.get('recipients', {})
    if not isinstance(recipients, dict):
        raise ValueError('SMTP recipients 须为 用户名:邮箱 的映射')
    for address in [config.get('from', ''), *recipients.values()]:
        if (not isinstance(address, str) or not address or '@' not in address
                or any(c in address for c in '\r\n')):
            raise ValueError('非法邮件地址')
    if not recipients:
        raise ValueError('至少配置一个接收用户及邮箱')
    for name in ('username', 'password'):
        if not isinstance(config.get(name, ''), str):
            raise ValueError('非法 SMTP 凭据')
    return config


def send_message(config, message):
    config = validate_mail(config)
    context = ssl.create_default_context()
    if config['security'] == 'ssl':
        client = smtplib.SMTP_SSL(config['host'], config['port'], timeout=8, context=context)
    else:
        client = smtplib.SMTP(config['host'], config['port'], timeout=8)
    with client:
        if config['security'] == 'starttls':
            client.starttls(context=context)
        if config.get('username'):
            client.login(config['username'], config.get('password', ''))
        client.send_message(message)


def send_account_notice(config, username, publish_key, read_key, publish_base,
                        read_base, event, identifier, recipient=None, test_url=None):
    config = validate_mail(config)
    path = '/live/' + username
    publish_url = publish_base.rstrip('/') + path
    read_url = read_base.rstrip('/') + path
    if not read_url.startswith('rtsp://'):
        raise ValueError('观看通知必须使用 rtsp 地址')
    # Keep refresh notices identical, including older queued notifications.
    event = 'CialloChat用户创建' if event == 'CialloChat用户创建' else 'CialloChat密码刷新'
    message = EmailMessage()
    message['Subject'] = event
    message['From'] = config['from']
    message['To'] = recipient or config['recipients'][username]
    message['Message-ID'] = f'<{hashlib.sha256(identifier.encode()).hexdigest()}@ciallochat.local>'
    lines = [event, f'用户名：{username}',
             f'推流密码：{publish_key or "当前密码已过期，请刷新推流密码"}',
             f'观看密码：{read_key or "未保存，请联系管理员"}', '']
    if publish_key:
        query = urllib.parse.urlencode({'user': username, 'pass': publish_key})
        obs_url = publish_url + '?' + query
    else:
        obs_url = '当前密码已过期，请刷新推流密码'
    if read_key:
        player_url = read_url + '?read_key=' + urllib.parse.quote(read_key, safe='')
    else:
        player_url = '未保存，请联系管理员'
    lines += ['OBS推流URL：' + obs_url, '播放器输入URL：' + player_url]
    if test_url:
        if not test_url.startswith('rtsp://'): raise ValueError('测试频道必须使用 rtsp 地址')
        lines.append('测试频道URL：' + test_url)
    lines += ['', '推流密钥可用时长2小时']
    message.set_content('\n'.join(lines) + '\n')
    send_message(config, message)


def send_notice(config, notice, publish_base, read_base=None, test_url=None):
    if read_base is None:
        host = urllib.parse.urlsplit(publish_base).hostname
        host = '[' + host + ']' if ':' in host else host
        read_base = 'rtsp://' + host + ':8554'
    send_account_notice(config, notice['username'], notice['key'], notice.get('read_key'),
                        publish_base, read_base, 'CialloChat密码刷新', notice['hash'],
                        recipient=notice.get('email'), test_url=test_url)
