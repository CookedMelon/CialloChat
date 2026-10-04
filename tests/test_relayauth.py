import time
import unittest
import urllib.parse
from unittest.mock import patch

from streamctl.relayauth import reader_request, signed_query


class RelayAuthentication(unittest.TestCase):
    def request(self):
        query = signed_query({'read_key': ['user-key']}, '203.0.113.7', 'live/alice', b's'*32)
        return dict(protocol='rtsp', action='read', ip='127.0.0.1', path='live/alice',
                    query=urllib.parse.urlencode(query, doseq=True))

    def test_signature_binds_peer_path_and_client_key(self):
        request = self.request()
        result = reader_request(request, b's'*32)
        self.assertEqual(result['ip'], '203.0.113.7')
        self.assertEqual(result['query'], 'read_key=user-key')
        for changes in [dict(ip='203.0.113.1'), dict(path='live/bob'), dict(action='publish'),
                        dict(query=request['query'].replace('user-key', 'another-key')),
                        dict(query=request['query'].replace('203.0.113.7', '203.0.113.8'))]:
            with self.assertRaises(ValueError):
                reader_request(dict(request, **changes), b's'*32)
        with patch('streamctl.relayauth.time.time', return_value=time.time()+61):
            with self.assertRaises(ValueError):
                reader_request(request, b's'*32)

    def test_unsigned_direct_requests_keep_their_actual_peer(self):
        request = dict(ip='203.0.113.7', query='read_key=user-key')
        self.assertIs(reader_request(request, b's'*32), request)
