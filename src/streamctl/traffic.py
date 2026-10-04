"""Application byte accounting; independent of credential storage, stdlib only."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

INTERVAL = 600
RETENTION = 7 * 86400


class TrafficStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS buckets (
                    start INTEGER, username TEXT, push INTEGER, pull INTEGER,
                    PRIMARY KEY(start,username));
                CREATE TABLE IF NOT EXISTS coverage (
                    start INTEGER PRIMARY KEY, seconds REAL, incomplete INTEGER);
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, created REAL, payload TEXT, image BLOB,
                    status TEXT, retry_at REAL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monthly'").fetchone():
                db.execute('CREATE TABLE monthly (month TEXT PRIMARY KEY, pull INTEGER NOT NULL)')
                db.execute("INSERT INTO monthly SELECT strftime('%Y-%m',start,'unixepoch','+8 hours'),sum(pull) "
                           "FROM buckets GROUP BY 1")
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=0.25)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def restore(self):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None

    def save(self, rows, coverage, state, now):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            monthly = {}
            for (start, username), values in rows.items():
                prior = db.execute('SELECT pull FROM buckets WHERE start=? AND username=?', (start, username)).fetchone()
                delta = values[1] - (prior[0] if prior else 0)
                month = time.strftime('%Y-%m', time.gmtime(start+8*3600))
                monthly[month] = monthly.get(month, 0) + delta
            db.executemany('INSERT INTO monthly VALUES (?,?) ON CONFLICT(month) DO UPDATE SET pull=pull+excluded.pull',
                           monthly.items())
            db.executemany('INSERT OR REPLACE INTO buckets VALUES (?,?,?,?)',
                           [(start, user, *values) for (start, user), values in rows.items()])
            db.executemany('INSERT OR REPLACE INTO coverage VALUES (?,?,?)',
                           [(start, *values) for start, values in coverage.items()])
            db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (json.dumps(state),))
            cutoff = int(now-RETENTION)//INTERVAL*INTERVAL
            db.execute('DELETE FROM buckets WHERE start<?', (cutoff,))
            db.execute('DELETE FROM coverage WHERE start<?', (cutoff,))
            db.execute("DELETE FROM monthly WHERE month<strftime('%Y-%m',?,'unixepoch','+8 hours','start of month','-1 month')", (now,))
            db.execute('DELETE FROM reports WHERE created<?', (now-RETENTION,))
            # Bound queued image storage independently of accounts/stream traffic.
            db.execute('DELETE FROM reports WHERE id NOT IN '
                       '(SELECT id FROM reports ORDER BY created DESC LIMIT 64)')

    def snapshot(self, start=None, end=None, now=None):
        now = time.time() if now is None else now
        end = int(now)//INTERVAL*INTERVAL if end is None else end
        start = end-3600 if start is None else start
        if (type(start) is not int or type(end) is not int or start % INTERVAL or end % INTERVAL
                or start >= end or end > int(now)//INTERVAL*INTERVAL
                or start < int(now-RETENTION)//INTERVAL*INTERVAL):
            raise ValueError('时间须按整 10 分钟对齐，限最近 7 天已结束的区间，开始须早于结束')
        with self.connection() as db:
            db.execute('BEGIN')
            rows = db.execute('SELECT * FROM buckets WHERE start>=? AND start<? ORDER BY start,username LIMIT 25001',
                              (start, end)).fetchall()
            coverage = {r['start']: dict(r) for r in db.execute(
                'SELECT * FROM coverage WHERE start>=? AND start<?', (start, end))}
            monthly = {r['month']: r['pull'] for r in db.execute('SELECT * FROM monthly')}
            following = {r[0]: r[1] for r in db.execute(
                "SELECT strftime('%Y-%m',start,'unixepoch','+8 hours'),sum(pull) FROM buckets WHERE start>=? GROUP BY 1", (start,))}
            before = {month: max(0, amount-following.get(month, 0)) for month, amount in monthly.items()}
        if len(rows) > 25000:
            raise ValueError('记录过多，请缩短查询区间')
        totals = {}
        series = []
        for row in rows:
            record = dict(row); series.append(record)
            user = totals.setdefault(row['username'], dict(push=0, pull=0))
            user['push'] += row['push']; user['pull'] += row['pull']
        complete = sum(1 for stamp in range(start, end, INTERVAL)
                       if stamp in coverage and coverage[stamp]['seconds'] >= 598
                       and not coverage[stamp]['incomplete'])
        return dict(start=start, end=end, interval=INTERVAL, series=series, totals=totals,
                    push=sum(u['push'] for u in totals.values()),
                    pull=sum(u['pull'] for u in totals.values()),
                    complete_buckets=complete, bucket_count=(end-start)//INTERVAL,
                    coverage=list(coverage.values()), monthly_before=before)


class TrafficSampler:
    """One in-memory update per existing watchdog poll; checkpoint once a minute."""
    def __init__(self, store):
        self.store = store
        state = store.restore() or {}
        self.last = state.get('last')
        self.counts = state.get('counts', {})
        self.rows = {(r[0], r[1]): r[2:] for r in state.get('rows', [])}
        self.coverage = {r[0]: r[1:] for r in state.get('coverage', [])}
        self.checkpoint = time.monotonic()
        self.restarted = True

    def sample(self, items, users, now=None):
        now = time.time() if now is None else now
        current = {}
        deltas = []
        for item in items:
            state = item.get('state')
            if state not in ('publish', 'read'):
                continue
            path = item.get('path', '')
            if not path.startswith('live/') or path[5:] not in users:
                continue
            count = item.get('inboundBytes' if state == 'publish' else 'outboundBytes')
            if type(count) is not int or count < 0:
                continue
            key = item['kind'] + ':' + item['id'] + ':' + state + ':' + path
            current[key] = count
            previous = self.counts.get(key, 0)
            if self.last is not None and now > self.last:
                deltas.append((path[5:], state, count-previous if count >= previous else count))
        # Wall-clock rollback: establish a fresh baseline, never subtract bytes.
        if self.last is None or now <= self.last:
            self.last, self.counts = now, current
            self.restarted = True
            return
        crossed_boundary = int(now)//INTERVAL != int(self.last)//INTERVAL
        elapsed = now-self.last
        # Attribute a delta across interval boundaries proportionally. Sampling
        # gaps are explicitly incomplete; no claim of exact intra-gap timing.
        begin = max(self.last, now-RETENTION)
        cursor = begin
        assigned = [0] * len(deltas)
        while cursor < now:
            bucket = int(cursor)//INTERVAL*INTERVAL
            stop = min(now, bucket+INTERVAL)
            seconds = stop-cursor
            coverage = self.coverage.setdefault(bucket, [0.0, 0])
            if elapsed <= 3 and not self.restarted:
                coverage[0] = min(INTERVAL, coverage[0]+seconds)
            else:
                coverage[1] = 1
            for i, (user, state, count) in enumerate(deltas):
                # Use cumulative rounding to preserve totals at boundaries.
                target = int(count*(stop-begin)/(now-begin))
                amount = target-assigned[i]; assigned[i] = target
                value = self.rows.setdefault((bucket, user), [0, 0])
                value[0 if state == 'publish' else 1] += amount
            cursor = stop
        self.last, self.counts, self.restarted = now, current, False
        if crossed_boundary or time.monotonic()-self.checkpoint >= 60:
            self.flush()

    def flush(self):
        if self.last is None:
            return
        # Only the current bucket stays in memory after a successful atomic
        # checkpoint. Counter state and bucket totals commit together.
        keep = int(self.last)//INTERVAL*INTERVAL
        state = dict(last=self.last, counts=self.counts,
                     rows=[[*key, *value] for key, value in self.rows.items() if key[0] >= keep],
                     coverage=[[key, *value] for key, value in self.coverage.items() if key >= keep])
        self.store.save(self.rows, self.coverage, state, self.last)
        self.rows = {k: v for k, v in self.rows.items() if k[0] >= keep}
        self.coverage = {k: v for k, v in self.coverage.items() if k >= keep}
        self.checkpoint = time.monotonic()
