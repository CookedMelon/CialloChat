"""Isolated real-media validation. Default backend is Docker Compose."""
import argparse
import concurrent.futures
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from streamctl.accounts import new_account, new_password, hash_password, credentials, read_identity
from streamctl.config import ROOT, Store, VERSION, atomic_write, dump, render, check_tls
from streamctl.service import Service, commit, backup, restore


def wait(predicate, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate(): return
        time.sleep(.15)
    raise AssertionError('bounded wait expired')


def free_ports():
    sockets = []
    try:
        for _ in range(4):
            s = socket.socket(); s.bind(('127.0.0.1', 0)); sockets.append(s)
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets: s.close()


class Harness:
    def __init__(self, args, directory):
        self.args = args
        self.store = Store(directory)
        self.store.initialize()
        settings, accounts, control = self.store.load()
        settings.update(zip(('rtmp_port', 'rtmps_port', 'rtsp_port', 'api_port'), args.ports or free_ports()))
        settings['bind_address'] = '127.0.0.1'
        self.passwords = {'alice': new_password(), 'bob': new_password() + '&#+?%'}
        self.read_keys = {'alice':new_password(), 'bob':new_password()+'&#/%中文'}
        accounts['users'] = [new_account(name, value, self.read_keys[name]) for name, value in self.passwords.items()]
        atomic_write(self.store.path / 'settings.json', dump(settings))
        atomic_write(self.store.path / 'accounts.json', dump(accounts))
        atomic_write(self.store.path / 'mediamtx/mediamtx.yml', render(settings, accounts, control))
        self.service = Service(self.store)
        self.process = None
        self.publishers = []
        self.handles = []
        self.log = self.store.path / 'server.log'
        self.project = 'ciallochat-smoke-' + str(os.getpid())
        self.original_project = os.environ.get('CIALLOCHAT_PROJECT')
        os.environ['CIALLOCHAT_PROJECT'] = self.project
        if args.mediamtx:
            version = subprocess.check_output([args.mediamtx, '--version'], text=True).strip().lstrip('v')
            if version != VERSION['mediamtx']: raise ValueError('native MediaMTX version differs from lock')
            # Lifecycle is injected only in the harness; production CLI always uses Compose.
            self.service.running = lambda: self.process is not None and self.process.poll() is None
            def native_compose(args, capture=True):
                if args == ['restart','mediamtx']:
                    self.stop_service(); self.start(); return ''
                raise ValueError('unsupported native lifecycle operation')
            self.service.compose = native_compose

    def start(self):
        self.service.settings = self.store.read('settings.json')
        if self.args.mediamtx:
            s = self.service.settings
            env = os.environ.copy()
            env.update(MTX_APIADDRESS=f'127.0.0.1:{s["api_port"]}', MTX_RTSPADDRESS=f'127.0.0.1:{s["rtsp_port"]}',
                       MTX_RTMPADDRESS=f'127.0.0.1:{s["rtmp_port"]}', MTX_RTMPSADDRESS=f'127.0.0.1:{s["rtmps_port"]}',
                       MTX_RTMPSERVERCERT=str(self.store.path/s['certificate']), MTX_RTMPSERVERKEY=str(self.store.path/s['private_key']))
            file = open(self.log, 'ab'); self.handles.append(file)
            self.process = subprocess.Popen([self.args.mediamtx, str(self.store.path/'mediamtx/mediamtx.yml')], env=env, stdout=file, stderr=file)
            self.service.wait_loaded((self.store.path/'mediamtx/mediamtx.yml').read_text())
        else:
            self.service.up()

    def stop_service(self):
        if self.args.mediamtx:
            if self.process and self.process.poll() is None:
                self.process.terminate(); self.process.wait(timeout=8)
            self.process = None
        else:
            self.service.down()

    def cleanup(self):
        for p in self.publishers:
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=5)
                except subprocess.TimeoutExpired: p.kill(); p.wait()
        try: self.stop_service()
        finally:
            for f in self.handles: f.close()
            if self.original_project is None: os.environ.pop('CIALLOCHAT_PROJECT', None)
            else: os.environ['CIALLOCHAT_PROJECT'] = self.original_project

    def state(self, path):
        try: return self.service.api('paths/get/' + urllib.parse.quote(path, safe=''))
        except urllib.error.HTTPError as exc:
            if exc.code == 404: return None
            raise

    def deployment_state(self):
        container_id = self.service.compose(['ps', '-q', 'mediamtx']).strip()
        assert container_id, 'missing test container'
        item = json.loads(subprocess.check_output(['docker', 'inspect', container_id], text=True, timeout=15))[0]
        host = item['HostConfig']
        assert item['Config']['Image'] == VERSION['image'], 'unlocked image'
        assert item['Config']['User'] == f'{os.getuid()}:{os.getgid()}', 'unexpected container UID/GID'
        assert host['RestartPolicy']['Name'] == 'unless-stopped'
        assert host['ReadonlyRootfs'] is True
        assert host['LogConfig']['Type'] == 'json-file'
        assert host['LogConfig']['Config'] == {'max-size': '10m', 'max-file': '3'}
        ports = host['PortBindings']
        publish_port = '1936/tcp' if self.service.settings['mode'] == 'production' else '1935/tcp'
        assert set(ports) == {publish_port, '8554/tcp', '9997/tcp'}
        assert all(binding['HostIp'] == '127.0.0.1' for values in ports.values() for binding in values)
        mounts = {m['Destination']: m for m in item['Mounts']}
        assert set(mounts) == {'/config', '/certs'}
        assert all(m['Type'] == 'bind' and m['RW'] is False for m in mounts.values())
        return {'mode': self.service.settings['mode'], 'image': item['Config']['Image'],
                'user': item['Config']['User'], 'ports': ports, 'restart': 'unless-stopped',
                'read_only_config_and_certificates': True, 'logging': host['LogConfig']}

    def ready(self, path):
        p = self.state(path); return bool(p and p['ready'])

    def source_id(self, path):
        return self.state(path)['source']['id']

    def publish(self, username='alice', path=None, value=None, anonymous=False, tls=False, trusted=True):
        s = self.service.settings
        query = '' if anonymous else '?' + urllib.parse.urlencode({'user':username, 'pass':value if value is not None else self.passwords[username]})
        url = f'{"rtmps" if tls else "rtmp"}://localhost:{s["rtmps_port" if tls else "rtmp_port"]}/{path or "live/"+username}' + query
        cmd = [self.args.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-re', '-f', 'lavfi', '-i',
               f'color=c={"red" if username == "alice" else "blue"}:s=1280x720:r=30',
               '-re', '-f', 'lavfi', '-i', f'sine=frequency={440 if username == "alice" else 880}:sample_rate=48000',
               '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-tune', 'zerolatency', '-pix_fmt', 'yuv420p',
               '-b:v', '2M', '-g', '30', '-c:a', 'aac', '-b:a', '128k']
        if tls:
            cmd += ['-tls_verify', '1', '-ca_file', str(self.store.path/'certs/ca.crt') if trusted else '/etc/ssl/certs/ca-certificates.crt']
        cmd += ['-f', 'flv', url]
        file = open(self.store.path/f'publisher-{len(self.publishers)}.log', 'wb'); self.handles.append(file)
        p = subprocess.Popen(cmd, stdout=file, stderr=file)
        self.publishers.append(p)
        return p

    def read_url(self, username):
        return credentials(self.service.settings, username, read_key=self.read_keys[username])['read_url']

    def read(self, username, seconds=3):
        url = self.read_url(username)
        probe = subprocess.run([self.args.ffprobe, '-v', 'error', '-rtsp_transport', 'tcp', '-show_entries',
                                'stream=codec_name,codec_type', '-of', 'json', url], capture_output=True, timeout=12)
        assert probe.returncode == 0, 'ffprobe failed'
        codecs = {x['codec_name'] for x in json.loads(probe.stdout)['streams']}
        assert {'h264', 'aac'} <= codecs, codecs
        result = subprocess.run([self.args.ffmpeg, '-v', 'error', '-nostdin', '-rtsp_transport', 'tcp', '-i', url,
                                 '-t', str(seconds), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'], capture_output=True, timeout=12)
        assert result.returncode == 0, 'audio/video decode failed'
        frame = subprocess.run([self.args.ffmpeg, '-v', 'error', '-nostdin', '-rtsp_transport', 'tcp', '-i', url,
                                '-frames:v', '1', '-vf', 'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], capture_output=True, timeout=12)
        assert frame.returncode == 0 and len(frame.stdout) == 3, 'missing RGB sample'
        r,g,b = frame.stdout
        assert (r > 200 and b < 50) if username == 'alice' else (b > 200 and r < 50), (r,g,b)

    def rejected_read(self, username='alice', key=None, identity=None, anonymous=False, removed=False):
        host = f"127.0.0.1:{self.service.settings['rtsp_port']}"
        if anonymous:
            url = f'rtsp://{host}/live/{username}'
        else:
            login = urllib.parse.quote(identity or read_identity(username), safe='')
            value = urllib.parse.quote(key if key is not None else 'incorrect-watch-key', safe='')
            url = f'rtsp://{login}:{value}@{host}/live/{username}'
        result = subprocess.run([self.args.ffprobe,'-v','error','-rtsp_transport','tcp',
                                 '-show_streams',url],capture_output=True,timeout=12)
        codes = (b'400',b'404') if removed else (b'401',)
        assert result.returncode != 0 and any(code in result.stderr for code in codes), 'reader rejection response unproven'
        if removed:
            assert self.state('live/'+username) is None, 'deleted path remains available'

    def rejected(self, **kwargs):
        path = kwargs.get('path') or 'live/' + kwargs.get('username','alice')
        before = self.state(path)
        source = before.get('source') if before else None
        offset = self.log.stat().st_size if self.args.mediamtx else None
        attempt_started = datetime.now(timezone.utc).isoformat()
        p = self.publish(**kwargs)
        try: p.wait(timeout=9)
        except subprocess.TimeoutExpired:
            p.terminate(); p.wait(timeout=5)
            raise AssertionError('publisher stayed connected; rejection unproven')
        assert p.returncode != 0, 'unexpected successful publish'
        if self.args.mediamtx:
            reason = self.log.read_text()[offset:]
        else:
            reason = self.service.compose(['logs', '--since', attempt_started, '--tail', '200', 'mediamtx'])
        duplicate = source and path == 'live/' + kwargs.get('username','alice') and not kwargs.get('anonymous') and kwargs.get('value',self.passwords.get(kwargs.get('username','alice'))) == self.passwords.get(kwargs.get('username','alice'))
        expected = 'tls' if kwargs.get('tls') and not kwargs.get('trusted',True) else ('already publishing' if duplicate else 'authentication')
        if expected == 'tls':
            client = (self.store.path/f'publisher-{len(self.publishers)-1}.log').read_text().lower()
            assert 'certificate' in client and ('verif' in client or 'trust' in client), 'no explicit client certificate rejection'
            assert '[RTMPS]' in reason and 'closed:' in reason, 'no closed server TLS connection'
        else:
            assert expected in reason.lower() or ('not configured' in reason.lower() and path not in {u['stream_path'] for u in self.store.read('accounts.json')['users']}), 'no server rejection evidence'
        after = self.state(path)
        assert (after.get('source') if after else None) == source, 'existing publisher changed or rejected path became ready'

    def change(self, action, name='alice'):
        with self.store.lock():
            s,a,c = self.store.load()
            u = next(u for u in a['users'] if u['username'] == name)
            revoke = []
            if action == 'disable': u['enabled'] = False; revoke = [u['stream_path']]
            if action == 'enable': u['enabled'] = True
            if action == 'reset':
                self.old_password = self.passwords[name]
                self.passwords[name] = new_password()
                u['publish_key_hash'] = hash_password(self.passwords[name]); revoke = [u['stream_path']]
            if action == 'delete': a['users'].remove(u); revoke = [u['stream_path']]
            if action == 'reset-read':
                self.old_read_key = self.read_keys[name]
                self.read_keys[name] = new_password()
                u['read_key_hash'] = hash_password(self.read_keys[name]); revoke = [u['stream_path']]
            states = ('read',) if action == 'reset-read' else (('publish','read') if action in ('disable','delete') else ('publish',))
            commit(self.store,a,revoke=revoke,service=self.service,revoke_states=states)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mediamtx', help='Explicit native binary for auxiliary validation (not a Docker deployment check)')
    parser.add_argument('--ffmpeg', default='ffmpeg'); parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--ports', type=int, nargs=4, metavar=('RTMP','RTMPS','RTSP','API'))
    parser.add_argument('--report', default=str(ROOT/'runtime/reports/smoke.json'))
    args = parser.parse_args()
    os.umask(0o077)
    report = {'version':VERSION, 'backend':'native' if args.mediamtx else 'compose', 'started_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()), 'tests':[]}
    status = 0
    def test(name, operation):
        start = time.monotonic()
        try:
            operation()
            report['tests'].append({'name':name,'status':'passed','seconds':round(time.monotonic()-start,2)})
            print('PASS ' + name, flush=True)
        except Exception as exc:
            report['tests'].append({'name':name,'status':'failed','error':type(exc).__name__ + ': ' + str(exc) if isinstance(exc, AssertionError) else type(exc).__name__})
            print('FAIL ' + name + ' (' + type(exc).__name__ + ')',flush=True)
            raise
    with tempfile.TemporaryDirectory(prefix='ciallochat-smoke-') as directory:
        h = None
        try:
            h = Harness(args,directory)
            test('startup and config load',h.start)
            if not args.mediamtx:
                test('actual container image, ports, UID, mounts and logging',
                     lambda: report.setdefault('deployment', []).append(h.deployment_state()))
            for name,kw in [('anonymous rejected',{'anonymous':True}), ('wrong password rejected',{'value':'incorrect-password'}),
                            ('unknown path rejected',{'path':'live/unknown'}), ('cross-path rejected',{'path':'live/bob'})]:
                test(name,lambda kw=kw:h.rejected(**kw))
            def publish_both():
                h.alice = h.publish(); h.bob = h.publish('bob')
                wait(lambda:h.ready('live/alice') and h.ready('live/bob'))
            test('two publishers ready',publish_both)
            def publisher_api_denied():
                denied = Service(h.store, h.service.settings, {'username':'alice','password':h.passwords['alice']})
                try: denied.api('config/global/get')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 401
                    return
                raise AssertionError('publisher accessed management API')
            test('publisher cannot access management API',publisher_api_denied)
            for name, kw in [
                ('anonymous read denied', {'anonymous':True}),
                ('wrong watch key denied', {}),
                ('username alone denied', {'identity':'alice','key':''}),
                ('publish key cannot read', {'identity':'alice','key':h.passwords['alice']}),
                ('publish key cannot authenticate viewer', {'key':h.passwords['alice']}),
                ('Bob viewer cannot read Alice', {'identity':read_identity('bob'),'key':h.read_keys['bob']})]:
                test(name,lambda kw=kw:h.rejected_read(**kw))
            test('watch key cannot publish',lambda:h.rejected(value=h.read_keys['alice']))
            test('viewer identity cannot publish',lambda:h.rejected(username=read_identity('alice'),
                    path='live/alice',value=h.read_keys['alice']))
            def viewer_api_denied():
                denied=Service(h.store,h.service.settings,{'username':read_identity('alice'),'password':h.read_keys['alice']})
                try: denied.api('config/global/get')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 401
                    return
                raise AssertionError('viewer accessed management API')
            test('viewer cannot access management API',viewer_api_denied)
            def reset_read():
                source=h.source_id('live/alice')
                reader=subprocess.Popen([args.ffmpeg,'-v','error','-nostdin','-rtsp_transport','tcp',
                    '-i',h.read_url('alice'),'-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                h.publishers.append(reader)
                wait(lambda:len(h.state('live/alice')['readers']) >= 1)
                h.change('reset-read')
                wait(lambda:reader.poll() is not None)
                h.rejected_read(key=h.old_read_key)
                assert h.alice.poll() is None and h.source_id('live/alice') == source
                h.read('alice')
            test('watch reset disconnects readers, old denied, new decoded, publisher preserved',reset_read)

            def two_readers():
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    readers = [pool.submit(h.read,'alice',4) for _ in range(2)]
                    wait(lambda:len(h.state('live/alice')['readers']) >= 2,12)
                    for r in readers: r.result()
            test('two concurrent readers decode H264 AAC',two_readers)
            test('Bob distinct video and audio decode',lambda:h.read('bob'))
            test('second publisher rejected, original preserved',lambda:h.rejected())
            test('cross-path rejected, Bob preserved',lambda:h.rejected(path='live/bob'))
            def disable():
                reader=subprocess.Popen([args.ffmpeg,'-v','error','-nostdin','-rtsp_transport','tcp','-i',
                    h.read_url('alice'),'-map','0:v:0','-map','0:a:0','-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                h.publishers.append(reader)
                wait(lambda:len(h.state('live/alice')['readers']) >= 1)
                h.change('disable'); wait(lambda:h.alice.poll() is not None)
                wait(lambda:reader.poll() is not None)
                wait(lambda:not h.ready('live/alice'))
                h.rejected()
                assert h.bob.poll() is None and h.ready('live/bob')
            test('disable terminates existing connection and denies reconnect',disable)
            def enable():
                h.change('enable'); h.alice=h.publish(); wait(lambda:h.ready('live/alice'))
            test('enable permits new publish',enable)
            def reset():
                h.change('reset'); wait(lambda:h.alice.poll() is not None)
                h.rejected(value=h.old_password)
                h.alice=h.publish(); wait(lambda:h.ready('live/alice')); h.read('alice')
            test('reset terminates old connection, old denied, new decoded',reset)
            def persistence():
                before=h.store.read('accounts.json')
                h.stop_service(); h.start()
                assert h.store.read('accounts.json') == before
                h.alice=h.publish(); wait(lambda:h.ready('live/alice')); h.read('alice')
            test('restart preserves accounts and accepts publish',persistence)
            def tls_setup():
                h.stop_service()
                subprocess.run([str(ROOT/'scripts/make-test-cert.sh'),str(h.store.path/'certs')],check=True,stdout=subprocess.DEVNULL)
                s,a,c=h.store.load(); s['mode']='production'
                check_tls(h.store,s)
                atomic_write(h.store.path/'settings.json',dump(s))
                atomic_write(h.store.path/'mediamtx/mediamtx.yml',render(s,a,c)); h.start()
                with socket.socket() as sock:
                    assert sock.connect_ex(('127.0.0.1',s['rtmp_port'])) != 0, 'plaintext RTMP open in production'
            test('production TLS validated and plaintext closed',tls_setup)
            def wrong_tls():
                port=h.service.settings['rtmps_port']
                for context,hostname in [(ssl.create_default_context(),'localhost'),
                                         (ssl.create_default_context(cafile=str(h.store.path/'certs/ca.crt')),'wrong.example.com')]:
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=4) as sock:
                            with context.wrap_socket(sock,server_hostname=hostname): pass
                    except ssl.SSLCertVerificationError: continue
                    raise AssertionError('bad TLS trust/hostname accepted')
            test('wrong TLS trust and hostname rejected',wrong_tls)
            test('untrusted actual RTMPS publisher rejected',lambda:h.rejected(tls=True,trusted=False))
            def tls_media():
                h.alice=h.publish(tls=True); wait(lambda:h.ready('live/alice')); h.read('alice')
            test('trusted RTMPS publish with RTSP audio video decode',tls_media)
            def tls_reset():
                h.change('reset'); wait(lambda:h.alice.poll() is not None)
                h.rejected(tls=True,value=h.old_password)
                h.alice=h.publish(tls=True); wait(lambda:h.ready('live/alice'))
            test('RTMPS credential reset revokes live connection',tls_reset)
            def tls_revocation_without_renewal_file():
                cert = h.store.path / h.service.settings['certificate']
                original = cert.read_bytes()
                cert.unlink()
                try:
                    try:
                        h.service.up()
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('production startup accepted missing certificate')
                    h.change('disable')
                    wait(lambda: h.alice.poll() is not None)
                    wait(lambda: not h.ready('live/alice'))
                    h.rejected(tls=True)
                finally:
                    atomic_write(cert, original)
                h.change('enable')
                h.alice = h.publish(tls=True)
                wait(lambda: h.ready('live/alice'))
                h.read('alice')
            test('missing renewal file does not block revocation, startup still denied', tls_revocation_without_renewal_file)
            def certificate_update():
                old = (h.store.path/'certs/server.crt').read_bytes()
                with tempfile.TemporaryDirectory(prefix='ciallochat-new-cert-') as renewed:
                    subprocess.run([str(ROOT/'scripts/make-test-cert.sh'),renewed],check=True,stdout=subprocess.DEVNULL)
                    for name in ('server.crt','server.key','ca.crt'):
                        atomic_write(h.store.path/'certs'/name,(Path(renewed)/name).read_bytes())
                assert old != (h.store.path/'certs/server.crt').read_bytes()
                h.service.reload_certificate()
                wait(lambda:h.alice.poll() is not None)
                h.alice=h.publish(tls=True); wait(lambda:h.ready('live/alice')); h.read('alice')
            test('certificate update verified on TLS socket and media reconnected',certificate_update)
            def backup_restore_media():
                if not args.mediamtx:
                    report.setdefault('deployment', []).append(h.deployment_state())
                expected = h.store.read('accounts.json')
                destination = h.store.path / 'media-backup.json'
                with h.store.lock():
                    backup(h.store, destination)
                saved = json.loads(destination.read_text())
                assert 'certificates' not in saved, 'default backup included private keys'
                assert all(value not in destination.read_text() for value in list(h.passwords.values()) + list(h.read_keys.values())), 'backup included media key'
                h.stop_service()
                h.change('disable')
                assert not h.store.read('accounts.json')['users'][0]['enabled']
                with h.store.lock():
                    restore(h.store, destination)
                assert h.store.read('accounts.json') == expected
                h.start()
                h.alice = h.publish(tls=True)
                wait(lambda: h.ready('live/alice'))
                h.read('alice')
            test('production backup restore preserves credentials and playable media', backup_restore_media)
            def deletion():
                h.change('delete'); wait(lambda:h.alice.poll() is not None)
                h.rejected(tls=True)
                h.rejected_read(key=h.read_keys['alice'],removed=True)
            test('delete terminates live publisher and rejects reconnect',deletion)
        except BaseException:
            status=1
            if h and args.mediamtx and h.log.exists():
                log = h.log.read_text()[-6000:]
                for value in list(h.passwords.values()) + list(h.read_keys.values()): log = log.replace(value,'[REDACTED]')
                report['server_tail'] = log
                import re
                publisher_logs=sorted(h.store.path.glob('publisher-*.log'),key=lambda p:p.stat().st_mtime)
                if publisher_logs:
                    client=publisher_logs[-1].read_text()[-2000:]
                    report['client_tail']=re.sub(r'rtmps?://[^\s]+','[REDACTED URL]',client)
            if not report['tests'] or report['tests'][-1]['status'] != 'failed':
                report['tests'].append({'name':'harness','status':'failed'})
        finally:
            if h:
                try:h.cleanup()
                except Exception as exc:
                    status=1; report['tests'].append({'name':'cleanup','status':'failed','error':type(exc).__name__})
    report['result']='passed' if status == 0 else 'failed'
    atomic_write(Path(args.report),dump(report))
    print('Report: ' + str(Path(args.report).resolve()),flush=True)
    return status


if __name__ == '__main__': sys.exit(main())
