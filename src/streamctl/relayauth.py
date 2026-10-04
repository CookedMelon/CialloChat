"""Bind a private RTSP relay request to the reader's actual source address."""
import hashlib
import hmac
import ipaddress
from pathlib import Path
import time
import urllib.parse

FIELDS = ('_cc_peer', '_cc_time', '_cc_sig')


def load_secret(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError('RTSP relay secret requires permission 600')
    secret = path.read_bytes()
    if len(secret) != 32:
        raise ValueError('invalid RTSP relay secret')
    return secret


def signed_query(query, peer, path, secret):
    query = {k: v for k, v in query.items() if k not in FIELDS}
    stamp = str(int(time.time()))
    canonical = urllib.parse.urlencode(sorted(query.items()), doseq=True)
    signature = hmac.new(secret, f'{peer}\n{stamp}\n{path}\n{canonical}'.encode(), hashlib.sha256).hexdigest()
    return dict(query, _cc_peer=[peer], _cc_time=[stamp], _cc_sig=[signature])


def reader_request(request, secret):
    """Return verified original peer/query; reject untrusted proxy metadata."""
    query = urllib.parse.parse_qs(request.get('query', ''), keep_blank_values=True,
                                 strict_parsing=True, max_num_fields=16, errors='strict')
    if not any(k in query for k in FIELDS):
        return request
    if (request.get('protocol') != 'rtsp' or request.get('action') != 'read'
            or not ipaddress.ip_address(request.get('ip', '')).is_loopback
            or any(len(query.get(k, [])) != 1 for k in FIELDS)):
        raise ValueError('untrusted RTSP relay metadata')
    peer, stamp, supplied = (query.pop(k)[0] for k in FIELDS)
    ipaddress.ip_address(peer)
    if abs(time.time() - int(stamp)) > 60:
        raise ValueError('expired RTSP relay metadata')
    canonical = urllib.parse.urlencode(sorted(query.items()), doseq=True)
    expected = hmac.new(secret, f'{peer}\n{stamp}\n{request["path"]}\n{canonical}'.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise ValueError('invalid RTSP relay signature')
    return dict(request, ip=peer, query=canonical)
