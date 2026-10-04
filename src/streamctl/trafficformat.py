"""Shared report text formatting, standard library only."""
from datetime import datetime, timezone, timedelta
from .trafficpricing import price_label


def format_bytes(count):
    return f'{count / 1_000_000:.3f} MB ({count:,} B)'


def display_time(report, timestamp):
    offset = report.get('timezone_offset_minutes')
    zone = timezone(timedelta(minutes=offset)) if offset is not None else None
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(zone)


def summary(report):
    stamp = lambda t: display_time(report,t).isoformat(timespec='minutes')
    lines = [f'CialloChat流量报告：{stamp(report["start"])} 至 {stamp(report["end"])}',
             '']
    for user, value in sorted(report['totals'].items()):
        lines.append(f'{user}：推流 {format_bytes(value["push"])}；观看 {format_bytes(value["pull"])}；'
                     f'合计 {format_bytes(value["push"]+value["pull"])}')
    lines += ['', f'推流总计：{format_bytes(report["push"])}',
              f'观看总计：{format_bytes(report["pull"])}',
              f'全部合计：{format_bytes(report["push"]+report["pull"])}',
              price_label(report)]
    return '\n'.join(lines)
