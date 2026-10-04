import unittest
from streamctl.watchdog import RateLimiter, Watchdog
from streamctl.config import Store, write_watchdog_config, dump, atomic_write
from streamctl.service import commit
import tempfile
from pathlib import Path
from unittest.mock import patch


def publisher(count, name='publisher', kind='rtmps/conns', state='publish'):
    return dict(id=name, kind=kind, state=state, inboundBytes=count, path='live/alice')


class BitrateWatchdog(unittest.TestCase):
    def test_sustained_over_limit_kicks_after_full_window_and_confirmation(self):
        limiter = RateLimiter()
        for second in range(6):
            self.assertEqual(limiter.sample([publisher(second*250000)], second, 1000), [])
        exceeded = limiter.sample([publisher(6*250000)], 6, 1000)
        self.assertEqual(len(exceeded), 1)
        self.assertEqual(exceeded[0][1], 2000)

    def test_burst_at_limit_readers_and_multiple_publishers_are_not_kicked(self):
        limiter = RateLimiter()
        for second in range(12):
            items = [publisher(second*125000, 'at-limit'),
                     publisher(500000 if second else 0, 'one-burst'),
                     publisher(second*10000000, 'reader', state='read'),
                     publisher(second*100000, 'other-account')]
            self.assertEqual(limiter.sample(items, second, 1000), [])
        self.assertNotIn(('rtmps/conns', 'reader'), limiter.history)

    def test_reconnect_counter_reset_and_limit_change_do_not_reuse_old_strikes(self):
        limiter = RateLimiter()
        for second in range(6):
            limiter.sample([publisher(second*250000)], second, 1000)
        self.assertEqual(limiter.sample([publisher(6*250000)], 6, 3000), [])
        self.assertEqual(limiter.sample([], 7, 1000), [])
        self.assertEqual(limiter.sample([publisher(2000000)], 8, 1000), [])
        self.assertEqual(limiter.sample([publisher(0)], 9, 1000), [])
        self.assertEqual(limiter.sample([publisher(1000)], 10, 1000), [])

    def test_missing_counter_is_an_error_and_legacy_settings_get_mandatory_default(self):
        with self.assertRaises(ValueError):
            RateLimiter().sample([dict(publisher(1), inboundBytes=None)], 0, 45000)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory); store.initialize()
            settings, accounts, control = store.load()
            settings.pop('publish_limit_kbps')
            write_watchdog_config(store, settings, control)
            import json
            self.assertEqual(json.loads((store.path/'watchdog/config.json').read_text())['publish_limit_kbps'], 4000)

    def test_live_limit_changes_are_atomic_and_other_live_setting_changes_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory); store.initialize()
            settings, accounts, control = store.load()
            class Live:
                def running(self): return True
                def wait_loaded(self, config): pass
            changed = dict(settings, publish_limit_kbps=32000)
            commit(store, settings=changed, service=Live())
            import json
            self.assertEqual(json.loads((store.path/'watchdog/config.json').read_text())['publish_limit_kbps'], 32000)
            with self.assertRaises(ValueError):
                commit(store, settings=dict(changed, hostname='different.example.com'), service=Live())
            with patch('streamctl.service.write_watchdog_config', side_effect=[OSError('write failed'), None]):
                with self.assertRaises(OSError):
                    commit(store, settings=dict(changed, publish_limit_kbps=40000), service=Live())
            self.assertEqual(store.load()[0]['publish_limit_kbps'], 32000)

    def test_monitor_poll_auth_failure_does_not_silently_ignore_protection(self):
        import urllib.error
        import json
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.json'
            atomic_write(config, dump({'publish_limit_kbps':45000}))
            worker = Watchdog(config)
            worker.api = lambda *a: (_ for _ in ()).throw(urllib.error.HTTPError('private', 401, 'Unauthorized', {}, None))
            with self.assertRaises(urllib.error.HTTPError):
                worker.tick()
