import tempfile
import unittest
from unittest.mock import patch

import yaml

from streamctl.config import Store, render, validate_settings
from streamctl.service import Service
from streamctl.native import manager, unit_text, units
from pathlib import Path


class NativeConfiguration(unittest.TestCase):
    def test_lan_buffer_keeps_native_rtsp_private(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory); store.initialize()
            settings, accounts, control = store.load()
            settings.update(service_backend='systemd', systemd_scope='user', local_network=True,
                            bind_address='192.168.1.2', hostname='192.168.1.2', rtsp_buffer_ms=1000,
                            rtsp_internal_port=18554, control_enabled=True)
            config = yaml.safe_load(render(settings, accounts, control))
            self.assertEqual(config['rtspAddress'], '127.0.0.1:18554')
            self.assertEqual(config['rtmpAddress'], '192.168.1.2:1935')
            self.assertEqual(manager(settings), ['systemctl', '--user'])
            self.assertEqual(len(units(settings)), 5)
            text = unit_text(Path('/home/user/project'), Path(directory), 'python command',
                             'ciallochat-buffer', settings)
            self.assertIn('WantedBy=default.target', text)
            self.assertNotIn('ProtectHome=true', text)
            for changes in ({'rtsp_internal_port': 8554}, {'rtsp_buffer_ms': 99},
                            {'systemd_scope': 'invalid'}, {'control_enabled': 'yes'}):
                with self.assertRaises(ValueError): validate_settings(dict(settings, **changes))

    def test_management_is_private_and_local_media_uses_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory); store.initialize()
            settings, accounts, control = store.load()
            settings.update(service_backend='systemd', native_runtime=directory)
            config = yaml.safe_load(render(settings, accounts, control))
            self.assertEqual(config['apiAddress'], '127.0.0.1:9997')
            self.assertEqual(config['authHTTPAddress'], 'http://127.0.0.1:9000/auth')
            self.assertEqual(config['rtspAddress'], '127.0.0.1:8554')
            self.assertEqual(config['rtmpServerCert'], directory+'/certs/server.crt')
            settings['mode'] = 'production'
            self.assertEqual(yaml.safe_load(render(settings, accounts, control))['rtmpEncryption'], 'strict')
            for changes in ({'auth_port': 9997}, {'auth_port': True}, {'service_backend': 'invalid'}):
                with self.assertRaises(ValueError): validate_settings(dict(settings, **changes))

    def test_systemd_backend_does_not_invoke_docker_and_requires_tls_for_production(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory); store.initialize()
            settings, accounts, control = store.load()
            settings.update(service_backend='systemd', mode='production')
            service = Service(store, settings, control)
            with patch('streamctl.native.running', return_value=True) as native:
                self.assertTrue(service.running())
                native.assert_called_once()
            with patch('streamctl.native.up') as start:
                with self.assertRaises(ValueError): service.up()
                start.assert_not_called()
