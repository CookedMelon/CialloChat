import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
from streamctl.accounts import new_account, matches, read_identity
from streamctl.config import Store, atomic_write, dump, render, VERSION
from streamctl.service import Service, migrate, restore, commit, recover


class WatchKeys(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'instance')
        self.store.initialize()
        self.s, self.a, self.c = self.store.load()
        self.a['users'].append(new_account('alice', 'publisher-secret-123', 'reader-secret-456'))
        atomic_write(self.store.path/'accounts.json', dump(self.a))
        atomic_write(self.store.path/'mediamtx/mediamtx.yml', render(self.s, self.a, self.c))

    def legacy(self):
        old = copy.deepcopy(self.a)
        old['schema'] = 1
        for u in old['users']:
            u['password_hash'] = u.pop('publish_key_hash')
            u.pop('read_key_hash')
        return old

    def test_separate_scoped_permissions_and_no_anonymous_reader(self):
        cfg = yaml.safe_load(render(self.s, self.a, self.c))
        users = {u['user']:u for u in cfg['authInternalUsers']}
        self.assertNotIn('any', users)
        self.assertEqual(users['alice']['permissions'], [{'action':'publish','path':'live/alice'}])
        self.assertEqual(users[read_identity('alice')]['permissions'], [{'action':'read','path':'live/alice'}])
        self.assertNotEqual(users['alice']['pass'], users[read_identity('alice')]['pass'])
        self.a['users'][0]['enabled'] = False
        names = {u['user'] for u in yaml.safe_load(render(self.s,self.a,self.c))['authInternalUsers']}
        self.assertNotIn('alice', names)
        self.assertNotIn(read_identity('alice'), names)
        with self.assertRaises(ValueError):
            new_account('ciallochat-read-alice', 'publisher-secret-123')
        with self.assertRaises(ValueError):
            new_account('bob', 'identical-secret-123', 'identical-secret-123')
        with self.assertRaises(ValueError):
            new_account('bob', 'publisher-secret-123', '')

    def test_read_only_revocation_preserves_publisher_and_other_path(self):
        service = Service(self.store)
        connections = [
            {'id':'pub','path':'live/alice','state':'publish'},
            {'id':'old-reader','path':'live/alice','state':'read'},
            {'id':'bob-reader','path':'live/bob','state':'read'},
        ]
        def api(endpoint, method='GET'):
            if '/kick/' in endpoint:
                identifier = endpoint.split('/')[-1]
                connections[:] = [x for x in connections if x['id'] != identifier]
                return None
            return {'items':copy.deepcopy(connections)} if endpoint.startswith('rtsp/') else {'items':[]}
        service.api = api
        service.kick({'live/alice'}, states=('read',))
        self.assertEqual({x['id'] for x in connections}, {'pub','bob-reader'})

    def test_read_revocation_recovery_keeps_scope(self):
        service = Service(self.store)
        service.running = lambda: True
        service.wait_loaded = lambda value: None
        service.kick = lambda *a,**kw: (_ for _ in ()).throw(OSError('unavailable'))
        with self.assertRaisesRegex(RuntimeError, '撤销未完成'):
            commit(self.store, self.a, revoke=['live/alice'], revoke_states=('read',), service=service)
        pending = json.loads((self.store.path/'pending-revocations.json').read_text())
        self.assertEqual(pending['states'], ['read'])
        with patch.object(Service, 'running', return_value=True), patch.object(Service, 'kick') as kick:
            recover(self.store)
            kick.assert_called_once_with({'live/alice'}, states=['read'])

    def test_legacy_requires_explicit_offline_migration_and_preserves_publish_key(self):
        old = self.legacy()
        atomic_write(self.store.path/'accounts.json', dump(old))
        atomic_write(self.store.path/'mediamtx/mediamtx.yml', render(self.s,old,self.c,legacy_validation=True))
        with self.assertRaisesRegex(ValueError, '迁移'):
            self.store.load()
        target = Path(self.temp.name)/'handoff.json'
        with patch.object(Service, 'running', return_value=True), self.assertRaisesRegex(ValueError, 'down'):
            migrate(self.store,target)
        self.assertFalse(target.exists())
        with patch.object(Service, 'running', return_value=False):
            migrate(self.store,target)
        updated = self.store.load()[1]
        self.assertEqual(updated['users'][0]['publish_key_hash'], old['users'][0]['password_hash'])
        exported = json.loads(target.read_text())[0]
        self.assertNotIn('publish_key', exported)
        self.assertTrue(matches(updated['users'][0]['read_key_hash'], exported['read_key']))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('any', {u['user'] for u in yaml.safe_load((self.store.path/'mediamtx/mediamtx.yml').read_text())['authInternalUsers']})

    def test_interrupted_migration_never_restores_anonymous_read(self):
        old = self.legacy()
        atomic_write(self.store.path/'accounts.json', dump(old))
        atomic_write(self.store.path/'mediamtx/mediamtx.yml', render(self.s,old,self.c,legacy_validation=True))
        target = Path(self.temp.name)/'handoff.json'
        failed = False
        def write(path, content, *args, **kw):
            nonlocal failed
            if Path(path).name == 'accounts.json' and not failed:
                failed = True
                raise OSError('simulated write failure')
            return atomic_write(path, content, *args, **kw)
        with patch.object(Service, 'running', return_value=False), patch('streamctl.service.atomic_write', side_effect=write):
            with self.assertRaises(OSError): migrate(self.store,target)
        with self.assertRaisesRegex(ValueError, '迁移'): self.store.load()
        config = yaml.safe_load((self.store.path/'mediamtx/mediamtx.yml').read_text())
        self.assertFalse(any(p['action'] == 'read' for u in config['authInternalUsers'] for p in u['permissions']))
        self.assertFalse(target.exists())

    def test_old_backup_requires_handoff_and_restores_closed_policy(self):
        old = self.legacy()
        data = dict(version=VERSION,settings=self.s,accounts=old,control=self.c,
                    config=render(self.s,old,self.c,legacy_validation=True))
        source = Path(self.temp.name)/'old-backup.json'
        atomic_write(source,dump(data))
        before = (self.store.path/'accounts.json').read_bytes()
        with patch.object(Service,'running',return_value=False):
            with self.assertRaisesRegex(ValueError,'migrate-credentials-file'): restore(self.store,source)
            self.assertEqual(before,(self.store.path/'accounts.json').read_bytes())
            target = Path(self.temp.name)/'restored-read-keys.json'
            restore(self.store,source,target)
        updated = self.store.load()[1]
        self.assertEqual(updated['schema'],2)
        self.assertEqual(updated['users'][0]['publish_key_hash'],old['users'][0]['password_hash'])
        self.assertTrue(matches(updated['users'][0]['read_key_hash'],json.loads(target.read_text())[0]['read_key']))
        self.assertNotIn('any',{u['user'] for u in yaml.safe_load((self.store.path/'mediamtx/mediamtx.yml').read_text())['authInternalUsers']})


if __name__ == '__main__': unittest.main()
