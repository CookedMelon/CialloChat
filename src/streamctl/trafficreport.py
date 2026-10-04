"""Bounded traffic reports and durable administrator email delivery."""
from email.message import EmailMessage
from datetime import datetime
import html
import json
import re
import secrets
import struct
import threading
import time

from .leases import send_message
from .traffic import TrafficStore
from .trafficpricing import price_report

MAX_IMAGE = 256 * 1024


from .trafficformat import summary


class TrafficReports:
    def __init__(self, admin):
        self.admin = admin
        self.store = TrafficStore(admin.store.path/'traffic/traffic.sqlite3')
        self.mail_lock = threading.Lock()

    def create(self, command):
        if len(command) not in (1, 3):
            raise ValueError('用法：traffic [--start 时间 --end 时间]')
        config = self.admin.mail_config()
        recipient = config.get('admin_recipient')
        if not isinstance(recipient, str) or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', recipient):
            raise ValueError('请先在 SMTP 配置中设置 admin_recipient 管理员邮箱')
        try:
            start, end = (int(command[1]), int(command[2])) if len(command) == 3 else (None, None)
        except ValueError:
            raise ValueError('非法流量查询时间') from None
        report = price_report(self.store.snapshot(start, end), self.admin.store.path/'traffic/pricing.json')
        report['timezone_offset_minutes'] = int(datetime.now().astimezone().utcoffset().total_seconds()/60)
        now = time.time()
        with self.store.connection() as db:
            db.execute('DELETE FROM reports WHERE created<?', (now-7*86400,))
            recent = db.execute('SELECT count(*) FROM reports WHERE created>?', (now-60,)).fetchone()[0]
            if recent >= 6:
                raise ValueError('每分钟最多生成 6 份流量报告，请稍后重试')
            if db.execute('SELECT count(*) FROM reports').fetchone()[0] >= 64:
                db.execute("DELETE FROM reports WHERE id IN (SELECT id FROM reports WHERE status IN ('sent','new') ORDER BY created LIMIT 8)")
            if db.execute('SELECT count(*) FROM reports').fetchone()[0] >= 64:
                raise ValueError('待发报告队列已满，请检查 SMTP 状态')
            identifier = secrets.token_hex(16)
            db.execute('INSERT INTO reports VALUES (?,?,?,NULL,?,0)',
                       (identifier, now, json.dumps(report), 'new'))
        return dict(ok=True, report_id=identifier, report=report)

    def upload(self, identifier, image):
        if (not isinstance(image, bytes) or not 33 <= len(image) <= MAX_IMAGE
                or image[:8] != b'\x89PNG\r\n\x1a\n' or image[12:16] != b'IHDR'):
            raise ValueError('报告图表必须是小于 256 KiB 的 PNG')
        width, height = struct.unpack('>II', image[16:24])
        if not (1 <= width <= 1600 and 1 <= height <= 1200):
            raise ValueError('图表尺寸过大')
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM reports WHERE id=?', (identifier,)).fetchone()
            if not row or row['created'] < time.time()-7*86400:
                raise ValueError('报告不存在或已过期')
            if row['status'] == 'new':
                db.execute("UPDATE reports SET image=?,status='queued' WHERE id=?", (image, identifier))
            elif row['status'] not in ('queued', 'sent'):
                raise ValueError('非法报告状态')
        sent = self.deliver(identifier)
        return dict(ok=True, email_status='sent' if sent else 'queued')

    def deliver(self, identifier):
        with self.mail_lock:
            with self.store.connection() as db:
                row = db.execute('SELECT * FROM reports WHERE id=?', (identifier,)).fetchone()
                if not row or row['status'] == 'sent':
                    return True
                if row['status'] != 'queued' or row['retry_at'] > time.time():
                    return False
                db.execute('UPDATE reports SET retry_at=? WHERE id=?', (time.time()+60, identifier))
            try:
                config = self.admin.mail_config()
                recipient = config.get('admin_recipient', '')
                if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', recipient):
                    raise ValueError('missing administrator email')
                report = json.loads(row['payload'])
                message = EmailMessage()
                message['From'] = config['from']; message['To'] = recipient
                message['Subject'] = 'CialloChat流量报告'
                message['Message-ID'] = f'<traffic-{identifier}@ciallochat.local>'
                text = summary(report)
                message.set_content(text)
                cid = f'traffic-chart-{identifier}'
                body = ('<html><body><div style="font-family:Arial,sans-serif;line-height:1.6">'
                        + html.escape(text).replace('\n', '<br>')
                        + f'</div><p><img src="cid:{cid}" alt="CialloChat流量图" '
                        'style="display:block;max-width:100%;height:auto"></p></body></html>')
                message.add_alternative(body, subtype='html')
                message.get_payload()[1].add_related(
                    row['image'], maintype='image', subtype='png',
                    cid=f'<{cid}>', disposition='inline')
                send_message(config, message)
            except Exception:
                print('traffic report email failed; retry queued; details omitted', flush=True)
                return False
            with self.store.connection() as db:
                db.execute("UPDATE reports SET status='sent',image=NULL WHERE id=?", (identifier,))
            return True

    def retry(self):
        with self.store.connection() as db:
            identifiers = [row[0] for row in db.execute(
                "SELECT id FROM reports WHERE status='queued' AND retry_at<=? ORDER BY created LIMIT 4", (time.time(),))]
        for identifier in identifiers:
            self.deliver(identifier)
