import asyncio
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock


spec = importlib.util.spec_from_file_location(
    'local_tls_relay', Path(__file__).resolve().parents[1] / 'scripts/local-tls-relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class RecordTests(unittest.TestCase):
    def test_split_preserves_handshake_and_record_version(self):
        body = b'\x01' + bytes(range(256)) * 3
        data = b'\x16\x03\x01' + len(body).to_bytes(2, 'big') + body
        split = relay.split_record(data)
        recovered = bytearray()
        while split:
            self.assertEqual(split[:3], data[:3])
            length = int.from_bytes(split[3:5], 'big')
            self.assertLessEqual(length, 100)
            recovered.extend(split[5:5+length])
            split = split[5+length:]
        self.assertEqual(recovered, body)

    def test_rejects_invalid_and_incomplete_records(self):
        for record in [b'', b'\x17\x03\x03\x00\x01\x01',
                       b'\x16\x03\x03\x00\x02\x01',
                       b'\x16\x03\x03\xff\xff\x01',
                       b'\x16\x03\x03\x00\x01\x02']:
            with self.subTest(record=record), self.assertRaises(ValueError):
                relay.split_record(record)

    def test_patch_retains_existing_rules_and_runtime_tun_state(self):
        source = 'proxies:\n  - name: existing\nrules:\n- DOMAIN,other.test,DIRECT\ntun:\n  enable: false\n  stack: mixed\n'
        settings = dict(listen_port=19443, username='local', password='secret',
                        server_name='chat.v50to.cc', allowed_ports=[443, 15347],
                        upstream_ip='120.55.167.91')
        patched = relay.patched_config(source, settings, True)
        self.assertIn('DOMAIN,other.test,DIRECT', patched)
        self.assertIn('  - name: existing', patched)
        self.assertIn('  enable: true', patched)
        proxy = json.loads(patched.splitlines()[1].strip()[2:])
        self.assertEqual(proxy['server'], '127.0.0.1')
        self.assertFalse(proxy['udp'])
        self.assertEqual(source.splitlines()[-2], '  enable: false')


class SocksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = relay.Relay(dict(username='local', password='secret',
                                     server_name='chat.v50to.cc',
                                     upstream_ip='120.55.167.91', allowed_ports=[443, 15347]))
        self.server = await asyncio.start_server(self.relay.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.sleep(0.02)

    async def connect(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.writers.append(writer)
        return reader, writer

    async def authenticate(self, reader, writer, password=b'secret'):
        writer.write(b'\x05\x01\x02')
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b'\x05\x02')
        writer.write(b'\x01\x05local' + bytes([len(password)]) + password)
        await writer.drain()
        return await reader.readexactly(2)

    async def test_requires_authentication(self):
        reader, writer = await self.connect()
        writer.write(b'\x05\x01\x00'); await writer.drain()
        self.assertEqual(await reader.readexactly(2), b'\x05\xff')
        self.assertEqual(await reader.read(), b'')

    async def test_rejects_wrong_password(self):
        reader, writer = await self.connect()
        self.assertEqual(await self.authenticate(reader, writer, b'wrong'), b'\x01\x01')
        self.assertEqual(await reader.read(), b'')

    async def test_rejects_unrelated_host_and_port(self):
        for host, port in [(b'example.com', 443), (b'chat.v50to.cc', 22)]:
            reader, writer = await self.connect()
            self.assertEqual(await self.authenticate(reader, writer), b'\x01\x00')
            writer.write(b'\x05\x01\x00\x03' + bytes([len(host)]) + host
                         + port.to_bytes(2, 'big')); await writer.drain()
            self.assertEqual((await reader.readexactly(10))[:2], b'\x05\x02')
            self.assertEqual(await reader.read(), b'')


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(listen_port=19443, username='local', password='private-password',
                             server_name='chat.v50to.cc', allowed_ports=[443, 15347])

    def test_controller_failure_does_not_expose_error_or_probe_tls(self):
        with patch.object(relay, 'api', side_effect=ValueError('private-password')), \
                patch.object(relay.socket, 'create_connection', side_effect=OSError('private-password')) as connect:
            report = relay.check_connection(self.settings)
        self.assertFalse(report['ok'])
        self.assertNotIn('private-password', json.dumps(report))
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args[0], ('127.0.0.1', 19443))

    def test_success_uses_default_certificate_verification_for_both_entries(self):
        rules = [{'proxy': relay.NAME, 'payload': f'chat.v50to.cc {port}'} for port in [443, 15347]]
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.side_effect = [b'\x05\x02', b'\x01\x00']
        with patch.object(relay, 'api', side_effect=[dict(tun=dict(enable=True), mode='rule'), dict(rules=rules)]), \
                patch.object(relay.socket, 'create_connection', return_value=sock) as connect, \
                patch.object(relay.ssl.SSLContext, 'wrap_socket', return_value=MagicMock()) as wrap:
            report = relay.check_connection(self.settings)
        self.assertTrue(report['ok'])
        self.assertEqual([call.args[0][1] for call in connect.call_args_list], [19443, 443, 15347])
        self.assertEqual(wrap.call_count, 2)
        self.assertTrue(all(call.kwargs['server_hostname'] == 'chat.v50to.cc' for call in wrap.call_args_list))
        self.assertNotIn('private-password', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
