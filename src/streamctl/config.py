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
            for directory in ("mediamtx", "certs", "backups", "reports"):
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
    ports = [settings.get(k) for k in ("rtmp_port", "rtmps_port", "rtsp_port", "api_port")]
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 4:
        raise ValueError("端口须在 1024–65535 且互不重复")
    ipaddress.ip_address(settings["bind_address"])
    hostname = settings["hostname"]
    if not isinstance(hostname, str) or not hostname or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:" for c in hostname):
        raise ValueError("非法服务主机名")
    for key in ("certificate", "private_key"):
        path = Path(settings[key])
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "certs" or path.name in (".", ".."):
            raise ValueError("证书必须位于 runtime/certs，使用 certs/文件名")
    return settings


def render(settings, accounts, control, *, legacy_validation=False):
    validate_settings(settings)
    validate_accounts(accounts, allow_legacy=legacy_validation)
    config = yaml.safe_load((ROOT / "config/mediamtx.base.yml").read_text())
    config["rtmpEncryption"] = "strict" if settings["mode"] == "production" else "no"
    config["rtmpServerCert"] = "/certs/" + Path(settings["certificate"]).name
    config["rtmpServerKey"] = "/certs/" + Path(settings["private_key"]).name
    paths = {u["stream_path"]: {} for u in accounts["users"]}
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
