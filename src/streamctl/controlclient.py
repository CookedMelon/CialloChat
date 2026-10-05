"""Standalone client: standard library only, no password on the command line."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import ssl
import sys
import unicodedata

from .controlprotocol import encode, load_config, MAX_RESPONSE, receive, sign_request


def format_users_table(users):
    if not users:
        return '暂无用户。'
    rows = [['用户名', '邮箱', '推流密码', '剩余有效时长', '观看密码', '最近登录时间', '是否正在推流']]
    for user in users:
        remaining, state = user['push_seconds_remaining'], user['push_state']
        if state == 'not_started':
            duration = '未开始（2小时）'
        elif state == 'renewal_ready':
            duration = '待续期（2小时）'
        elif state == 'expired':
            duration = '已过期'
        elif state == 'disabled':
            duration = '已禁用'
        elif remaining is not None:
            duration = f'{remaining//3600:02}:{remaining%3600//60:02}:{remaining%60:02}'
        else:
            duration = '不可用'
        last_login = user.get('last_login')
        login_time = (datetime.fromtimestamp(last_login, timezone.utc).astimezone()
                      .strftime('%Y-%m-%d %H:%M:%S %z')
                      if last_login is not None else '从未登录')
        streaming = {True: '是', False: '否', None: '未知'}[user.get('is_streaming')]
        rows.append([user['username'], user['email'] or '未配置',
                     user['push_password'] or '已过期或未保存', duration,
                     user['pull_password'] or '未保存', login_time, streaming])
    # Count terminal cells rather than characters so Chinese headings align.
    # Escape control characters to keep every user on a single table row.
    rows = [[''.join(c if c.isprintable() else ascii(c)[1:-1] for c in str(cell))
             for cell in row] for row in rows]
    def width(value):
        return sum(0 if unicodedata.combining(c) else
                   2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in value)
    widths = [max(width(row[i]) for row in rows) for i in range(len(rows[0]))]
    border = '+' + '+'.join('-' * (size+2) for size in widths) + '+'
    def line(row):
        return '| ' + ' | '.join(cell + ' ' * (size-width(cell))
                                for cell, size in zip(row, widths)) + ' |'
    return '\n'.join([border, line(rows[0]), border, *map(line, rows[1:]), border])


def call(config, command, image=None):
    context = ssl.create_default_context(cafile=config.get('ca_file'))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    host = config['host']
    with socket.create_connection((host, config.get('port', 15347)), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=config.get('server_name', host)) as tls:
            tls.settimeout(90)
            tls.sendall(encode(sign_request(config['password'], command)) + b'\n')
            result = receive(tls, MAX_RESPONSE)
            if image is not None and result.get('upload') is True:
                tls.sendall(image)
                result = receive(tls, MAX_RESPONSE)
    if not isinstance(result, dict) or type(result.get('ok')) is not bool:
        raise ValueError('非法服务器响应')
    if not result['ok']:
        raise ValueError(result.get('error', '命令执行失败'))
    return result


def main():
    parser = argparse.ArgumentParser(
        description='CialloChat 远程用户控制；管理密码自动从配置文件读取。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例：\n'
               '  cialloctl list\n'
               '  cialloctl info alice\n'
               '  cialloctl traffic\n'
               '  cialloctl traffic --start "2026-10-04 09:00" --end "2026-10-04 12:00"\n'
               '  cialloctl add alice alice@example.com\n'
               '  cialloctl del alice\n'
               '  cialloctl refresh alice all\n'
               '  cialloctl refresh alice push\n'
               '  cialloctl refresh alice pull\n'
               '  cialloctl refresh alice time\n'
               '  cialloctl --json list\n'
               '  cialloctl help refresh')
    parser.add_argument('--config', default=str(Path(__file__).resolve().parents[2]/'config/control-client.json'),
                        help='私有管理配置文件，默认使用项目中的 config/control-client.json')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出结果，适合脚本处理')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list', help='表格列出用户、邮箱、密码、剩余时长、最近登录及推流状态',
                   description='表格列出所有用户及凭据；剩余时长为累计推流额度，停推暂停。可使用 cialloctl --json list 输出原始字段。')
    info = sub.add_parser('info', help='显示指定用户的 OBS 推流 URL 和播放器输入 URL',
                          description='显示当前有效密码对应的完整 URL；查询不会刷新密码或开始推流计时。')
    info.add_argument('user', help='用户名')
    delete = sub.add_parser('del', help='删除用户并撤销推流及观看连接')
    delete.add_argument('user', help='用户名')
    add = sub.add_parser('add', help='创建用户及两类密码，并发送通知邮件')
    add.add_argument('user', help='用户名'); add.add_argument('email', help='接收密码通知的邮箱')
    refresh = sub.add_parser('refresh', help='刷新密码或恢复推流使用时长，并发送通知邮件',
                             description='all：刷新全部密码；push：仅刷新推流密码；pull：仅刷新观看密码；\n'
                                         'time：保留当前密码，将推流剩余使用时长恢复至两小时。\n'
                                         'all/push 重置为两小时累计推流额度，停推不计时；pull 保留推流剩余额度。')
    refresh.add_argument('user', help='用户名')
    refresh.add_argument('kind', choices=['all', 'push', 'pull', 'time'], help='刷新范围：全部、推流、观看密码，或仅恢复推流时长')
    traffic = sub.add_parser('traffic', help='最近一小时的流量柱状图、文字总计及管理员邮件',
                             description='默认查询最近六个已结束的十分钟区间；自定义时间限最近七天，需按整十分钟对齐。')
    traffic.add_argument('--start', help='开始时间，例如 2026-10-04 09:00；默认使用本机时区，可附 +09:00')
    traffic.add_argument('--end', help='结束时间，例如 2026-10-04 12:00；须与 --start 同时提供')
    traffic.add_argument('--output', help='PNG 输出路径；默认保存到项目 runtime/reports')
    help_command = sub.add_parser('help' , help='查看整体帮助或某个命令的帮助')
    help_command.add_argument('topic', nargs='?', choices=['list', 'info', 'del', 'add', 'refresh', 'traffic'],
                             help='可选：要查看的命令')
    args = parser.parse_args()
    if args.action == 'help':
        (sub.choices[args.topic] if args.topic else parser).print_help()
        return 0
    command = [args.action]
    if args.action not in ('list', 'traffic'):
        command.append(args.user)
    if args.action == 'add': command.append(args.email)
    if args.action == 'refresh': command.append(args.kind)
    try:
        config = load_config(args.config)
        if args.action == 'traffic':
            if bool(args.start) != bool(args.end):
                raise ValueError('--start 和 --end 须同时提供')
            if args.start:
                try:
                    command += [str(int(datetime.fromisoformat(value).timestamp()))
                                for value in (args.start, args.end)]
                except ValueError:
                    raise ValueError('时间格式错误，请参考 cialloctl help traffic') from None
            # Check client plotting support before creating a server report.
            try:
                import PIL
            except ImportError:
                raise ValueError('traffic 需要在客户端 Python 环境安装 Pillow；服务器无需安装') from None
        result = call(config, command)
        if args.action == 'traffic':
            import hashlib
            from .trafficchart import render
            from .trafficformat import summary
            report_id = result['report_id']
            output = args.output or str(Path(__file__).resolve().parents[2]/'runtime/reports'/f'traffic-{report_id}.png')
            path = render(result['report'], output)
            image = path.read_bytes()
            delivery = call(config, ['traffic-mail', report_id, f'{len(image)}:{hashlib.sha256(image).hexdigest()}'], image)
            result.update(image_path=str(path.resolve()), email_status=delivery['email_status'])
            if not args.json:
                print(summary(result['report']))
                print('图表：'+str(path.resolve()))
                print('管理员邮件：'+('已发送' if delivery['email_status'] == 'sent' else '已排队，稍后自动重试'))
                return 0
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.action == 'list':
            print(format_users_table(result['users']))
        elif args.action == 'info':
            missing_push = {'expired': '推流密码已过期，请先刷新', 'disabled': '账号已禁用'}.get(
                result.get('push_state'), '推流密码未保存，请先刷新')
            print('OBS推流URL：' + (result.get('obs_publish_url') or missing_push))
            print('播放器输入URL：' + (result.get('player_input_url') or '观看密码未保存，请先刷新'))
        else:
            print(result['message'])
            if result.get('email_status') == 'queued':
                print('邮件暂未发出，已保存通知并自动重试。')
        return 0
    except ssl.SSLCertVerificationError:
        print('控制连接的 TLS 证书校验失败：请检查服务域名、证书、CA 配置和系统时间；未自动重复执行命令。', file=sys.stderr)
        return 1
    except ssl.SSLError:
        print('控制连接的 TLS 握手或通信失败：请检查 Python/OpenSSL 环境和网络代理；未自动重复执行命令。', file=sys.stderr)
        return 1
    except TimeoutError:
        print('控制连接或响应超时：请检查网络代理、控制端口和服务器状态；未自动重复执行命令。', file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, KeyError, ConnectionError):
        # Exception strings from TLS, SMTP, etc. must not echo secret data.
        print('控制命令失败：请检查配置、连接和服务器状态；未自动重复执行命令。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
