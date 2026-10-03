import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from streamctl.accounts import new_account, read_identity, hash_password
from streamctl.authserver import AdmissionServer, Policy
from streamctl.config import Store, render, atomic_write


class URLAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'instance')
        self.store.initialize()
        self.s, self.a, self.c = self.store.load()
        self.publish = 'publisher-key-123456'
        self.read = 'viewer-key&#+%/中文123'
        self.a['users'] = [new_account('alice', self.publish, self.read),
                           new_account('bob', 'bob-publish-secret-123', 'bob-viewer-secret-456')]
        self.file = self.store.path/'mediamtx/mediamtx.yml'
        self.save()
        self.policy = Policy(self.file)

    def save(self):
        atomic_write(self.file, render(self.s, self.a, self.c))

    def request(self, **changes):
        data = dict(action='read', path='live/alice', protocol='rtsp', user='', password='',
                    query=urllib.parse.urlencode({'read_key':self.read}), ip='127.0.0.1')
        data.update(changes)
        return data

    def test_url_key_requires_no_username_or_authorization(self):
        self.assertTrue(self.policy.authorize(self.request()))
        self.assertFalse(self.policy.authorize(self.request(query='')))
        self.assertFalse(self.policy.authorize(self.request(query='read_key=')))
        self.assertFalse(self.policy.authorize(self.request(query='read_key=wrong-key-1234')))
        self.assertFalse(self.policy.authorize(self.request(query='read_key='+self.publish)))

    def test_query_key_cannot_cross_path_publish_use_other_protocol_or_access_api(self):
        for changes in ({'path':'live/bob'}, {'path':'live/alice/trackID=0'}, {'path':'live/../alice'},
                        {'action':'publish'}, {'action':'api'}, {'protocol':'rtmp'},
                        {'query':self.request()['query']+'&read_key='+self.read},
                        {'query':'read_key=%FF'}, {'query':'bad-query'}, {'query':None}):
            with self.subTest(changes=changes):
                self.assertFalse(self.policy.authorize(self.request(**changes)))

    def test_key_reset_disable_delete_and_bad_policy_invalidate_cached_success(self):
        self.assertTrue(self.policy.authorize(self.request()))
        self.a['users'][0]['read_key_hash'] = hash_password('new-viewer-secret-123')
        self.save()
        self.assertFalse(self.policy.authorize(self.request()))
        new = self.request(query='read_key=new-viewer-secret-123')
        self.assertTrue(self.policy.authorize(new))
        self.a['users'][0]['enabled'] = False
        self.save()
        self.assertFalse(self.policy.authorize(new))
        self.a['users'][0]['enabled'] = True
        self.save()
        self.assertTrue(self.policy.authorize(new))
        self.a['users'] = self.a['users'][1:]
        self.save()
        self.assertFalse(self.policy.authorize(new))
        atomic_write(self.file, '{bad yaml')
        self.assertFalse(self.policy.healthy())
        self.assertFalse(self.policy.authorize(new))

    def test_existing_publish_control_and_basic_read_are_still_scoped(self):
        self.assertTrue(self.policy.authorize(self.request(action='publish', protocol='rtmp',
                        query='', user='alice', password=self.publish)))
        self.assertFalse(self.policy.authorize(self.request(action='publish', protocol='rtmp',
                        query='', user='alice', password=self.read)))
        self.assertTrue(self.policy.authorize(self.request(query='', user=read_identity('alice'), password=self.read)))
        self.assertTrue(self.policy.authorize(self.request(action='api', path='', protocol='',
                        query='', user=self.c['username'], password=self.c['password'])))
        self.assertFalse(self.policy.authorize(self.request(action='api', path='', protocol='',
                        query='', user='alice', password=self.publish)))
        self.assertFalse(self.policy.authorize(self.request(action='api', path='', protocol='',
                        query='', user=read_identity('alice'), password=self.read)))
        # An invalid URL key cannot be bypassed by supplying a valid header.
        self.assertFalse(self.policy.authorize(self.request(query='read_key=bad-key-12345',
                        user=read_identity('alice'), password=self.read)))

    def test_http_callback_is_closed_on_invalid_bodies_and_never_echoes_credentials(self):
        server = AdmissionServer(('127.0.0.1', 0), self.policy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f'http://127.0.0.1:{server.server_port}'
        with opener.open(url+'/health') as response:
            self.assertEqual(response.status, 200)
        for body, expected in [(json.dumps(self.request()).encode(), 200),
                               (json.dumps(self.request(query='')).encode(), 403),
                               (b'{malformed', 400), (b'[]', 403), (b'x'*16385, 400)]:
            request = urllib.request.Request(url+'/auth', body, {'Content-Type':'application/json'})
            try:
                response = opener.open(request)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                self.assertEqual(response.status, expected)
                self.assertEqual(response.read(), b'')
                self.assertIsNone(response.headers.get('WWW-Authenticate'))


if __name__ == '__main__':
    unittest.main()
