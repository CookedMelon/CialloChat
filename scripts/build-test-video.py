#!/usr/bin/env python3
"""Build one reusable 10-minute 1440p60 H264/AAC motion/countdown test asset."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--font', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'runtime/test-video/test.mp4')
    parser.add_argument('--seconds', type=int, default=600, help='默认完整十分钟；短片仅供隔离验证')
    parser.add_argument('--threads', type=int, default=min(8, os.cpu_count() or 2))
    args = parser.parse_args()
    args.ffmpeg = args.ffmpeg or shutil.which('ffmpeg')
    if not args.ffmpeg:
        args.ffmpeg = str(ROOT/'runtime/tools/ffmpeg-root/usr/bin/ffmpeg')
        library = ROOT/'runtime/tools/ffmpeg-root/usr/lib/x86_64-linux-gnu'
        os.environ['LD_LIBRARY_PATH'] = ':'.join(str(p) for p in (library,library/'pulseaudio',library/'samba'))
    if not Path(args.ffmpeg).is_file() and not shutil.which(args.ffmpeg): parser.error('FFmpeg not found')
    if not 1 <= args.seconds <= 600: parser.error('duration outside allowed range')
    if args.output.exists(): parser.error('output already exists; choose an empty destination')
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidates = [Path('/mnt/c/Windows/Fonts/msyh.ttc'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
                  ROOT/'runtime/tools/ffmpeg-root/usr/share/fonts/opentype/freefont/FreeSans.otf']
    font = args.font or next((p for p in candidates if p.exists()), None)
    if not font or not font.is_file(): parser.error('font not found; supply --font')
    with tempfile.TemporaryDirectory(prefix='ciallochat-video-') as directory:
        work = Path(directory)
        def text(name, value, size, color, x, y):
            path = work/(name+'.txt'); path.write_text(value)
            return f"drawtext=fontfile='{font}':textfile='{path}':fontsize={size}:fontcolor={color}:x='{x}':y='{y}'"
        filters = ['drawgrid=width=160:height=160:thickness=1:color=0x223344',
            text('title', 'CialloChat / LIVE MOTION TEST', 72, 'white', '90', '90'),
            text('spec', '2560 x 1440   |   x264   |   60 FPS   |   CBR 1600 Kbps', 42, '0x93c5fd', '94', '190'),
            text('countdown', 'DISCONNECT IN  %{eif:floor(ceil(max(0,599-t))/60):d:2}:%{eif:mod(ceil(max(0,599-t)),60):d:2}',
                 100, '0xfbbf24', '90', '285'),
            text('frame', 'FRAME  %{eif:n:d:5}     MEDIA TIME  %{pts:hms}', 40, '0x94a3b8', '96', '425')]
        for i,(speed,color) in enumerate([(170,'0x38bdf8'),(290,'0x86efac'),(410,'0xfda4af'),(230,'0xc4b5fd')]):
            sentence = ['SMOOTH SCROLL  >>>  CialloChat  >>>  60 FPS',
                        'Every frame moves - watch for pauses or jumps',
                        'One second keyframes / continuous audio test',
                        'Motion test  <<<  Motion test  <<<  Motion test'][i]
            filters.append(text('barrage'+str(i),sentence,58,color,
                f'w-mod(t*{speed}+{i*620},w+text_w)',str(610+i*165)))
        filters.append(text('footer','10 min per connection window / 5 min cooldown / 60 min per IP per day',
                            32,'0x94a3b8','90','1350'))
        graph=work/'video.filter'; graph.write_text(',\n'.join(filters))
        temporary = args.output.with_suffix('.building.mp4')
        command=[args.ffmpeg,'-hide_banner','-v','error','-nostdin','-f','lavfi','-i','color=c=0x0b1220:s=2560x1440:r=60',
            '-f','lavfi','-i',r'aevalsrc=0.14*sin(2*PI*(440+110*mod(floor(t)\,4))*t)*(0.4+0.6*pow(sin(PI*mod(t\,1))\,2)):s=48000',
            '-t',str(args.seconds),'-filter_script:v',str(graph),'-c:v','libx264','-threads',str(args.threads),
            '-preset','veryfast','-tune','zerolatency','-pix_fmt','yuv420p','-profile:v','high','-level:v','5.1',
            '-b:v','1600k','-minrate','1600k','-maxrate','1600k','-bufsize','1600k',
            '-x264-params','bframes=0:sliced-threads=0:slices=1:keyint=60:min-keyint=60:scenecut=0:nal-hrd=cbr:force-cfr=1',
            '-c:a','aac','-ar','48000','-ac','2','-b:a','128k','-movflags','+faststart',
            '-progress',str(args.output.with_suffix('.progress')),str(temporary)]
        try:
            subprocess.run(command,check=True)
            temporary.chmod(0o600); temporary.replace(args.output)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps(dict(asset=str(args.output),width=2560,height=1440,fps=60,
                         video_kbps=1600,audio_kbps=128,duration=args.seconds,bytes=args.output.stat().st_size)))


if __name__ == '__main__': main()
