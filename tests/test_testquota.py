import tempfile
import unittest
from datetime import datetime
from pathlib import Path
import json
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

from streamctl.testquota import TestDenied, TestQuota, source_ip
from streamctl.testchannel import TestChannel, prepare
from streamctl.config import Store


class TestAllowance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026,10,4,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.path = Path(self.temp.name)/'quota.sqlite3'
        self.quota = TestQuota(self.path,clock=lambda:self.now)
        self.ip = '203.0.113.1'

    def test_reconnect_retains_window_and_cooldown_after_ten_minutes(self):
        first = self.quota.acquire(self.ip,'one')
        with self.assertRaises(TestDenied): self.quota.acquire(self.ip,'duplicate')
        self.now+=200; self.assertEqual(self.quota.check(first),400)
        self.quota.release(first); self.now+=20
        second=self.quota.acquire(self.ip,'two')
        self.assertEqual(second.remaining,380)
        self.now+=380; self.assertEqual(self.quota.check(second),0)
        self.quota.release(second)
        with self.assertRaises(TestDenied): self.quota.acquire(self.ip,'cooldown')
        self.now+=300
        self.assertEqual(self.quota.acquire(self.ip,'after').remaining,600)
        with self.quota.connection() as db:
            used=db.execute('SELECT SUM(used) FROM days').fetchone()[0]
        self.assertEqual(used,580)  # Offline time was not billed as watching.

    def test_six_full_windows_exhaust_daily_budget_and_next_day_resets(self):
        for i in range(6):
            grant=self.quota.acquire(self.ip,str(i)); self.now+=600
            self.quota.check(grant); self.quota.release(grant); self.now+=300
        with self.assertRaises(TestDenied): self.quota.acquire(self.ip,'overdaily')
        self.now+=86400
        self.assertEqual(self.quota.acquire(self.ip,'tomorrow').remaining,600)

    def test_last_daily_minutes_seek_to_shorter_countdown(self):
        day=self.quota.day(self.now)[0]
        with self.quota.connection() as db: db.execute('INSERT INTO days VALUES (?,?,?)',(self.ip,day,3590))
        grant=self.quota.acquire(self.ip,'short')
        self.assertEqual(grant.remaining,10)
        self.now+=10; self.assertEqual(self.quota.check(grant),0)
        self.quota.release(grant)
        with self.assertRaises(TestDenied): self.quota.acquire(self.ip,'daily exhausted')

    def test_restart_does_not_refund_connected_time_or_reset_window(self):
        grant=self.quota.acquire(self.ip,'old'); self.now+=100
        restored=TestQuota(self.path,clock=lambda:self.now); restored.recover()
        self.assertEqual(restored.acquire(self.ip,'new').remaining,500)
        with restored.connection() as db: self.assertEqual(db.execute('SELECT used FROM days').fetchone()[0],100)

    def test_midnight_counts_both_days_and_ipv4_mapped_addresses_share_allowance(self):
        self.now=datetime(2026,10,4,23,59,50,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        grant=self.quota.acquire('::ffff:203.0.113.1','midnight'); self.now+=20
        self.quota.check(grant); self.quota.release(grant)
        with self.quota.connection() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT used FROM days ORDER BY day')],[10,10])
        self.assertEqual(source_ip('::ffff:203.0.113.1'),self.ip)
        self.assertEqual(self.quota.acquire(self.ip,'same').remaining,580)

    def test_clock_rollback_fails_closed_and_ip_quotas_are_independent(self):
        grant=self.quota.acquire(self.ip,'one'); self.now-=10
        with self.assertRaises(TestDenied): self.quota.check(grant)
        self.quota.release(grant); self.now+=10
        self.assertEqual(self.quota.acquire('203.0.113.2','other').remaining,600)

    def test_internal_media_admission_requires_loopback_publisher_and_verified_peer(self):
        grant=self.quota.acquire(self.ip,'one')
        read=dict(action='read',protocol='rtsp',path='test/'+grant.token,ip=self.ip)
        self.assertFalse(self.quota.authorize(read))
        self.assertTrue(self.quota.authorize(read,True))
        self.assertFalse(self.quota.authorize(dict(read,ip='203.0.113.2'),True))
        publish=dict(read,action='publish',ip='127.0.0.1',user='ciallochat-test-publisher',password=grant.publisher_password)
        self.assertTrue(self.quota.authorize(publish))
        self.assertFalse(self.quota.authorize(dict(publish,ip=self.ip)))
        self.assertFalse(self.quota.authorize(dict(publish,password='invalid')))
        self.now+=600; self.assertFalse(self.quota.authorize(read,True))

    def test_pending_asset_does_not_consume_allowance_or_block_normal_startup(self):
        store=Store(Path(self.temp.name)/'service');store.initialize()
        with patch('streamctl.testchannel.shutil.which',return_value=sys.executable):
            self.assertFalse(prepare(store.path,allow_pending=True))
            with self.assertRaises(ValueError): prepare(store.path)
        self.assertEqual(json.loads((store.path/'test-video/config.json').read_text())['ffmpeg'],sys.executable)
        channel=TestChannel(store.path,store.read('settings.json'),quota=self.quota)
        import asyncio
        with self.assertRaises(TestDenied): asyncio.run(channel.acquire(self.ip,'pending'))
        with self.quota.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM windows').fetchone()[0],0)
