#!/usr/bin/env python3
"""Run an expiring test matrix, preserving every account not owned by this run."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--hours", type=float, default=48)
    parser.add_argument("--publish-loopback", action="store_true",
                        help="Publish locally while verifying TLS against the configured public hostname")
    args = parser.parse_args()
    if not 0 < args.hours <= 168:
        parser.error("hours must be greater than 0 and no more than 168")
    sys.path.insert(0, str(args.project_root / "src"))
    from streamctl.accounts import credentials, new_account, new_password
    from streamctl.config import Store, atomic_write, dump
    from streamctl.service import Service, commit

    manifest = json.loads((args.fixtures / "manifest.json").read_text())
    import hashlib
    for fixture in manifest["fixtures"]:
        file = (args.fixtures / fixture["file"]).resolve()
        if not file.is_relative_to(args.fixtures.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest() != fixture["sha256"]:
            raise RuntimeError("fixture checksum mismatch")
    store = Store(args.runtime)
    service = Service(store)
    owned = []
    processes = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    expires = datetime.now(timezone.utc) + timedelta(hours=args.hours)
    deadline = time.monotonic() + args.hours * 3600
    created = False
    try:
        with store.lock():
            settings, accounts, _ = store.load()
            names = {x["name"] for x in manifest["fixtures"]}
            if any(x["username"] in names for x in accounts["users"]):
                raise RuntimeError("test account already exists; refusing to overwrite it")
            updated = copy.deepcopy(accounts)
            entries = []
            publishers = []
            for fixture in manifest["fixtures"]:
                publish_key, read_key = new_password(), new_password()
                account = new_account(fixture["name"], publish_key, read_key)
                owned.append(account)
                updated["users"].append(account)
                urls = credentials(settings, fixture["name"], publish_key, read_key)
                entries.append(dict(fixture, read_url=urls["read_url"], vrchat_read_url=urls["vrchat_read_url"]))
                publisher = urls["publish_url"]
                publishers.append((fixture, publisher))
            commit(store, accounts=updated, service=service)
            created = True
        atomic_write(args.handoff, dump(dict(schema=1, expires_utc=expires.isoformat(), loop_seconds=manifest["loop_seconds"], streams=entries)))
        logs = args.handoff.parent / "publisher-logs"
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        for fixture, publisher in publishers:
            tls = ["-tls_verify", "1", "-ca_file", "/etc/ssl/certs/ca-certificates.crt"] if publisher.startswith("rtmps://") else []
            if args.publish_loopback:
                parts = urlsplit(publisher)
                if tls:
                    tls += ["-verifyhost", parts.hostname]
                publisher = urlunsplit(parts._replace(netloc=f"127.0.0.1:{parts.port}"))
            with (logs / (fixture["name"] + ".log")).open("wb") as log:
                process = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                                            "-stream_loop", "-1", "-re", "-i", str(args.fixtures / fixture["file"]),
                                            "-map", "0:v:0", "-map", "0:a?", "-c", "copy", *tls,
                                            "-f", "flv", publisher], stdout=log, stderr=log)
            processes.append((fixture["name"], process))
        ready_deadline = time.monotonic() + 40
        while not stopping:
            if any(p.poll() is not None for _, p in processes):
                raise RuntimeError("a diagnostic publisher exited; see private publisher logs")
            paths = service.api("paths/list?itemsPerPage=10000")["items"]
            ready = {x["name"] for x in paths if x.get("ready")}
            if all("live/" + x["name"] in ready for x in manifest["fixtures"]):
                break
            if time.monotonic() > ready_deadline:
                raise RuntimeError("diagnostic streams did not become ready")
            time.sleep(.5)
        if not stopping:
            print(dump(dict(ready_streams=len(entries), expires_utc=expires.isoformat(), transcoding_on_server=False)), flush=True)
        while not stopping and time.monotonic() < deadline:
            if any(p.poll() is not None for _, p in processes):
                raise RuntimeError("a diagnostic publisher exited; see private publisher logs")
            time.sleep(1)
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if created:
            with store.lock():
                _, accounts, _ = store.load()
                hashes = {x["username"]: x["publish_key_hash"] for x in owned}
                removed = [x for x in accounts["users"] if hashes.get(x["username"]) == x["publish_key_hash"]]
                accounts["users"] = [x for x in accounts["users"] if x not in removed]
                commit(store, accounts=accounts, revoke=[x["stream_path"] for x in removed],
                       revoke_states=("publish", "read"), service=service)
        print("Diagnostic publishers stopped; only owned test accounts were removed.", flush=True)


if __name__ == "__main__":
    main()
