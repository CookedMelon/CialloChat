import asyncio
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from streamctl.accounts import credentials
from streamctl.config import Store, public_urls, render, validate_settings
from streamctl.relayauth import reader_request
from streamctl.rtspbuffer import Session, proxy_peer


class ProxyConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.initialize()
        self.settings, self.accounts, self.control = self.store.load()
        self.settings.update(mode='production', service_backend='systemd', rtsp_buffer_ms=1000,
                             reverse_proxy_enabled=True, hostname='chat.v50to.cc',
                             read_hostname='watch.v50to.cc', test_video_enabled=True)

    def test_public_urls_credentials_and_backend_configuration(self):
        validate_settings(self.settings)
        urls = public_urls(self.settings)
        self.assertEqual(urls, dict(publish_base='rtmps://chat.v50to.cc',
                                   read_base='rtsp://watch.v50to.cc', test_url='rtsp://watch.v50to.cc/test'))
        keys = credentials(self.settings, 'alice', 'push&key-123456', 'pull&key-123456')
        for field, base in [('publish_url', urls['publish_base']), ('read_url', urls['read_base']),
                            ('vrchat_read_url', urls['read_base'])]:
            self.assertTrue(keys[field].startswith(base + '/live/alice?'))
        self.assertEqual(urllib.parse.urlsplit(keys['legacy_read_url']).hostname, 'watch.v50to.cc')
        config = yaml.safe_load(render(self.settings, self.accounts, self.control))
        self.assertEqual(config['rtmpsAddress'], '127.0.0.1:1936')
        self.assertEqual(config['rtspAddress'], '127.0.0.1:18554')
        self.assertEqual(config['rtmpTrustedProxies'], ['127.0.0.1/32'])

    def test_custom_public_ports_ipv6_and_invalid_configuration(self):
        settings = dict(self.settings, public_rtmps_port=2443, public_rtsp_port=1554,
                        hostname='::1', read_hostname='::1')
        urls = public_urls(settings)
        self.assertEqual(urls['publish_base'], 'rtmps://[::1]:2443')
        self.assertEqual(urls['read_base'], 'rtsp://[::1]:1554')
        for changes in [dict(mode='local'), dict(service_backend='docker'), dict(rtsp_buffer_ms=0),
                        dict(reverse_proxy_enabled='yes'), dict(public_rtsp_port=True),
                        dict(public_rtmps_port=0), dict(read_hostname='watch.example/a')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_settings(dict(self.settings, **changes))


class ProxyHeaders(unittest.IsolatedAsyncioTestCase):
    async def read_header(self, header, peer=('127.0.0.1', 1234)):
        reader = asyncio.StreamReader()
        reader.feed_data(header)
        reader.feed_eof()
        return await proxy_peer(reader, peer)

    async def test_ipv4_ipv6_and_unconsumed_rtsp_request(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'PROXY TCP4 203.0.113.8 120.55.167.91 54321 554\r\nOPTIONS * RTSP/1.0\r\n')
        self.assertEqual(await proxy_peer(reader, ('127.0.0.1', 1234)), ('203.0.113.8', 54321))
        self.assertEqual(await reader.readline(), b'OPTIONS * RTSP/1.0\r\n')
        self.assertEqual(await self.read_header(b'PROXY TCP6 2001:db8::1 ::1 1234 554\r\n'),
                         ('2001:db8::1', 1234))

    async def test_untrusted_malformed_and_oversized_headers_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.read_header(b'PROXY TCP4 203.0.113.8 127.0.0.1 1234 554\r\n', ('203.0.113.9', 1))
        for header in [b'PROXY UNKNOWN\r\n', b'PROXY TCP4 ::1 ::1 1 554\r\n',
                       b'PROXY TCP4 203.0.113.8 127.0.0.1 0 554\r\n', b'x' * 108,
                       b'PROXY TCP4 203.0.113.8 127.0.0.1 1 65536\r\n']:
            with self.subTest(header=header), self.assertRaises(ValueError):
                await self.read_header(header)
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.read_header(b'PROXY TCP4')

    async def test_public_authority_peer_signature_and_test_quota(self):
        channel = SimpleNamespace(acquire=Mock())
        async def acquire(ip, connection):
            channel.acquire(ip, connection)
            return SimpleNamespace(ip=ip, token='a' * 32)
        config = SimpleNamespace(public_hosts={'watch.v50to.cc'}, listen_port=8554, public_port=554,
                                 upstream_host='127.0.0.1', upstream_port=18554, buffer_ms=1000,
                                 max_buffer_bytes=1024, proxy_secret=b's' * 32,
                                 test_channel=SimpleNamespace(acquire=acquire))
        session = Session(config, None, None, lambda **kw: None, peer=('203.0.113.8', 1234))
        incoming = 'rtsp://watch.v50to.cc/live/alice?read_key=reader-key-123'
        mapped = urllib.parse.urlsplit(session.map_url(incoming, True))
        verified = reader_request(dict(action='read', protocol='rtsp', ip='127.0.0.1',
                                       path='live/alice', query=mapped.query), b's' * 32)
        self.assertEqual(verified['ip'], '203.0.113.8')
        self.assertEqual(session.map_url('rtsp://127.0.0.1:18554/live/alice/trackID=0', False),
                         'rtsp://watch.v50to.cc/live/alice/trackID=0')
        session.map_url('rtsp://watch.v50to.cc:554/live/alice', True)
        for url in ['rtsp://watch.v50to.cc:8554/live/alice', 'rtsp://chat.v50to.cc/live/alice']:
            with self.assertRaises(ValueError):
                session.map_url(url, True)
        test = Session(config, None, None, lambda **kw: None, peer=('203.0.113.9', 1234))
        await test.prepare_test('rtsp://watch.v50to.cc/test', 'DESCRIBE')
        channel.acquire.assert_called_once_with('203.0.113.9', test.id)


if __name__ == '__main__':
    unittest.main()
