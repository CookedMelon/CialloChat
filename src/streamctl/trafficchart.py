"""Client-only small PNG plotting; Pillow is never imported on the server."""
from datetime import datetime
import math
from pathlib import Path
from .trafficpricing import price_label
from .trafficformat import display_time


def render(report, path):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ValueError('traffic 图表需要客户端 Pillow；请在运行 cialloctl 的 Python 环境安装 Pillow，服务器无需安装') from None
    fonts = ['/mnt/c/Windows/Fonts/msyh.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
             '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    selected = next((font for font in fonts if Path(font).exists()), None)
    font = ImageFont.truetype(selected, 15) if selected else ImageFont.load_default(size=15)
    title_font = ImageFont.truetype(selected, 20) if selected else font
    users = sorted(report['totals'])
    # Legend plus finite bar count; long ranges aggregate without dropping data.
    group = max(1, math.ceil(report['bucket_count']/48))
    step = group*600
    bars = math.ceil((report['end']-report['start'])/step)
    values = [dict() for _ in range(bars)]
    for row in report['series']:
        index = (row['start']-report['start'])//step
        for kind in ('push', 'pull'):
            key = (row['username'], kind)
            values[index][key] = values[index].get(key, 0)+row[kind]
    # Cap legend height while retaining every account in the chart and text.
    legend_users = users[:40]
    height = min(1200, 470 + math.ceil(len(legend_users)*2/4)*25)
    image = Image.new('RGB', (1200, height), 'white'); draw = ImageDraw.Draw(image)
    stamp = lambda t: display_time(report,t).strftime('%Y-%m-%d %H:%M %z')
    draw.text((35, 16), 'CialloChat traffic | '+stamp(report['start'])+' - '+stamp(report['end']), font=title_font, fill='#223344')
    has_cjk = selected and ('msyh' in selected or 'CJK' in selected)
    cost_text = price_label(report) if has_cjk else 'Estimated cost: CNY ' + (report.get('estimated_cny') or 'not configured')
    draw.text((35, 48), cost_text, font=title_font, fill='#223344')
    left, right, top, bottom = 95, 1170, 100, 380
    maximum = max(1, max((sum(bar.values()) for bar in values), default=0)) * 1.1
    complete = {v['start'] for v in report['coverage'] if v['seconds'] >= 598 and not v['incomplete']}
    import colorsys
    palette = {}
    for index, user in enumerate(users):
        hue = (index*0.61803398875) % 1
        for kind, light in (('push', 0.65), ('pull', 0.93)):
            rgb = colorsys.hsv_to_rgb(hue, 0.65 if kind == 'push' else 0.33, light)
            palette[(user, kind)] = tuple(int(v*255) for v in rgb)
    bar_width = (right-left)/bars
    for index, bar in enumerate(values):
        x = left+index*bar_width
        begin = report['start']+index*step
        end = min(begin+step, report['end'])
        if any(t not in complete for t in range(begin, end, 600)):
            draw.rectangle((x, top, x+bar_width, bottom), fill='#eeeeee')
        y = bottom
        for key in sorted(bar):
            amount = bar[key]/maximum*(bottom-top)
            if amount:
                draw.rectangle((x+bar_width*0.15, y-amount, x+bar_width*0.85, y), fill=palette[key])
                y -= amount
    for i in range(5):
        y = bottom-(bottom-top)*i/4
        draw.line((left, y, right, y), fill='#c9cdd1', width=1)
        draw.text((8, y-9), f'{maximum*i/4/1_000_000:.2f}', font=font, fill='#444444')
    draw.text((8, top-25), 'MB', font=font, fill='#444444')
    for index in range(0, bars, max(1, math.ceil(bars/8))):
        stamp_value = report['start']+index*step
        label = display_time(report,stamp_value).strftime('%m-%d %H:%M' if report['end']-report['start'] >= 86400 else '%H:%M')
        draw.text((left+index*bar_width, bottom+10), label, font=font, fill='#444444')
    draw.text((35, 420), f'Push: {report["push"]/1_000_000:.3f} MB   View: {report["pull"]/1_000_000:.3f} MB   Total: {(report["push"]+report["pull"])/1_000_000:.3f} MB', font=title_font, fill='#223344')
    legend = [(user, kind) for user in legend_users for kind in ('push', 'pull')]
    for index, key in enumerate(legend):
        x, y = 35+(index % 4)*290, 463+(index//4)*25
        draw.rectangle((x, y+3, x+16, y+16), fill=palette[key])
        draw.text((x+23, y), key[0]+' / '+('push' if key[1] == 'push' else 'view'), font=font, fill='#333333')
    if len(users) > len(legend_users):
        draw.text((35, height-22), 'Additional accounts: see complete text totals.', font=font, fill='#555555')
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Palette PNG keeps upload/email very small without any lossy compression.
    image.quantize(colors=128).save(path, format='PNG', optimize=True)
    path.chmod(0o600)
    if path.stat().st_size > 256*1024:
        path.unlink()
        raise ValueError('生成图表超出 256 KiB 上限，请缩短查询区间')
    return path
