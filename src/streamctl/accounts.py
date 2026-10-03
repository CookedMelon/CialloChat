import re
import secrets
from datetime import datetime, timezone
from argon2 import PasswordHasher, extract_parameters, Type
from argon2.exceptions import VerifyMismatchError

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16)
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}\Z")
RESERVED = {"any", "admin", "ciallochat-control"}


def validate_username(name):
    if not isinstance(name, str) or not NAME.fullmatch(name) or name.lower() in RESERVED or name.lower().startswith('ciallochat-'):
        raise ValueError("用户名须为 1–48 位 ASCII 字母、数字、_、-，首位为字母或数字，且不能为保留名")
    return name


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValueError("密码须为 12–256 个字符，不含控制字符")
    return password


def hash_password(password):
    return "argon2:" + HASHER.hash(validate_password(password))


def new_password():
    return secrets.token_urlsafe(24)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def read_identity(username):
    return 'ciallochat-read-' + validate_username(username)


def credentials(settings, username, publish_key=None, read_key=None):
    import urllib.parse
    host = settings['hostname'] if settings['mode'] == 'production' else '127.0.0.1'
    if ':' in host:
        host = '[' + host + ']'
    result = {'username': username, 'stream_path': 'live/' + username}
    if publish_key is not None:
        scheme = 'rtmps' if settings['mode'] == 'production' else 'rtmp'
        port = settings['rtmps_port' if scheme == 'rtmps' else 'rtmp_port']
        query = urllib.parse.urlencode({'user': username, 'pass': publish_key})
        result.update(publish_username=username, publish_key=publish_key, password=publish_key,
                      publish_url=f'{scheme}://{host}:{port}/live/{username}?{query}')
    if read_key is not None:
        identity = read_identity(username)
        encoded = urllib.parse.quote(read_key, safe='')
        result.update(read_username=identity, read_key=read_key,
                      read_url=f'rtsp://{identity}:{encoded}@{host}:{settings["rtsp_port"]}/live/{username}')
    return result


def matches(hashed, value):
    try:
        return HASHER.verify(hashed[7:], value)
    except VerifyMismatchError:
        return False


def new_account(username, password, read_key=None):
    validate_username(username)
    read_key = new_password() if read_key is None else read_key
    validate_password(password); validate_password(read_key)
    if password == read_key:
        raise ValueError('推流密钥与观看密钥必须不同')
    now = timestamp()
    return dict(username=username, publish_key_hash=hash_password(password), read_key_hash=hash_password(read_key), stream_path="live/" + username,
                enabled=True, created_at=now, updated_at=now)


def validate_accounts(data, allow_legacy=False):
    if isinstance(data, dict) and data.get('schema') == 1 and not allow_legacy:
        raise ValueError('旧账号数据需要迁移：先 down，再 user migrate --credentials-file /安全目录/read-keys.json')
    if not isinstance(data, dict) or data.get("schema") not in ((1, 2) if allow_legacy else (2,)) or not isinstance(data.get("users"), list):
        raise ValueError("不兼容的账号文件")
    seen = set()
    for account in data["users"]:
        name = validate_username(account["username"])
        if name in seen or account["stream_path"] != "live/" + name:
            raise ValueError("重复账号或非法路径")
        seen.add(name)
        if type(account["enabled"]) is not bool:
            raise ValueError("enabled 必须为布尔值")
        if data['schema'] == 1:
            validate_hash(account['password_hash'])
        else:
            validate_hash(account['publish_key_hash'])
            validate_hash(account['read_key_hash'])
            if account['publish_key_hash'] == account['read_key_hash']:
                raise ValueError('两类密钥不能使用相同哈希')
        for key in ("created_at", "updated_at"):
            datetime.fromisoformat(account[key])
    return data


def migrate_records(data):
    import copy
    validate_accounts(data, allow_legacy=True)
    if data['schema'] != 1:
        raise ValueError('账号已经是双密钥格式，无需迁移')
    updated = copy.deepcopy(data)
    updated['schema'] = 2
    delivered = []
    for account in updated['users']:
        read_key = new_password()
        # Retain the existing publisher hash verbatim; do not reset publishers.
        while matches(account['password_hash'], read_key):
            read_key = new_password()
        account['publish_key_hash'] = account.pop('password_hash')
        account['read_key_hash'] = hash_password(read_key)
        account['updated_at'] = timestamp()
        delivered.append((account['username'], read_key))
    return validate_accounts(updated), delivered


def validate_hash(hashed):
    if not isinstance(hashed, str) or not hashed.startswith("argon2:$argon2id$"):
        raise ValueError("仅支持 Argon2id 密码哈希")
    params = extract_parameters(hashed[len("argon2:"):])
    if params.type != Type.ID or params.salt_len < 16 or params.hash_len < 32 or not 8192 <= params.memory_cost <= 262144 or not 1 <= params.time_cost <= 10 or not 1 <= params.parallelism <= 8:
        raise ValueError("不安全或过度耗费资源的 Argon2 参数")
    return hashed
