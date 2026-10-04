from datetime import datetime, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from streamctl.config import Store, atomic_write, dump
from streamctl.controladmin import Administration
from streamctl.traffic import TrafficSampler, TrafficStore, RETENTION
from streamctl.trafficchart import render
from streamctl.trafficformat import summary
from streamctl.trafficpricing import price_report


def conn(identifier, count, state='publish', path='live/alice'):
    return dict(kind='rtmps/conns' if state == 'publish' else 'rtsp/sessions',
                id=identifier, path=path, state=state,
                **{'inboundBytes' if state == 'publish' else 'outboundBytes': count})


class TrafficAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = TrafficStore(Path(self.tmp.name)/'traffic/traffic.sqlite3')
        self.sampler = TrafficSampler(self.store)
        self.base = int(time.time())//600*600-1200

    def test_baseline_push_multiple_readers_and_unrelated_paths(self):
        s, t = self.sampler, self.base
        s.sample([conn('push', 1000), conn('r1', 2000, 'read')], {'alice'}, t)
        s.sample([conn('push', 1500), conn('r1', 2300, 'read'), conn('r2', 100, 'read'),
                  conn('other', 999999, path='other/alice'),
                  conn('unknown', 999999, path='live/missing')], {'alice'}, t+1)
        s.flush(); report = self.store.snapshot(t, t+600)
        self.assertEqual(report['totals'], {'alice': {'push': 500, 'pull': 400}})
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_reconnect_counter_reset_and_checkpoint_restart_do_not_double_count(self):
        s, t = self.sampler, self.base
        s.sample([conn('p', 100)], {'alice'}, t)
        s.sample([conn('p', 200)], {'alice'}, t+1)
        s.flush()
        s = TrafficSampler(self.store)
        s.sample([conn('p', 400)], {'alice'}, t+3)
        s.sample([conn('p', 50)], {'alice'}, t+4)
        s.sample([conn('new', 80)], {'alice'}, t+5)
        s.flush()
        self.assertEqual(self.store.snapshot(t,t+600)['push'], 430)
        self.assertEqual(self.store.snapshot(t,t+600)['complete_buckets'], 0)

    def test_boundary_conserves_bytes_and_completed_buckets_survive_future_flush(self):
        s, t = self.sampler, self.base
        s.sample([conn('p', 0)], {'alice'}, t+598)
        s.sample([conn('p', 301)], {'alice'}, t+601)
        s.flush()
        s.sample([conn('p', 401)], {'alice'}, t+602); s.flush()
        self.assertEqual(self.store.snapshot(t,t+600)['push'], 200)
        self.assertEqual(self.store.snapshot(t+600,t+1200)['push'], 201)

    def test_idle_coverage_gap_and_clock_rollback(self):
        s, t = self.sampler, self.base
        for second in range(601): s.sample([], {'alice'}, t+second)
        s.flush()
        # First baseline/restart interval is correctly flagged incomplete.
        self.assertEqual(self.store.snapshot(t,t+600)['push'], 0)
        s.sample([], {'alice'}, t+601)
        for second in range(602,1201): s.sample([], {'alice'}, t+second)
        s.flush()
        self.assertEqual(self.store.snapshot(t+600,t+1200)['complete_buckets'], 1)
        s.sample([], {'alice'}, t+1100); s.sample([], {'alice'}, t+1300); s.flush()
        self.assertEqual(self.store.snapshot(t+1200,t+1800,now=t+1801)['complete_buckets'], 0)

    def test_retention_and_query_validation(self):
        now = time.time()
        self.store.save({(self.base-RETENTION-600,'alice'):[1,2]}, {}, {}, now)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM buckets').fetchone()[0], 0)
        for start,end in ((self.base+1,self.base+600), (self.base,self.base),
                          (self.base-RETENTION,self.base), (self.base,self.base+86400)):
            with self.assertRaises(ValueError): self.store.snapshot(start,end)

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is client-only')
    def test_plot_size_and_sums_over_long_range(self):
        report = dict(start=self.base-6*86400,end=self.base,interval=600,bucket_count=864,
                      complete_buckets=0,coverage=[],totals={'alice':dict(push=12345,pull=6789)},
                      push=12345,pull=6789,series=[dict(start=self.base-600,username='alice',push=12345,pull=6789)])
        output = render(report, Path(self.tmp.name)/'report.png')
        self.assertLess(output.stat().st_size, 100*1024)
        self.assertEqual(output.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        self.assertIn('19,134 B', summary(report))


class TrafficFormatting(unittest.TestCase):
    def test_exact_concise_template_and_outbound_only_decimal_price(self):
        report = dict(start=0,end=600,push=30175655,pull=23746302,
                      totals={'cc':dict(push=30175655,pull=23746302)},
                      complete_buckets=0,bucket_count=1)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'pricing.json'
            path.write_text(json.dumps(dict(bytes_per_gb=1000000000,
                                            push_cny_per_gb=0,pull_cny_per_gb='0.8')))
            price_report(report,path)
        self.assertEqual(report['estimated_cny'],'0.0190')
        self.assertEqual(summary(report).splitlines()[1:], [
            '',
            'cc：推流 30.176 MB (30,175,655 B)；观看 23.746 MB (23,746,302 B)；合计 53.922 MB (53,921,957 B)',
            '',
            '推流总计：30.176 MB (30,175,655 B)',
            '观看总计：23.746 MB (23,746,302 B)',
            '全部合计：53.922 MB (53,921,957 B)',
            '预计价格：¥0.0190'])

    def test_report_default_is_raw_outbound_times_rate_without_monthly_discount(self):
        path=Path('config/traffic-pricing.example.json')
        report=dict(push=4661942580,pull=7874611001,monthly_before={'2026-10':0})
        self.assertEqual(price_report(report,path)['estimated_cny'],'5.8670')
        report['monthly_before']={'2026-10':999999999999999}
        self.assertEqual(price_report(report,path)['estimated_cny'],'5.8670')
        report.update(push=10**12,pull=1073741824)
        self.assertEqual(price_report(report,path)['estimated_cny'],'0.8000')

    def test_monthly_free_allowance_tier_crossing_and_month_reset(self):
        config=dict(model='cdt-mainland-bgp',bytes_per_gb=1073741824,free_gb_per_month='20',
                    tiers=[dict(up_to_gb='10240',cny_per_gb='0.80'),
                           dict(up_to_gb='51200',cny_per_gb='0.75'),
                           dict(up_to_gb='153600',cny_per_gb='0.70'),
                           dict(up_to_gb=None,cny_per_gb='0.65')])
        gb=config['bytes_per_gb']
        october=int(datetime(2026,10,31,15,50,tzinfo=timezone.utc).timestamp())
        november=october+600
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'pricing.json';path.write_text(json.dumps(config))
            report=dict(push=100*gb,pull=gb,series=[dict(start=october,pull=gb)],
                        monthly_before={'2026-10':int(19.5*gb)})
            self.assertEqual(price_report(report,path)['estimated_cny'],'0.4000')
            self.assertEqual(price_report(report,path)['estimated_cny'],'0.4000')
            report['pull']=2*gb;report['series'][0]['pull']=2*gb
            report['monthly_before']['2026-10']=(20+10239)*gb
            self.assertEqual(price_report(report,path)['estimated_cny'],'1.5500')
            config['monthly_usage_offset_gb']={'2026-10':'20'};path.write_text(json.dumps(config))
            report.update(pull=2*gb,monthly_before={},series=[dict(start=october,pull=gb),dict(start=november,pull=gb)])
            self.assertEqual(price_report(report,path)['estimated_cny'],'0.8000')

    def test_monthly_ledger_survives_retention_and_repeated_checkpoints(self):
        now=int(datetime(2026,10,20,tzinfo=timezone.utc).timestamp())
        old=now-14*86400
        with tempfile.TemporaryDirectory() as directory:
            store=TrafficStore(Path(directory)/'traffic.sqlite3')
            store.save({(old,'cc'):[0,500]},{},{},old)
            store.save({(now,'cc'):[0,100]},{},{},now)
            store.save({(now,'cc'):[0,100]},{},{},now)
            report=TrafficStore(store.path).snapshot(now,now+600,now=now+600)
            self.assertEqual(report['pull'],100)
            self.assertEqual(report['monthly_before']['2026-10'],500)
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM buckets').fetchone()[0],1)
                db.execute('DROP TABLE monthly')
            migrated=TrafficStore(store.path)
            with migrated.connection() as db:
                self.assertEqual(db.execute('SELECT pull FROM monthly').fetchone()[0],100)

    def test_binary_units_both_directions_and_invalid_price_config(self):
        report=dict(push=1073741824,pull=2147483648)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'pricing.json'
            config=dict(bytes_per_gb=1073741824,push_cny_per_gb='0.10',pull_cny_per_gb='0.70')
            path.write_text(json.dumps(config))
            self.assertEqual(price_report(report,path)['estimated_cny'],'1.5000')
            for field,value in [('bytes_per_gb',0),('push_cny_per_gb',-1),('pull_cny_per_gb','NaN')]:
                invalid=dict(config);invalid[field]=value;path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError): price_report(report,path)
            path.unlink()
            self.assertIsNone(price_report(report,path)['estimated_cny'])


class TrafficMail(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        store=Store(self.tmp.name); store.initialize()
        atomic_write(store.path/'notifications/smtp.json', dump(dict(
            host='smtp.example.com',port=465,security='ssl',
            **{'from':'sender@example.com','recipients':{'alice':'alice@example.com'},
               'admin_recipient':'admin@example.com'})))
        self.admin=Administration(self.tmp.name)

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is client-only')
    def test_server_owned_totals_durable_retry_and_duplicate_upload(self):
        with patch('streamctl.trafficreport.send_message', side_effect=OSError('offline')) as send:
            result=self.admin.command(['traffic'])
            path=render(result['report'], Path(self.tmp.name)/'report.png')
            image=path.read_bytes()
            status=self.admin.traffic_reports.upload(result['report_id'], image)
            self.assertEqual(status['email_status'],'queued')
            send.side_effect=None
            new=Administration(self.tmp.name)
            with new.traffic_reports.store.connection() as db:
                db.execute('UPDATE reports SET retry_at=0')
            new.traffic_reports.retry()
            self.assertEqual(send.call_count,2)
            self.assertEqual(send.call_args.args[1]['To'],'admin@example.com')
            message=send.call_args.args[1]
            self.assertIn('全部合计',message.get_body(preferencelist=('plain',)).get_content())
            html_body=message.get_body(preferencelist=('html',)).get_content()
            inline=[part for part in message.walk() if part.get_content_type()=='image/png']
            self.assertEqual(len(inline),1)
            self.assertEqual(inline[0].get_content_disposition(),'inline')
            self.assertIn('cid:'+inline[0]['Content-ID'].strip('<>'),html_body)
            self.assertFalse(any(part.get_content_disposition()=='attachment' for part in message.walk()))
            self.assertEqual(inline[0].get_payload(decode=True),image)
            self.assertEqual(new.traffic_reports.upload(result['report_id'],image)['email_status'],'sent')
            self.assertEqual(send.call_count,2)
            with new.traffic_reports.store.connection() as db:
                self.assertIsNone(db.execute('SELECT image FROM reports').fetchone()[0])

    def test_invalid_png_missing_admin_and_report_rate_bound(self):
        result=self.admin.command(['traffic'])
        for image in (b'x'*34, b'\x89PNG\r\n\x1a\n'+b'x'*300000):
            with self.assertRaises(ValueError): self.admin.traffic_reports.upload(result['report_id'],image)
        for i in range(5): self.admin.command(['traffic'])
        with self.assertRaises(ValueError): self.admin.command(['traffic'])
        path=self.admin.store.path/'notifications/smtp.json'; config=json.loads(path.read_text())
        del config['admin_recipient']; atomic_write(path,dump(config))
        with self.assertRaises(ValueError): self.admin.command(['traffic'])


if __name__ == '__main__': unittest.main()
