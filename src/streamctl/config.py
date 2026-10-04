import hashlib
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import yaml
from .accounts import HASHER, hash_password, new_password, validate_accounts, validate_password, validate_hash, read_identity

ROOT = Path(__file__).resolve().parents[2]
VERSION = json.loads((ROOT / "config/version.json").read_text())


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as file:
            file.write(data.encode() if isinstance(data, str) else data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def dump(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def exclusive_json(destination, data):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(dump(data)); file.flush(); os.fsync(file.fileno())
    return destination


class Store:
    def __init__(self, runtime=None):
        self.path = Path(runtime or os.environ.get("CIALLOCHAT_RUNTIME", ROOT / "runtime")).resolve()

    @contextlib.contextmanager
    def lock(self):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.path / ".lock", "a+b") as file:
            os.chmod(file.name, 0o600)
            fcntl.flock(file, fcntl.LOCK_EX)
            yield

    def read(self, name):
        return json.loads((self.path / name).read_text())

    def initialize(self, mode="local"):
        with self.lock():
            os.chmod(self.path, 0o700)
            for directory in ("mediamtx", "watchdog", "leases", "notifications", "traffic", "certs", "backups", "reports"):
                (self.path / directory).mkdir(mode=0o700, exist_ok=True)
            if not (self.path / "settings.json").exists():
                settings = json.loads((ROOT / "config/settings.example.json").read_text())
                settings["mode"] = mode
                atomic_write(self.path / "settings.json", dump(settings))
            if not (self.path / "accounts.json").exists():
                atomic_write(self.path / "accounts.json", dump({"schema": 2, "users": []}))
            if not (self.path / "control.json").exists():
                password = new_password()
                atomic_write(self.path / "control.json", dump({"username": "ciallochat-control", "password": password, "password_hash": hash_password(password)}))
            # No publisher is created by initialization; never overwrite existing config.
            if not (self.path / "mediamtx/mediamtx.yml").exists():
                settings, accounts, control = self.load()
                atomic_write(self.path / "mediamtx/mediamtx.yml", render(settings, accounts, control))

    def load(self, allow_legacy=False):
        settings = validate_settings(self.read("settings.json"))
        accounts = validate_accounts(self.read("accounts.json"), allow_legacy=allow_legacy)
        control = self.read("control.json")
        if control.get("username") != "ciallochat-control" or not control.get("password_hash", "").startswith("argon2:$argon2id$"):
            raise ValueError("非法管理凭据")
        validate_hash(control["password_hash"])
        validate_password(control["password"])
        HASHER.verify(control["password_hash"][7:], control["password"])
        return settings, accounts, control


def validate_settings(settings):
    if settings.get("schema") != 1 or settings.get("mode") not in ("local", "production"):
        raise ValueError("非法设置版本或运行模式")
    if settings.get('service_backend', 'docker') not in ('docker', 'systemd'):
        raise ValueError('service_backend 须为 docker 或 systemd')
    if type(settings.get('native_connection_guard', False)) is not bool:
        raise ValueError('native_connection_guard 须为布尔值')
    if settings.get('systemd_scope', 'system') not in ('system', 'user'):
        raise ValueError('systemd_scope 须为 system 或 user')
    if type(settings.get('local_network', False)) is not bool:
        raise ValueError('local_network 须为布尔值')
    if type(settings.get('control_enabled', False)) is not bool:
        raise ValueError('control_enabled 须为布尔值')
    if type(settings.get('test_video_enabled', False)) is not bool:
        raise ValueError('test_video_enabled 须为布尔值')
    ports = [settings.get(k) for k in ("rtmp_port", "rtmps_port", "rtsp_port", "api_port")]
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 4:
        raise ValueError("端口须在 1024–65535 且互不重复")
    auth_port = settings.get('auth_port', 9000)
    if type(auth_port) is not int or not 1024 <= auth_port <= 65535 or auth_port in ports:
        raise ValueError('鉴权端口须在 1024–65535 且与其他端口不同')
    buffer_ms = settings.get('rtsp_buffer_ms', 0)
    if type(buffer_ms) is not int or (buffer_ms != 0 and not 100 <= buffer_ms <= 3000):
        raise ValueError('RTSP 缓冲须为 0 或 100–3000 毫秒')
    if buffer_ms:
        if settings.get('service_backend') != 'systemd':
            raise ValueError('RTSP 缓冲需要原生 systemd 后端')
        internal = settings.get('rtsp_internal_port', 18554)
        if type(internal) is not int or not 1024 <= internal <= 65535 or internal in ports + [auth_port]:
            raise ValueError('内部 RTSP 端口须与其他端口不同')
    if settings.get('test_video_enabled') and not buffer_ms:
        raise ValueError('测试频道需要原生 RTSP 缓冲入口')
    limit = settings.get('publish_limit_kbps', 4000)
    if type(limit) is not int or not 100 <= limit <= 1000000:
        raise ValueError('每路推流上限须为 100–1000000 Kbps，不能关闭')
    ipaddress.ip_address(settings["bind_address"])
    hostname = settings["hostname"]
    if not isinstance(hostname, str) or not hostname or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:" for c in hostname):
        raise ValueError("非法服务主机名")
    for key in ("certificate", "private_key"):
        path = Path(settings[key])
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "certs" or path.name in (".", ".."):
            raise ValueError("证书必须位于 runtime/certs，使用 certs/文件名")
    return settings


def public_urls(settings):
    scheme = 'rtmps' if settings['mode'] == 'production' else 'rtmp'
    host = settings['hostname'] if settings['mode'] == 'production' or settings.get('local_network') else '127.0.0.1'
    if ':' in host:
        host = '[' + host + ']'
    return dict(publish_base=f'{scheme}://{host}:{settings["rtmps_port" if scheme == "rtmps" else "rtmp_port"]}',
                read_base=f'rtsp://{host}:{settings["rtsp_port"]}',
                test_url=f'rtsp://{host}:{settings["rtsp_port"]}/test' if settings.get('test_video_enabled') else None)


def write_watchdog_config(store, settings, control):
    validate_settings(settings)
    if type(settings.get('traffic_enabled', True)) is not bool:
        raise ValueError('traffic_enabled 须为布尔值')
    for name in ('leases', 'notifications', 'traffic'):
        (store.path/name).mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(store.path/'watchdog/config.json', dump({
        'api_url': (f'http://127.0.0.1:{settings["api_port"]}/v3/'
                    if settings.get('service_backend') == 'systemd' else 'http://mediamtx:9997/v3/'),
        'username': control['username'],
        'password': control['password'],
        'publish_limit_kbps': settings.get('publish_limit_kbps', 4000),
        'traffic_enabled': settings.get('traffic_enabled', True),
        'test_video_enabled': settings.get('test_video_enabled', False),
        **public_urls(settings),
    }))


def render(settings, accounts, control, *, legacy_validation=False, legacy_authentication=False):
    validate_settings(settings)
    validate_accounts(accounts, allow_legacy=legacy_validation)
    config = yaml.safe_load((ROOT / "config/mediamtx.base.yml").read_text())
    # Only used to validate backups made before URL-key admission existed.
    if legacy_authentication:
        config['authMethod'] = 'internal'
        config.pop('authHTTPAddress', None)
        config.pop('authHTTPExclude', None)
    config["rtmpEncryption"] = "strict" if settings["mode"] == "production" else "no"
    config["rtmpServerCert"] = "/certs/" + Path(settings["certificate"]).name
    config["rtmpServerKey"] = "/certs/" + Path(settings["private_key"]).name
    if settings.get('service_backend') == 'systemd':
        config['authHTTPAddress'] = f'http://127.0.0.1:{settings.get("auth_port", 9000)}/auth'
        config['apiAddress'] = f'127.0.0.1:{settings["api_port"]}'
        host = '127.0.0.1' if settings['mode'] == 'local' and not settings.get('local_network') else settings['bind_address']
        if ':' in host:
            host = '['+host+']'
        config['rtspAddress'] = (f'127.0.0.1:{settings.get("rtsp_internal_port", 18554)}'
                                 if settings.get('rtsp_buffer_ms') else f'{host}:{settings["rtsp_port"]}')
        config['rtmpAddress'] = f'{host}:{settings["rtmp_port"]}'
        config['rtmpsAddress'] = f'{host}:{settings["rtmps_port"]}'
        for field, setting in (('rtmpServerCert', 'certificate'), ('rtmpServerKey', 'private_key')):
            config[field] = str(Path(settings.get('native_runtime', ROOT/'runtime'))/settings[setting])
    paths = {u["stream_path"]: {} for u in accounts["users"]}
    if settings.get('test_video_enabled'):
        from .testquota import PATH_PATTERN
        paths[PATH_PATTERN] = {}
    config["authInternalUsers"] = [
        {"user": control["username"], "pass": control["password_hash"], "ips": [], "permissions": [{"action": "api"}]},
    ]
    if accounts['schema'] == 1:
        # Used solely to validate an old backup before migration; never applied.
        config['authInternalUsers'].insert(0, {'user':'any','pass':'','ips':[],
            'permissions':[{'action':'read','path':path} for path in paths]})
    for u in accounts['users']:
        if not u['enabled']:
            continue
        config['authInternalUsers'].append({'user':u['username'], 'pass':u['password_hash'] if accounts['schema']==1 else u['publish_key_hash'],
            'ips':[], 'permissions':[{'action':'publish','path':u['stream_path']}]})
        if accounts['schema'] == 2:
            config['authInternalUsers'].append({'user':read_identity(u['username']), 'pass':u['read_key_hash'],
                'ips':[], 'permissions':[{'action':'read','path':u['stream_path']}]})
    config["paths"] = paths
    revision = hashlib.sha256(dump(config).encode()).hexdigest()[:32]
    config["authInternalUsers"].append({"user": "ciallochat-revision-" + revision, "pass": "", "ips": [], "permissions": []})
    return yaml.safe_dump(config, sort_keys=False)


def check_tls(store, settings):
    cert, key = (store.path / settings[k] for k in ("certificate", "private_key"))
    for path in (cert, key):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"缺少 TLS 文件: {path}")
        if path.stat().st_mode & 0o077:
            raise ValueError(f"TLS 文件需 chmod 600: {path}")
    def run(args):
        result = subprocess.run(["openssl", *args], capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError("TLS 检查失败: " + result.stderr.decode(errors="replace").strip())
        return result.stdout
    run(["x509", "-in", str(cert), "-noout", "-checkend", "0"])
    try:
        ipaddress.ip_address(settings["hostname"])
        check_host, verify_host = "-checkip", "-verify_ip"
    except ValueError:
        check_host, verify_host = "-checkhost", "-verify_hostname"
    run(["x509", "-in", str(cert), "-noout", check_host, settings["hostname"]])
    cert_pub = run(["x509", "-in", str(cert), "-pubkey", "-noout"])
    key_pub = run(["pkey", "-in", str(key), "-pubout", "-passin", "pass:"])
    if cert_pub != key_pub:
        raise ValueError("证书与私钥不匹配")
    # Verify date (including notBefore) and chain. Test CA may be provided locally.
    args = ["verify", "-purpose", "sslserver", verify_host, settings["hostname"]]
    ca = store.path / "certs/ca.crt"
    if ca.exists():
        args += ["-CAfile", str(ca)]
    args += ["-untrusted", str(cert), str(cert)]
    run(args)
    return cert, key
