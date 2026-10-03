import concurrent.futures
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from streamctl.config import ROOT


class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)/'instance'
        self.env = os.environ.copy()
        self.env['PYTHONPATH'] = str(ROOT/'src')
        self.run_cli('init')

    def run_cli(self,*args,input=None,expect=0):
        p = subprocess.run([str(ROOT/'.venv/bin/python'),'-m','streamctl.cli','--runtime',str(self.runtime),*args],
                           input=input,capture_output=True,text=True,env=self.env,timeout=30)
        self.assertEqual(p.returncode,expect,p.stderr)
        return p.stdout

    def test_bulk_is_atomic_and_no_plaintext_saved(self):
        self.run_cli('user','import','-',input=json.dumps([{'username':'alice','password':'password-alpha-123','read_key':'read-key-alpha-123'}]))
        before=(self.runtime/'accounts.json').read_bytes()
        self.run_cli('user','import','-',input=json.dumps([{'username':'bob','password':'password-beta-123','read_key':'read-key-beta-123'},
                     {'username':'alice','password':'password-alpha-123','read_key':'read-key-alpha-123'}]),expect=1)
        self.assertEqual(before,(self.runtime/'accounts.json').read_bytes())
        self.assertNotIn('password-alpha-123',(self.runtime/'accounts.json').read_text())
        listed=self.run_cli('user','list')
        self.assertNotIn('password',listed)
        self.assertNotIn('argon2',listed)

    def test_url_encoding_and_explicit_credentials(self):
        value='secret&with#%+/:?编码'
        credentials=json.loads(self.run_cli('user','add','alice','--password-stdin',input=value+'\n'))
        from urllib.parse import urlparse,parse_qs
        self.assertEqual(parse_qs(urlparse(credentials['publish_url']).query)['pass'],[value])
        self.assertNotIn('?',credentials['read_url'])
        read=json.loads(self.run_cli('user','credentials','alice','--password-stdin',input=value+'\n'))
        self.assertEqual(read['publish_url'],credentials['publish_url'])
        self.assertNotIn('read_url',read)
        watched=json.loads(self.run_cli('user','credentials','alice','--kind','read','--password-stdin',input=credentials['read_key']+'\n'))
        self.assertEqual(watched['read_url'],credentials['read_url'])
        self.assertNotIn('publish_key',watched)
        self.run_cli('user','credentials','alice','--password-stdin',input='wrong-password-value\n',expect=1)

    def test_read_key_reset_is_independent_and_urls_encode_secrets(self):
        publish = 'publisher-special&#+%123'
        read = 'reader-special&#+%/中文123'
        initial = json.loads(self.run_cli('user','add','alice','--password-stdin','--read-key-stdin',input=publish+'\n'+read+'\n'))
        from urllib.parse import urlparse, unquote
        self.assertEqual(unquote(urlparse(initial['read_url']).password),read)
        self.assertNotIn(publish,initial['read_url'])
        before = json.loads((self.runtime/'accounts.json').read_text())['users'][0]
        changed = json.loads(self.run_cli('user','reset-read-key','alice'))
        after = json.loads((self.runtime/'accounts.json').read_text())['users'][0]
        self.assertEqual(before['publish_key_hash'],after['publish_key_hash'])
        self.assertNotEqual(before['read_key_hash'],after['read_key_hash'])
        self.assertNotIn('publish_key',changed)
        self.run_cli('user','credentials','alice','--kind','read','--password-stdin',input=read+'\n',expect=1)
        self.run_cli('user','credentials','alice','--kind','read','--password-stdin',input=changed['read_key']+'\n')
        self.run_cli('user','reset-read-key','alice','--password-stdin',input=publish+'\n',expect=1)
        self.assertEqual(after,json.loads((self.runtime/'accounts.json').read_text())['users'][0])
        self.run_cli('user','reset-publish-key','alice')
        final = json.loads((self.runtime/'accounts.json').read_text())['users'][0]
        self.assertEqual(after['read_key_hash'],final['read_key_hash'])

    def test_generated_bulk_keys_require_exclusive_handoff(self):
        incoming = json.dumps([{'username':'alice'}])
        self.run_cli('user','import','-',input=incoming,expect=1)
        self.assertEqual(json.loads(self.run_cli('user','list')),[])
        target = Path(self.tmp.name)/'handoff.json'
        self.run_cli('user','import','-','--credentials-file',str(target),input=incoming)
        handoff = json.loads(target.read_text())[0]
        self.assertNotEqual(handoff['publish_key'],handoff['read_key'])
        self.assertEqual(target.stat().st_mode & 0o777,0o600)
        self.run_cli('user','import','-','--credentials-file',str(target),input=json.dumps([{'username':'bob'}]),expect=1)
        self.assertEqual([u['username'] for u in json.loads(self.run_cli('user','list'))],['alice'])

    def test_concurrent_writers_preserve_both_users(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(self.run_cli,'user','add',name) for name in ('alice','bob')]
            for future in futures:future.result()
        self.assertEqual({u['username'] for u in json.loads(self.run_cli('user','list'))},{'alice','bob'})

    def test_log_follow_does_not_block_account_revocation(self):
        self.run_cli('user', 'add', 'alice')
        fake_bin = Path(self.tmp.name) / 'bin'
        fake_bin.mkdir()
        marker = Path(self.tmp.name) / 'following'
        docker = fake_bin / 'docker'
        docker.write_text(
            f'#!{ROOT / ".venv/bin/python"}\n'
            'import os, signal, sys\n'
            'from pathlib import Path\n'
            'if "logs" in sys.argv:\n'
            '    Path(os.environ["CIALLOCHAT_TEST_LOG_MARKER"]).touch()\n'
            '    signal.pause()\n'
        )
        docker.chmod(0o700)
        self.env['PATH'] = str(fake_bin) + os.pathsep + self.env['PATH']
        self.env['CIALLOCHAT_TEST_LOG_MARKER'] = str(marker)
        follower = subprocess.Popen(
            [str(ROOT / '.venv/bin/python'), '-m', 'streamctl.cli',
             '--runtime', str(self.runtime), 'logs', '--follow'],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and follower.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marker.exists(), 'log follower did not start')
            revoked = subprocess.run(
                [str(ROOT / '.venv/bin/python'), '-m', 'streamctl.cli',
                 '--runtime', str(self.runtime), 'user', 'disable', 'alice'],
                env=self.env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(revoked.returncode, 0, revoked.stderr)
            self.assertIsNone(follower.poll(), 'log follower ended unexpectedly')
            self.assertFalse(json.loads(self.run_cli('user', 'list'))[0]['enabled'])
        finally:
            if follower.poll() is None:
                os.killpg(follower.pid, signal.SIGTERM)
            follower.wait(timeout=5)

    def test_password_input_does_not_block_account_revocation(self):
        self.run_cli('user', 'add', 'alice')
        waiting = subprocess.Popen(
            [str(ROOT / '.venv/bin/python'), '-m', 'streamctl.cli',
             '--runtime', str(self.runtime), 'user', 'add', 'bob', '--password-stdin'],
            env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 5
            pipe_wait = False
            while waiting.poll() is None and time.monotonic() < deadline:
                if 'pipe_read' in Path(f'/proc/{waiting.pid}/wchan').read_text():
                    pipe_wait = True
                    break
                time.sleep(.05)
            self.assertTrue(pipe_wait, 'CLI was not waiting for password input')
            revoked = subprocess.run(
                [str(ROOT / '.venv/bin/python'), '-m', 'streamctl.cli',
                 '--runtime', str(self.runtime), 'user', 'disable', 'alice'],
                env=self.env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(revoked.returncode, 0, revoked.stderr)
            self.assertIsNone(waiting.poll())
            stdout, stderr = waiting.communicate('new-bob-password-123\n', timeout=5)
            self.assertEqual(waiting.returncode, 0, stderr)
            users = json.loads(self.run_cli('user', 'list'))
            self.assertEqual({u['username']: u['enabled'] for u in users}, {'alice': False, 'bob': True})
        finally:
            if waiting.poll() is None:
                waiting.terminate()
                waiting.communicate(timeout=5)

    def test_import_permissions_and_idempotent_setup_check(self):
        file=Path(self.tmp.name)/'users.json'
        file.write_text(json.dumps([{'username':'alice','password':'password-alpha-123','read_key':'read-key-alpha-123'}]));file.chmod(0o644)
        self.run_cli('user','import',str(file),expect=1)
        self.assertEqual(json.loads(self.run_cli('user','list')),[])
        file.chmod(0o600); self.run_cli('user','import',str(file))
        before={p.relative_to(self.runtime):p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()}
        env=self.env.copy();env['CIALLOCHAT_RUNTIME']=str(self.runtime)
        for _ in range(2):
            p=subprocess.run([str(ROOT/'setup.sh'),'--check'],env=env,capture_output=True,timeout=30)
            self.assertIn(p.returncode,(0,1))
            self.assertEqual(before,{p.relative_to(self.runtime):p.read_bytes() for p in self.runtime.rglob('*') if p.is_file()})

    def test_setup_reports_socket_permission_without_attempting_install(self):
        fake_bin = Path(self.tmp.name) / 'setup-bin'
        fake_bin.mkdir()
        docker = fake_bin / 'docker'
        docker.write_text('#!/bin/sh\necho "permission denied while trying to connect to unix:///var/run/docker.sock" >&2\nexit 1\n')
        query = fake_bin / 'dpkg-query'
        query.write_text('#!/bin/sh\nprintf "install ok installed"\n')
        forbidden = fake_bin / 'sudo'
        marker = Path(self.tmp.name) / 'unexpected-install'
        forbidden.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
        for file in (docker, query, forbidden):
            file.chmod(0o700)
        env = self.env.copy()
        env['PATH'] = str(fake_bin) + os.pathsep + env['PATH']
        result = subprocess.run([str(ROOT / 'setup.sh'), '--mode', 'local'],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Docker socket 访问被拒绝', result.stderr)
        self.assertIn('newgrp docker', result.stderr)
        self.assertNotIn('WSL Integration 启用', result.stderr)
        self.assertFalse(marker.exists(), 'setup attempted a privileged installation')


if __name__=='__main__':unittest.main()
