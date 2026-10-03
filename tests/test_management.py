import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
from streamctl.accounts import new_account, validate_accounts, HASHER
from streamctl.config import Store, render, atomic_write, dump
from streamctl.service import commit, backup, restore, recover


class Offline:
    def running(self): return False


class Live:
    def __init__(self): self.loaded = []; self.kicked = []
    def running(self): return True
    def wait_loaded(self, config): self.loaded.append(config)
    def kick(self, paths, states=('publish',)): self.kicked.extend(paths)


class Management(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.initialize()

    def add(self, name='alice', pwd='A-good-password&#+'):
        s, a, c = self.store.load()
        a['users'].append(new_account(name, pwd))
        commit(self.store, a, service=Offline())
        return self.store.load()

    def test_hash_and_path_isolation(self):
        s, a, c = self.add()
        a['users'].append(new_account('bob', 'A-good-password&#+'))
        self.assertNotEqual(a['users'][0]['publish_key_hash'], a['users'][1]['publish_key_hash'])
        self.assertTrue(HASHER.verify(a['users'][0]['publish_key_hash'][7:], 'A-good-password&#+'))
        conf = yaml.safe_load(render(s, a, c))
        self.assertEqual(conf['paths'], {'live/alice': {}, 'live/bob': {}})
        self.assertEqual(next(u for u in conf['authInternalUsers'] if u['user']=='alice')['permissions'], [{'action':'publish','path':'live/alice'}])
        self.assertFalse(conf['pathDefaults']['overridePublisher'])
        a['users'][0]['enabled'] = False
        self.assertNotIn('alice', [u['user'] for u in yaml.safe_load(render(s,a,c))['authInternalUsers']])

    def test_names_and_duplicate_validation(self):
        for name in ('any', 'ANY', '', '../a', '~.*', 'a/b', '你好', 'admin', 'a b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                new_account(name, 'A-good-password')
        s,a,c = self.add()
        a['users'].append(copy.deepcopy(a['users'][0]))
        with self.assertRaises(ValueError): validate_accounts(a)

    def test_initialize_preserves_state(self):
        self.add()
        atomic_write(self.store.path / 'certs/keep.key', 'keep')
        before = {p.name:p.read_bytes() for p in self.store.path.glob('*.json')}
        self.store.initialize('production')
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.store.path.glob('*.json')})
        self.assertEqual((self.store.path / 'certs/keep.key').read_text(), 'keep')

    def test_hot_apply_and_revoke(self):
        s,a,c = self.add()
        live = Live()
        a['users'][0]['enabled'] = False
        commit(self.store, a, revoke=['live/alice'], service=live)
        self.assertEqual(len(live.loaded), 1)
        self.assertEqual(live.kicked, ['live/alice'])
        self.assertFalse(self.store.load()[1]['users'][0]['enabled'])

    def test_production_revocation_does_not_require_renewal_files(self):
        s, a, c = self.add()
        s['mode'] = 'production'
        atomic_write(self.store.path / 'settings.json', dump(s))
        atomic_write(self.store.path / 'mediamtx/mediamtx.yml', render(s, a, c))
        a['users'][0]['enabled'] = False
        live = Live()
        with patch('streamctl.service.check_tls', side_effect=ValueError('missing certificate')) as check:
            commit(self.store, a, revoke=['live/alice'], service=live)
            check.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'missing certificate'):
                commit(self.store, settings=s, service=live)
        self.assertEqual(live.kicked, ['live/alice'])
        self.assertFalse(self.store.load()[1]['users'][0]['enabled'])
        self.assertEqual(yaml.safe_load(live.loaded[0])['rtmpEncryption'], 'strict')

    def test_rollback_on_reload_failure(self):
        self.add()
        before = {name:(self.store.path/name).read_bytes() for name in ('accounts.json','settings.json','mediamtx/mediamtx.yml')}
        live = Live()
        def load(config):
            if not live.loaded:
                live.loaded.append(config)
                raise RuntimeError('rejected')
            live.loaded.append(config)
        live.wait_loaded = load
        a = self.store.load()[1]; a['users'][0]['enabled'] = False
        with self.assertRaises(RuntimeError): commit(self.store,a,service=live)
        for name,value in before.items(): self.assertEqual(value,(self.store.path/name).read_bytes())
        self.assertFalse((self.store.path/'transaction.json').exists())

    def test_failed_revoke_retains_new_policy_and_reports(self):
        self.add()
        live = Live()
        def fail(paths): raise OSError('API failure')
        live.kick = fail
        a = self.store.load()[1]; a['users'][0]['enabled'] = False
        with self.assertRaisesRegex(RuntimeError, '撤销未完成'):
            commit(self.store,a,revoke=['live/alice'],service=live)
        self.assertFalse(self.store.load()[1]['users'][0]['enabled'])
        self.assertTrue((self.store.path/'pending-revocations.json').exists())
        with patch('streamctl.service.Service',return_value=Live()) as mock:
            recover(self.store)
            self.assertEqual(mock.return_value.kicked,['live/alice'])
        self.assertFalse((self.store.path/'pending-revocations.json').exists())

    def test_down_remains_available_after_failed_revocation(self):
        from streamctl.cli import execute, parser
        atomic_write(self.store.path/'pending-revocations.json', dump({'paths':['live/alice']}))
        args = parser().parse_args(['--runtime',str(self.store.path),'down'])
        with patch('streamctl.cli.Service') as mocked:
            self.assertEqual(execute(args), 0)
            mocked.return_value.down.assert_called_once()
        self.assertFalse((self.store.path/'pending-revocations.json').exists())

    def test_backup_restore_validation(self):
        s,a,c = self.add()
        atomic_write(self.store.path/'certs/secret.key','secret')
        target = self.store.path/'snapshot.json'
        backup(self.store,target)
        data = json.loads(target.read_text())
        self.assertNotIn('certificates',data)
        self.assertNotIn('A-good-password&#+',target.read_text())
        self.assertEqual(target.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError): backup(self.store,target)
        with patch('streamctl.service.Service.running',return_value=False):
            restore(self.store,target)
        self.assertEqual(self.store.load()[1],a)
        data['version']['mediamtx'] = 'wrong'
        atomic_write(target,dump(data))
        with self.assertRaises(ValueError): restore(self.store,target)

    def test_crash_journal_recovery(self):
        s,a,c = self.add()
        previous = dict(settings=s, accounts=a, config=render(s,a,c))
        atomic_write(self.store.path/'transaction.json',dump(previous))
        atomic_write(self.store.path/'accounts.json',dump({'schema':2,'users':[]}))
        with patch('streamctl.service.Service.running',return_value=False): recover(self.store)
        self.assertEqual(self.store.load()[1],a)


if __name__ == '__main__': unittest.main()
