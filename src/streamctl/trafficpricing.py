"""User-supplied traffic prices, including monthly mainland CDT tiers."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import time


def nonnegative(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise ValueError
    return value


def monthly_bill(usage_gb, config):
    billable = max(Decimal(0), usage_gb-nonnegative(config['free_gb_per_month']))
    total = Decimal(0)
    previous = Decimal(0)
    tiers = config['tiers']
    if not isinstance(tiers, list) or not tiers:
        raise ValueError
    for index, tier in enumerate(tiers):
        limit = tier['up_to_gb']
        rate = nonnegative(tier['cny_per_gb'])
        if limit is None:
            if index != len(tiers)-1:
                raise ValueError
            total += max(Decimal(0), billable-previous)*rate
        else:
            limit = nonnegative(limit)
            if limit <= previous or index == len(tiers)-1:
                raise ValueError
            total += max(Decimal(0), min(billable,limit)-previous)*rate
            previous = limit
    return total


def price_report(report, path):
    path = Path(path)
    if not path.exists():
        report['estimated_cny'] = None
        return report
    try:
        config = json.loads(path.read_text())
        divisor = config['bytes_per_gb']
        if type(divisor) is not int or divisor <= 0:
            raise ValueError
        if config.get('model', 'flat') == 'cdt-mainland-bgp':
            # CDT mainland monthly boundaries are UTC+8. Stored monthly
            # counters survive the seven-day pruning of individual buckets.
            monthly_bill(Decimal(0),config)  # Validate the entire tier schedule.
            amounts = {}
            for row in report['series']:
                month = time.strftime('%Y-%m',time.gmtime(row['start']+8*3600))
                amounts[month] = amounts.get(month,0)+row['pull']
            offsets = config.get('monthly_usage_offset_gb', {})
            if not isinstance(offsets,dict):
                raise ValueError
            offsets = {key: nonnegative(value) for key,value in offsets.items()}
            cost = Decimal(0)
            for month,amount in amounts.items():
                before = Decimal(report['monthly_before'].get(month,0))/divisor+offsets.get(month,Decimal(0))
                cost += monthly_bill(before+Decimal(amount)/divisor,config)-monthly_bill(before,config)
        elif config.get('model', 'flat') == 'flat':
            push = nonnegative(config['push_cny_per_gb'])
            pull = nonnegative(config['pull_cny_per_gb'])
            cost = (Decimal(report['push'])*push+Decimal(report['pull'])*pull)/divisor
        else:
            raise ValueError
        report['estimated_cny'] = str(cost.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
        report['pricing'] = config
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ValueError('流量价格配置错误，请检查 runtime/traffic/pricing.json') from None
    return report


def price_label(report):
    value = report.get('estimated_cny')
    return '预计价格：' + ('¥' + value if value is not None else '未配置')
