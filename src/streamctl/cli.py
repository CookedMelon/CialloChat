import argparse
import copy
import getpass
import json
import os
import subprocess
from pathlib import Path
import sys
import urllib.parse
from .accounts import new_account, new_password, hash_password, timestamp, credentials, matches
from .config import Store, dump, atomic_write, render, check_tls, exclusive_json
from .service import Service, commit, recover, backup, restore, migrate
from .diagnostics import doctor, check_ports


def password(args, generated=True):
    if getattr(args, 'password_stdin', False):
        value = sys.stdin.readline().rstrip('\r\n')
    elif getattr(args, 'prompt_password', False) or not generated:
        value = getpass.getpass('密码: ')
    else:
        value = new_password()
    from .accounts import validate_password
    return validate_password(value)


def print_credentials(settings, username, value=None, read_key=None):
    print(dump(credentials(settings, username, value, read_key)), end='')


def read_password(args):
    if args.read_key_stdin:
        value = sys.stdin.readline().rstrip('\r\n')
    elif args.prompt_read_key:
        value = getpass.getpass('观看密钥: ')
    else:
        value = new_password()
    from .accounts import validate_password
    return validate_password(value)


def parser():
    p = argparse.ArgumentParser(description='CialloChat 视频直播推流管理')
    p.add_argument('--runtime', help='独立运行目录，默认 runtime 或 CIALLOCHAT_RUNTIME')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init'); init.add_argument('--mode', choices=['local', 'production'], default='local')
    user = sub.add_parser('user').add_subparsers(dest='action', required=True)
    user.add_parser('list')
    for action in ('add', 'reset-password', 'reset-publish-key', 'reset-read-key', 'disable', 'enable', 'delete', 'info', 'credentials'):
        u = user.add_parser(action); u.add_argument('username')
        if action in ('add', 'reset-password', 'reset-publish-key', 'reset-read-key', 'credentials'):
            group = u.add_mutually_exclusive_group()
            group.add_argument('--password-stdin', action='store_true')
            group.add_argument('--prompt-password', action='store_true')
        if action == 'add':
            read = u.add_mutually_exclusive_group()
            read.add_argument('--read-key-stdin', action='store_true')
            read.add_argument('--prompt-read-key', action='store_true')
        if action == 'credentials':
            u.add_argument('--kind', choices=['publish','read'], default='publish')
    imp = user.add_parser('import'); imp.add_argument('file', help='JSON 数组文件，或 - 从 stdin 读取')
    imp.add_argument('--credentials-file')
    migration = user.add_parser('migrate'); migration.add_argument('--credentials-file', required=True)
    for name in ('up', 'apply'):
        s = sub.add_parser(name); s.add_argument('--mode', choices=['local', 'production'])
    for name in ('down', 'status', 'config-check'):
        sub.add_parser(name)
    logs = sub.add_parser('logs'); logs.add_argument('--follow', action='store_true'); logs.add_argument('--tail', type=int, default=100)
    b = sub.add_parser('backup'); b.add_argument('file'); b.add_argument('--include-certificates', action='store_true')
    r = sub.add_parser('restore'); r.add_argument('file'); r.add_argument('--migrate-credentials-file')
    d = sub.add_parser('doctor'); d.add_argument('--with-validation', action='store_true')
    sub.add_parser('certificate-reload')
    sub.add_parser('sessions')
    mail = sub.add_parser('mail').add_subparsers(dest='action', required=True)
    mail.add_parser('status')
    configure = mail.add_parser('configure')
    configure.add_argument('file', help='权限 600 的 SMTP JSON；不发送测试邮件')
    limits = sub.add_parser('limits')
    limits.add_argument('--publish-kbps', type=int, help='每路推流最大平均 Kbps，运行中可调整')
    return p


def execute(args):
    store = Store(args.runtime)
    if args.command == 'init':
        store.initialize(args.mode)
        print('CialloChat 初始化完成；未创建默认推流账号。')
        return 0
    if args.command == 'doctor':
        checks = doctor(store, args.with_validation)
        print(dump(checks), end='')
        return 0 if all(c['passed'] for c in checks) else 1
    if not (store.path / 'settings.json').exists():
        raise ValueError('尚未初始化，请运行 ./setup.sh 或 ./streamctl init')
    # Read interactive input before acquiring the mutation lock. A forgotten
    # password prompt or incomplete stdin must not block another administrator
    # from revoking an account.
    supplied_password = None
    supplied_read_key = None
    incoming = None
    if args.command == 'user':
        if args.action in ('add', 'reset-password', 'reset-publish-key', 'reset-read-key', 'credentials'):
            supplied_password = password(args, args.action != 'credentials')
            if args.action == 'add':
                supplied_read_key = read_password(args)
        elif args.action == 'import':
            if args.file == '-':
                incoming = json.load(sys.stdin)
            else:
                file = Path(args.file)
                if file.stat().st_mode & 0o077:
                    raise ValueError('导入明文文件权限必须为 600')
                incoming = json.loads(file.read_text())
    if args.command == 'logs':
        # Take a consistent snapshot, then release the account lock before a
        # potentially unbounded log-follow session.
        with store.lock():
            recover(store)
            settings, accounts, control = store.load()
            service = Service(store, settings, control)
        service.compose(['logs', '--tail', str(args.tail)] + (['--follow'] if args.follow else []), capture=False)
        return 0
    with store.lock():
        if args.command == 'down':
            Service(store).down()
            (store.path/'pending-revocations.json').unlink(missing_ok=True)
            recover(store)
            print('服务已停止。')
            return 0
        recover(store)
        if args.command == 'user' and args.action == 'migrate':
            target = migrate(store, args.credentials_file)
            print('迁移完成，原推流密钥保留；观看凭据已保存：' + str(target))
            return 0
        settings, accounts, control = store.load()
        service = Service(store, settings, control)
        if args.command == 'user':
            users = accounts['users']
            if args.action == 'list':
                print(dump([{k: v for k, v in u.items() if not k.endswith('_hash')} for u in users]), end='')
                return 0
            if args.action == 'import':
                if not isinstance(incoming, list) or not incoming:
                    raise ValueError('导入必须为非空 JSON 数组')
                seen = {u['username'] for u in users}
                pending = []
                handoff = []
                generated = False
                for entry in incoming:
                    name = entry['username']
                    if name in seen:
                        raise ValueError('重复用户名，整批未提交: ' + name)
                    seen.add(name)
                    publish_key = entry.get('publish_key', entry.get('password'))
                    read_key = entry.get('read_key')
                    generated |= publish_key is None or read_key is None
                    publish_key = new_password() if publish_key is None else publish_key
                    read_key = new_password() if read_key is None else read_key
                    pending.append(new_account(name, publish_key, read_key))
                    handoff.append(credentials(settings, name, publish_key, read_key))
                if generated and not args.credentials_file:
                    raise ValueError('自动生成密钥的导入必须指定 --credentials-file 交付，整批未提交')
                target = exclusive_json(args.credentials_file, handoff) if args.credentials_file else None
                accounts['users'] += pending
                try:
                    commit(store, accounts, service=service)
                except Exception:
                    if target: target.unlink(missing_ok=True)
                    raise
                print(f'已导入 {len(pending)} 个账号。' + (f' 凭据文件：{target}' if target else ''))
                return 0
            u = next((u for u in users if u['username'] == args.username), None)
            if args.action == 'add':
                if u:
                    raise ValueError('用户名已存在')
                value = supplied_password
                users.append(new_account(args.username, value, supplied_read_key))
                commit(store, accounts, service=service)
                print_credentials(settings, args.username, value, supplied_read_key)
                return 0
            if u is None:
                raise ValueError('账号不存在')
            if args.action == 'info':
                print(dump({k: v for k, v in u.items() if not k.endswith('_hash')}), end='')
                return 0
            if args.action == 'credentials':
                value = supplied_password
                field = 'read_key_hash' if args.kind == 'read' else 'publish_key_hash'
                if not matches(u[field], value):
                    raise ValueError('密钥错误')
                print_credentials(settings, args.username, value if args.kind == 'publish' else None,
                                  value if args.kind == 'read' else None)
                return 0
            revoke = []
            states = ('publish',)
            if args.action in ('reset-password', 'reset-publish-key', 'reset-read-key'):
                value = supplied_password
                read_reset = args.action == 'reset-read-key'
                other = 'publish_key_hash' if read_reset else 'read_key_hash'
                if matches(u[other], value):
                    raise ValueError('推流密钥与观看密钥必须不同')
                u['read_key_hash' if read_reset else 'publish_key_hash'] = hash_password(value)
                revoke.append(u['stream_path'])
                states = ('read',) if read_reset else ('publish',)
            elif args.action == 'disable':
                u['enabled'] = False; revoke.append(u['stream_path']); states = ('publish','read')
            elif args.action == 'enable':
                u['enabled'] = True
            elif args.action == 'delete':
                users.remove(u); revoke.append(u['stream_path']); states = ('publish','read')
            u['updated_at'] = timestamp()
            commit(store, accounts, revoke=revoke, service=service, revoke_states=states)
            if args.action == 'delete':
                from .leases import Leases
                with Leases(store.path/'leases/leases.sqlite3').connection() as db:
                    db.execute('DELETE FROM login_history WHERE username=?', (args.username,))
            if args.action in ('reset-password', 'reset-publish-key'):
                print_credentials(settings, args.username, value)
            elif args.action == 'reset-read-key':
                print_credentials(settings, args.username, read_key=value)
            else:
                print('账号操作已完成。')
        elif args.command == 'limits':
            if args.publish_kbps is not None:
                settings = copy.deepcopy(settings)
                settings['publish_limit_kbps'] = args.publish_kbps
                commit(store, settings=settings, service=service)
            print(dump({'publish_limit_kbps': settings.get('publish_limit_kbps', 4000),
                        'publish_session_seconds': 7200, 'renewal_notice_seconds': 600,
                        'scope': 'per publisher; audio, video and ingress protocol bytes',
                        'window_seconds': 5, 'poll_seconds': 1, 'consecutive_samples': 2}), end='')
        elif args.command == 'sessions':
            from .leases import Leases
            print(dump(Leases(store.path/'leases/leases.sqlite3').statuses()), end='')
        elif args.command == 'mail':
            from .leases import validate_mail
            target = store.path/'notifications/smtp.json'
            if args.action == 'configure':
                source = Path(args.file)
                if source.stat().st_mode & 0o077 or source.stat().st_size > 16384:
                    raise ValueError('SMTP 配置文件需权限 600，且不超过 16 KiB')
                config = validate_mail(json.loads(source.read_text()))
                atomic_write(target, dump(config))
                print('SMTP 配置已保存；临近到期时发送续期邮件。')
            else:
                config = validate_mail(json.loads(target.read_text())) if target.exists() else None
                print(dump({'configured': config is not None,
                            'users': sorted(config['recipients']) if config else []}), end='')
        elif args.command in ('up', 'apply'):
            changed = copy.deepcopy(settings)
            if args.mode:
                changed['mode'] = args.mode
            commit(store, settings=changed, service=service)
            if args.command == 'up':
                service = Service(store)
                if not service.running():
                    check_ports(changed)
                service.up()
            print('配置已确认生效。' if args.command == 'up' or service.running() else '配置已生成；服务未运行。')
        elif args.command == 'down':
            service.down()
            (store.path/'pending-revocations.json').unlink(missing_ok=True)
            recover(store)
            print('服务已停止。')
        elif args.command == 'status':
            running = service.running()
            result = {'container_running': running and settings.get('service_backend', 'docker') == 'docker', 'service_running': running,
                      'service_backend': settings.get('service_backend', 'docker'),
                      'api_available': False, 'paths': []}
            if settings.get('service_backend') == 'systemd':
                from .native import manager, units
                result['components'] = {name: subprocess.run(manager(settings)+['is-active', '--quiet', name],
                    capture_output=True, timeout=5).returncode == 0 for name in units(settings)}
            if running:
                try:
                    result['paths'] = [{k: p.get(k) for k in ('name', 'ready', 'tracks', 'readers')} for p in service.api('paths/list?itemsPerPage=10000')['items']]
                    result['api_available'] = True
                except OSError:
                    pass
            print(dump(result), end='')
            return 0 if result['api_available'] and all(result.get('components', {}).values()) else 1
        elif args.command == 'config-check':
            generated = render(settings, accounts, control)
            if generated != (store.path / 'mediamtx/mediamtx.yml').read_text():
                raise ValueError('生成配置与账号不一致；执行 apply')
            if settings['mode'] == 'production':
                check_tls(store, settings)
            service.compose(['config', '--quiet'])
            print('账号、设置、TLS（如适用）和服务配置检查通过。')
        elif args.command == 'backup':
            print(backup(store, args.file, args.include_certificates))
        elif args.command == 'restore':
            restore(store, args.file, args.migrate_credentials_file); print('恢复完成；请执行 up。')
        elif args.command == 'certificate-reload':
            service.reload_certificate()
            print('服务已重启并验证新证书；活动直播需重新连接。')
    return 0


def main():
    args = parser().parse_args()
    try:
        return execute(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Never print raw client URLs, account records, or password-bearing requests.
        from argon2.exceptions import Argon2Error
        if isinstance(exc, Argon2Error):
            message = '密码/哈希验证失败'
        elif isinstance(exc, (ValueError, RuntimeError, FileNotFoundError, PermissionError)):
            message = str(exc)
        else:
            message = type(exc).__name__ + '；请检查输入文件、依赖与服务状态'
        print('CialloChat: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
