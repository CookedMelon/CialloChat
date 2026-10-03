import base64
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import yaml
from .config import ROOT, VERSION, atomic_write, dump, render, check_tls, exclusive_json, write_watchdog_config
from .images import auth_image


class Service:
    def __init__(self, store, settings=None, control=None):
        self.store = store
        self.settings = settings or store.read("settings.json")
        self.control = control or store.read("control.json")

    def compose(self, args, capture=True):
        s = self.settings
        env = os.environ.copy()
        env.update(CIALLOCHAT_RUNTIME=str(self.store.path), CIALLOCHAT_CERT_DIR=str(self.store.path / "certs"),
                   CIALLOCHAT_AUTH_IMAGE=auth_image(),
                   CIALLOCHAT_UID=str(os.getuid()), CIALLOCHAT_GID=str(os.getgid()), CIALLOCHAT_BIND=("[" + s["bind_address"] + "]" if ":" in s["bind_address"] else s["bind_address"]),
                   CIALLOCHAT_RTMP_PORT=str(s["rtmp_port"]), CIALLOCHAT_RTMPS_PORT=str(s["rtmps_port"]),
                   CIALLOCHAT_RTSP_PORT=str(s["rtsp_port"]), CIALLOCHAT_API_PORT=str(s["api_port"]))
        cmd = ["docker", "compose", "--project-name", os.environ.get("CIALLOCHAT_PROJECT", "ciallochat"), "-f", str(ROOT / "compose.yaml")]
        if s["mode"] == "local":
            cmd += ["-f", str(ROOT / "compose.local.yaml")]
        result = subprocess.run(cmd + args, env=env, capture_output=capture, text=True, timeout=None if not capture else 120)
        if result.returncode:
            raise RuntimeError("Docker Compose 失败: " + (result.stderr.strip() if capture else "请检查 Docker daemon 和 WSL 集成"))
        return result.stdout if capture else ""

    def running(self):
        try:
            return bool(self.compose(["ps", "--status", "running", "-q", "mediamtx"]).strip())
        except (RuntimeError, FileNotFoundError):
            if (self.store.path / "active.json").exists():
                raise RuntimeError("无法确认已启动服务的状态；请恢复 Docker 连接后重试")
            return False

    def api(self, endpoint, method="GET"):
        token = base64.b64encode((self.control["username"] + ":" + self.control["password"]).encode()).decode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.settings['api_port']}/v3/" + endpoint,
                                     headers={"Authorization": "Basic " + token}, method=method)
        # No proxy: management traffic must remain on the host loopback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=6) as response:
            body = response.read()
            return json.loads(body) if body else None

    def wait_loaded(self, expected):
        deadline = time.monotonic() + 12
        last = ""
        desired = yaml.safe_load(expected)
        while time.monotonic() < deadline:
            try:
                global_config = self.api("config/global/get")
                paths = self.api("config/paths/list?itemsPerPage=10000")["items"]
                wanted_users = desired["authInternalUsers"]
                actual_users = global_config["authInternalUsers"]
                def normalized(users):
                    return [{"user": u["user"], "pass": "<redacted>" if u["pass"] else "", "permissions": [{"action": p["action"], "path": p.get("path", "")} for p in u["permissions"]]} for u in users]
                if normalized(actual_users) == normalized(wanted_users) and set(p["name"] for p in paths) == set(desired["paths"]):
                    keys = ("authMethod", "authHTTPAddress", "authHTTPExclude", "api", "metrics", "pprof", "playback", "rtsp", "rtspTransports", "rtmp", "rtmpEncryption", "hls", "webrtc", "srt", "moq")
                    defaults = self.api("config/path-defaults/get")
                    if all(global_config[k] == desired[k] for k in keys) and all(defaults[k] == v for k,v in desired["pathDefaults"].items()):
                        return
                last = "API 配置尚未与生成配置一致"
            except (OSError, ValueError, KeyError) as exc:
                last = type(exc).__name__
            time.sleep(0.15)
        raise RuntimeError("配置加载确认超时: " + last)

    def kick(self, paths, states=('publish',)):
        states = set(states)
        if not states or not states <= {'publish', 'read'}:
            raise ValueError('非法连接撤销范围')
        for protocol in ("rtmp", "rtmps", "rtsp"):
            kind = "sessions" if protocol == "rtsp" else "conns"
            try:
                items = self.api(f"{protocol}/{kind}/list?itemsPerPage=10000")["items"]
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # listener disabled in this mode
                    continue
                raise
            for item in items:
                if item.get("path") in paths and item.get("state") in states:
                    try:
                        self.api(f"{protocol}/{kind}/kick/" + item["id"], "POST")
                    except urllib.error.HTTPError as exc:
                        if exc.code != 404:
                            raise
        # Verify the requested connection classes are absent after revocation.
        for protocol in ("rtmp", "rtmps", "rtsp"):
            kind = "sessions" if protocol == "rtsp" else "conns"
            try:
                items = self.api(f"{protocol}/{kind}/list?itemsPerPage=10000")["items"]
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # listener disabled in this mode
                    continue
                raise
            if any(i.get("path") in paths and i.get("state") in states for i in items):
                raise RuntimeError("仍存在待撤销的推流/观看连接")

    def up(self):
        if self.settings["mode"] == "production":
            check_tls(self.store, self.settings)
        subprocess.run(['bash', str(ROOT / 'scripts/build-auth.sh')], check=True)
        write_watchdog_config(self.store, self.settings, self.control)
        self.compose(["config", "--quiet"])
        atomic_write(self.store.path / "active.json", dump({"mode": self.settings["mode"]}))
        self.compose(["up", "-d", "--wait", "--wait-timeout", "45"])
        self.wait_loaded((self.store.path / "mediamtx/mediamtx.yml").read_text())

    def reload_certificate(self):
        import socket
        import ssl
        if self.settings["mode"] != "production":
            raise ValueError("证书重载仅适用于 production")
        cert, key = check_tls(self.store, self.settings)
        if not self.running():
            raise ValueError("服务未运行，请执行 up")
        expected = subprocess.run(["openssl", "x509", "-in", str(cert), "-outform", "DER"], capture_output=True, check=True).stdout
        self.compose(["restart", "mediamtx"])
        self.wait_loaded((self.store.path / "mediamtx/mediamtx.yml").read_text())
        ca = self.store.path / "certs/ca.crt"
        context = ssl.create_default_context(cafile=str(ca) if ca.exists() else None)
        address = "127.0.0.1" if self.settings["bind_address"] in ("0.0.0.0", "::") else self.settings["bind_address"]
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection((address, self.settings["rtmps_port"]), timeout=3) as sock:
                    with context.wrap_socket(sock, server_hostname=self.settings["hostname"]) as tls:
                        if tls.getpeercert(binary_form=True) != expected:
                            raise ValueError("运行服务未使用新证书")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)

    def down(self):
        self.compose(["down"])
        (self.store.path / "active.json").unlink(missing_ok=True)


def commit(store, accounts=None, settings=None, revoke=(), service=None, revoke_states=('publish',)):
    """Caller holds Store.lock. Journal protects account/config consistency."""
    account_update = accounts is not None and settings is None
    old_settings, old_accounts, control = store.load()
    settings = settings or old_settings
    accounts = accounts if accounts is not None else old_accounts
    service = service or Service(store, old_settings, control)
    running = service.running()
    changed_keys = {k for k in set(settings) | set(old_settings) if settings.get(k) != old_settings.get(k)}
    if running and changed_keys - {'publish_limit_kbps'}:
        raise ValueError("运行中的服务设置变更需先 down，修改设置后 apply，再 up")
    # An expired or missing renewal file must not prevent revoking a live
    # publisher. Startup and explicit configuration application still require
    # valid TLS files, while account-only updates preserve the strict TLS mode.
    if settings["mode"] == "production" and not account_update:
        check_tls(store, settings)
    generated = render(settings, accounts, control)
    previous = {"settings": old_settings, "accounts": old_accounts,
                "config": (store.path / "mediamtx/mediamtx.yml").read_text()}
    backup = store.path / "backups" / ("pre-change-" + str(time.time_ns()) + ".json")
    atomic_write(backup, dump(previous))
    journal = store.path / "transaction.json"
    atomic_write(journal, dump(previous))
    try:
        atomic_write(store.path / "mediamtx/mediamtx.yml", generated)
        if running:
            service.wait_loaded(generated)
        atomic_write(store.path / "settings.json", dump(settings))
        atomic_write(store.path / "accounts.json", dump(accounts))
        write_watchdog_config(store, settings, control)
    except Exception:
        atomic_write(store.path / "mediamtx/mediamtx.yml", previous["config"])
        atomic_write(store.path / "settings.json", dump(previous["settings"]))
        atomic_write(store.path / "accounts.json", dump(previous["accounts"]))
        write_watchdog_config(store, old_settings, control)
        if running:
            service.wait_loaded(previous["config"])
        journal.unlink(missing_ok=True)
        raise
    pending = store.path / "pending-revocations.json"
    if running and revoke:
        atomic_write(pending, dump({"paths": list(revoke), 'states': list(revoke_states)}))
    journal.unlink()
    if running and revoke:
        try:
            if tuple(revoke_states) == ('publish',):
                service.kick(set(revoke))
            else:
                service.kick(set(revoke), states=revoke_states)
            pending.unlink(missing_ok=True)
        except Exception as exc:
            raise RuntimeError("新权限已保存，但旧连接撤销未完成；立即执行 ./streamctl down，然后 up。原因: " + type(exc).__name__) from exc


def recover(store):
    journal = store.path / "transaction.json"
    if journal.exists():
        previous = json.loads(journal.read_text())
        atomic_write(store.path / "mediamtx/mediamtx.yml", previous["config"])
        atomic_write(store.path / "settings.json", dump(previous["settings"]))
        atomic_write(store.path / "accounts.json", dump(previous["accounts"]))
        if "control" in previous:
            atomic_write(store.path / "control.json", dump(previous["control"]))
        write_watchdog_config(store, previous['settings'], store.read('control.json'))
        if "certificates" in previous:
            for path in (store.path / "certs").iterdir():
                if path.is_file() or path.is_symlink():
                    path.unlink()
            for name, value in previous["certificates"].items():
                atomic_write(store.path / "certs" / name, base64.b64decode(value))
        service = Service(store)
        if service.running():
            service.wait_loaded(previous["config"])
        journal.unlink()
    pending = store.path / "pending-revocations.json"
    if pending.exists():
        service = Service(store)
        if service.running():
            try:
                data = json.loads(pending.read_text())
                if data.get('states', ['publish']) == ['publish']:
                    service.kick(set(data['paths']))
                else:
                    service.kick(set(data['paths']), states=data['states'])
            except Exception as exc:
                raise RuntimeError("仍有未完成的连接撤销；立即 down，然后 up。原因: " + type(exc).__name__) from exc
        pending.unlink()


def migrate(store, destination):
    """Explicit offline migration. Caller holds lock; failure remains closed."""
    from .accounts import migrate_records, credentials
    settings, old, control = store.load(allow_legacy=True)
    service = Service(store, settings, control)
    if service.running():
        raise ValueError('账号迁移需要先 down，以撤销全部旧匿名连接')
    updated, delivered = migrate_records(old)
    generated = render(settings, updated, control)
    original = dict(settings=settings, accounts=old, config=(store.path/'mediamtx/mediamtx.yml').read_text())
    atomic_write(store.path/'backups'/('pre-migration-'+str(time.time_ns())+'.json'), dump(original))
    # A failed/interrupted migration must never restore anonymous permissions.
    previous = dict(original)
    previous['config'] = render(settings, {'schema':2,'users':[]}, control)
    exported = exclusive_json(destination, [credentials(settings, name, read_key=value) for name,value in delivered])
    try:
        atomic_write(store.path/'transaction.json', dump(previous))
        atomic_write(store.path/'mediamtx/mediamtx.yml', previous['config'])
        atomic_write(store.path/'accounts.json', dump(updated))
        atomic_write(store.path/'mediamtx/mediamtx.yml', generated)
        (store.path/'transaction.json').unlink()
    except Exception:
        recover(store)
        exported.unlink(missing_ok=True)
        raise
    return exported


def backup(store, destination, include_certificates=False):
    settings, accounts, control = store.load()
    data = dict(version=VERSION, settings=settings, accounts=accounts, control=control,
                config=render(settings, accounts, control))
    if include_certificates:
        data["certificates"] = {p.name: base64.b64encode(p.read_bytes()).decode() for p in (store.path / "certs").iterdir() if p.is_file() and not p.is_symlink()}
    destination = Path(destination).resolve()
    # O_EXCL ensures an explicit destination never silently replaces an existing backup.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(dump(data))
    return destination


def restore(store, source, migrate_credentials=None):
    from .config import validate_settings
    from .accounts import validate_accounts, validate_hash, HASHER, migrate_records, credentials
    source = Path(source)
    if not source.is_file() or source.stat().st_mode & 0o077 or source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("备份文件必须权限 600，且不超过 16 MiB")
    data = json.loads(source.read_text())
    if data.get("version") != VERSION:
        raise ValueError("备份版本不兼容；须先按原版本恢复")
    settings = validate_settings(data["settings"])
    accounts = validate_accounts(data["accounts"], allow_legacy=True)
    control = data["control"]
    validate_hash(control["password_hash"])
    if control.get("username") != "ciallochat-control" or not HASHER.verify(control["password_hash"][7:], control["password"]):
        raise ValueError("管理凭据校验失败")
    legacy = accounts['schema'] == 1
    current_config = render(settings, accounts, control, legacy_validation=legacy)
    if current_config != data["config"]:
        previous_config = render(settings, accounts, control, legacy_validation=legacy, legacy_authentication=True)
        if previous_config != data['config']:
            raise ValueError("备份配置与账号数据不一致")
        data['config'] = current_config
    delivered = []
    if legacy:
        if migrate_credentials is None:
            raise ValueError('旧备份需 --migrate-credentials-file 显式交付观看密钥；不会恢复匿名读取')
        accounts, delivered = migrate_records(accounts)
        data['config'] = render(settings, accounts, control)
    if Service(store).running():
        raise ValueError("恢复需先 down；恢复后执行 up")
    certificates = {}
    for name, content in data.get("certificates", {}).items():
        if Path(name).name != name or name.startswith("."):
            raise ValueError("非法证书备份名称")
        certificates[name] = base64.b64decode(content, validate=True)
    # Validate in an isolated staging directory before touching live data.
    import tempfile
    from .config import Store
    with tempfile.TemporaryDirectory(prefix="ciallochat-restore-") as temp:
        staging = Store(temp)
        staging.initialize()
        atomic_write(staging.path / "settings.json", dump(settings))
        atomic_write(staging.path / "accounts.json", dump(accounts))
        atomic_write(staging.path / "control.json", dump(control))
        for name, value in certificates.items():
            atomic_write(staging.path / "certs" / name, value)
        if settings["mode"] == "production":
            if (store.path / "certs/ca.crt").exists() and not (staging.path / "certs/ca.crt").exists():
                atomic_write(staging.path / "certs/ca.crt", (store.path / "certs/ca.crt").read_bytes())
            for field in ("certificate", "private_key"):
                target = staging.path / settings[field]
                if not target.exists() and (store.path / settings[field]).exists():
                    atomic_write(target, (store.path / settings[field]).read_bytes())
            check_tls(staging, settings)
        exported = exclusive_json(migrate_credentials, [credentials(settings, name, read_key=value) for name,value in delivered]) if legacy else None
        try:
            previous_backup = backup(store, store.path / "backups" / ("pre-restore-" + str(time.time_ns()) + ".json"), True)
            atomic_write(store.path / "transaction.json", previous_backup.read_bytes())
            for name, value in certificates.items():
                atomic_write(store.path / "certs" / name, value)
            atomic_write(store.path / "control.json", dump(control))
            atomic_write(store.path / "accounts.json", dump(accounts))
            atomic_write(store.path / "settings.json", dump(settings))
            atomic_write(store.path / "mediamtx/mediamtx.yml", data["config"])
        except Exception:
            recover(store)
            if exported:
                exported.unlink(missing_ok=True)
            raise
        (store.path / "transaction.json").unlink()
