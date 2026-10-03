"""Content-addressed tag for the locally built admission image."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def auth_image():
    digest = hashlib.sha256()
    for name in ('.dockerignore', 'docker/auth.Dockerfile', 'requirements.lock',
                 'src/streamctl/authserver.py', 'src/streamctl/watchdog.py'):
        digest.update(name.encode() + b'\0' + (ROOT / name).read_bytes() + b'\0')
    return 'ciallochat-auth:' + digest.hexdigest()


if __name__ == '__main__':
    print(auth_image())
