"""Bounded TLS JSON protocol, with a signed timestamp and single-use nonce."""
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time

MAX_REQUEST = 8192
MAX_RESPONSE = 4 * 1024 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def load_config(path):
    path = Path(path)
    if path.stat().st_mode & 0o077 or path.stat().st_size > MAX_REQUEST:
        raise ValueError('控制配置文件必须为权限 600，且不超过 8 KiB')
    config = json.loads(path.read_text())
    password = config.get('password')
    if (not isinstance(password, str) or not 32 <= len(password) <= 256
            or password.startswith('REPLACE_') or any(ord(c) < 32 for c in password)):
        raise ValueError('管理密码须为 32–256 个字符；请替换模板中的示例值')
    port = config.get('port', 15347)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('非法控制端口')
    return config


def sign_request(password, command, timestamp=None, nonce=None):
    request = dict(command=command, timestamp=int(time.time()) if timestamp is None else timestamp,
                   nonce=secrets.token_hex(24) if nonce is None else nonce)
    request['signature'] = hmac.new(password.encode(), encode(request), hashlib.sha256).hexdigest()
    return request


def verify_request(password, request, now=None):
    if not isinstance(request, dict) or set(request) != {'command', 'timestamp', 'nonce', 'signature'}:
        return False
    stamp, nonce, signature = request['timestamp'], request['nonce'], request['signature']
    if (type(stamp) is not int or abs((time.time() if now is None else now)-stamp) > 60
            or not isinstance(nonce, str) or len(nonce) != 48
            or any(c not in '0123456789abcdef' for c in nonce)
            or not isinstance(signature, str) or len(signature) != 64
            or not isinstance(request['command'], list)
            or not 1 <= len(request['command']) <= 3
            or any(not isinstance(v, str) or len(v) > 256 for v in request['command'])):
        return False
    unsigned = {k: v for k, v in request.items() if k != 'signature'}
    expected = hmac.new(password.encode(), encode(unsigned), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def receive(sock, limit):
    # One connection carries one newline-terminated frame; never buffer a
    # request or response without a fixed bound.
    data = bytearray()
    while len(data) <= limit:
        part = sock.recv(min(4096, limit+1-len(data)))
        if not part:
            raise ConnectionError('服务未返回响应（连接关闭或鉴权失败）')
        data.extend(part)
        if b'\n' in data:
            line, trailing = data.split(b'\n', 1)
            if trailing or len(line) > limit:
                raise ValueError('非法控制消息')
            return json.loads(line)
    raise ValueError('控制消息过大')
