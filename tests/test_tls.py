import subprocess
import tempfile
import unittest
from streamctl.config import Store, ROOT, check_tls


class TLS(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.store.initialize()
        subprocess.run([str(ROOT/'scripts/make-test-cert.sh'),str(self.store.path/'certs')],check=True,capture_output=True)
        self.settings=self.store.read('settings.json');self.settings['mode']='production'

    def test_valid_chain_hostname_and_permissions(self):
        check_tls(self.store,self.settings)
        key=self.store.path/'certs/server.key';key.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'chmod 600'):check_tls(self.store,self.settings)

    def test_hostname_mismatch_and_key_mismatch(self):
        self.settings['hostname']='wrong.example.com'
        with self.assertRaises(ValueError):check_tls(self.store,self.settings)
        self.settings['hostname']='localhost'
        (self.store.path/'certs/server.key').write_bytes((self.store.path/'certs/ca.key').read_bytes())
        with self.assertRaisesRegex(ValueError,'不匹配'):check_tls(self.store,self.settings)

    def test_ip_address_uses_certificate_ip_san(self):
        self.settings['hostname'] = '127.0.0.1'
        check_tls(self.store, self.settings)
        self.settings['hostname'] = '127.0.0.2'
        with self.assertRaises(ValueError):
            check_tls(self.store, self.settings)

    def test_missing_certificate_fails_without_plaintext_fallback(self):
        (self.store.path/'certs/server.crt').unlink()
        with self.assertRaises(ValueError):check_tls(self.store,self.settings)

    def test_expired_and_not_yet_valid_certificates_are_rejected(self):
        ca_dir = self.store.path / 'certs'
        request = ca_dir / 'server.csr'
        subprocess.run(['openssl', 'req', '-new', '-key', str(ca_dir / 'server.key'),
                        '-out', str(request), '-subj', '/CN=localhost'],
                       check=True, capture_output=True)
        ca_config = ca_dir / 'ca.cnf'
        ca_config.write_text(f'''[ca]
default_ca = test_ca
[test_ca]
database = {ca_dir / 'index'}
serial = {ca_dir / 'serial'}
new_certs_dir = {ca_dir}
certificate = {ca_dir / 'ca.crt'}
private_key = {ca_dir / 'ca.key'}
default_md = sha256
policy = test_policy
x509_extensions = server_extensions
[test_policy]
commonName = supplied
[server_extensions]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost,IP:127.0.0.1
''')
        for start, end in [('20200101000000Z', '20200102000000Z'),
                           ('20500101000000Z', '20500102000000Z')]:
            with self.subTest(start=start):
                (ca_dir / 'index').write_text('')
                (ca_dir / 'serial').write_text('01\n')
                subprocess.run(['openssl', 'ca', '-batch', '-notext', '-config', str(ca_config),
                                '-in', str(request), '-out', str(ca_dir / 'server.crt'),
                                '-startdate', start, '-enddate', end],
                               check=True, capture_output=True)
                with self.assertRaises(ValueError):
                    check_tls(self.store, self.settings)


if __name__=='__main__':unittest.main()
