"""Durable per-source-IP allowances for the public test channel; stdlib only."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hmac
import ipaddress
from pathlib import Path
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo

PATH_PATTERN = r'~^test/[a-f0-9]{32}$'


class TestDenied(ConnectionError):
    pass


@dataclass(frozen=True)
class Grant:
    ip: str
    owner: str
    token: str
    publisher_password: str
    deadline: float
    remaining: float


def source_ip(value):
    address = ipaddress.ip_address(value)
    return str(getattr(address, 'ipv4_mapped', None) or address)


class TestQuota:
    def __init__(self, path, clock=time.time, window=600, cooldown=300, daily=3600,
                 timezone='Asia/Shanghai'):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock, self.window, self.cooldown, self.daily = clock, window, cooldown, daily
        self.zone = ZoneInfo(timezone)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS days (ip TEXT, day TEXT, used REAL NOT NULL,
                    PRIMARY KEY(ip,day));
                CREATE TABLE IF NOT EXISTS windows (ip TEXT PRIMARY KEY, token TEXT UNIQUE,
                    secret TEXT, owner TEXT, window_end REAL, deadline REAL, updated REAL,
                    active INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY, value REAL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def now(self, db):
        current = self.clock()
        row = db.execute('SELECT value FROM clock WHERE id=1').fetchone()
        # Fail closed across clock rollback, rather than create a fresh day.
        if row and current < row[0]-2:
            raise TestDenied('test quota clock moved backwards')
        current = max(current, row[0] if row else current)
        db.execute('INSERT OR REPLACE INTO clock VALUES (1,?)', (current,))
        return current

    def day(self, stamp):
        date = datetime.fromtimestamp(stamp, self.zone)
        midnight = datetime.combine(date.date()+timedelta(days=1), datetime.min.time(), self.zone)
        return date.strftime('%Y-%m-%d'), midnight.timestamp()

    def used(self, db, ip, day):
        row = db.execute('SELECT used FROM days WHERE ip=? AND day=?', (ip,day)).fetchone()
        return row[0] if row else 0

    def account(self, db, row, now):
        if not row or not row['active']: return
        end, cursor = min(now,row['deadline']), row['updated']
        while cursor < end:
            day, midnight = self.day(cursor)
            stop = min(end,midnight)
            db.execute('INSERT INTO days VALUES (?,?,?) ON CONFLICT(ip,day) DO UPDATE SET used=used+excluded.used',
                       (row['ip'],day,stop-cursor))
            cursor = stop
        db.execute('UPDATE windows SET updated=? WHERE ip=?', (max(row['updated'],end),row['ip']))

    def acquire(self, ip, owner):
        ip = source_ip(ip)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE'); now = self.now(db)
            day, _ = self.day(now)
            db.execute('DELETE FROM days WHERE day<?', (self.day(now-8*86400)[0],))
            db.execute('DELETE FROM windows WHERE active=0 AND window_end<?', (now-86400,))
            row = db.execute('SELECT * FROM windows WHERE ip=?', (ip,)).fetchone()
            if row and row['active']:
                raise TestDenied('one test connection per IP')
            if row and now >= row['window_end'] and now < row['window_end']+self.cooldown:
                raise TestDenied('test channel cooldown')
            left = self.daily-self.used(db,ip,day)
            if left <= 0: raise TestDenied('daily test allowance exhausted')
            if row and now < row['window_end']:
                window_end = row['window_end']
            else:
                if not row and db.execute('SELECT COUNT(*) FROM windows').fetchone()[0] >= 4096:
                    raise TestDenied('test channel source limit')
                window_end = now+self.window
            token, password = secrets.token_hex(16), secrets.token_urlsafe(24)
            deadline = min(window_end,now+left)
            db.execute('INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?,?,1)',
                       (ip,token,password,owner,window_end,deadline,now))
            return Grant(ip,owner,token,password,deadline,deadline-now)

    def check(self, grant):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE'); now = self.now(db)
            row = db.execute('SELECT * FROM windows WHERE ip=? AND owner=? AND active=1',
                             (grant.ip,grant.owner)).fetchone()
            if not row: return 0
            self.account(db,row,now)
            day, _ = self.day(now)
            left = self.daily-self.used(db,grant.ip,day)
            return max(0,min(row['deadline']-now,left))

    def release(self, grant):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM windows WHERE ip=? AND owner=? AND active=1',
                             (grant.ip,grant.owner)).fetchone()
            # Releasing must still close a lease if the host clock rolled back.
            now = max(self.clock(),row['updated'] if row else 0)
            self.account(db,row,now)
            db.execute('UPDATE windows SET active=0,secret="",owner="" WHERE ip=? AND owner=?',
                       (grant.ip,grant.owner))

    def recover(self):
        """A relay restart cannot refund a previously admitted live interval."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE'); now = self.now(db)
            for row in db.execute('SELECT * FROM windows WHERE active=1').fetchall():
                self.account(db,row,now)
            db.execute('UPDATE windows SET active=0,secret="",owner=""')

    def authorize(self, request, trusted_relay=False):
        if request.get('protocol') != 'rtsp': return False
        token = request.get('path','').removeprefix('test/')
        if len(token)!=32: return False
        with self.connection() as db:
            row = db.execute('SELECT * FROM windows WHERE token=? AND active=1', (token,)).fetchone()
        if not row or self.clock() >= row['deadline'] or self.clock() < row['updated']-2:
            return False
        if request.get('action')=='read':
            return trusted_relay and source_ip(request.get('ip',''))==row['ip']
        if request.get('action')=='publish':
            return (ipaddress.ip_address(request.get('ip','')).is_loopback
                    and request.get('user')=='ciallochat-test-publisher'
                    and hmac.compare_digest(request.get('password',''),row['secret']))
        return False
