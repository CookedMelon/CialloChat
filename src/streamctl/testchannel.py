"""On-demand stream copies of one pre-encoded test asset; no live transcoding."""
import asyncio
import json
import math
from pathlib import Path
import shutil
import time

from .config import ROOT, Store
from .service import Service
from .testquota import TestDenied, TestQuota


class TestChannel:
    def __init__(self, runtime, settings, ffmpeg=None, quota=None):
        self.runtime = Path(runtime)
        options = json.loads((self.runtime/'test-video/config.json').read_text())
        self.asset = self.runtime/'test-video/test.mp4'
        self.ffmpeg = ffmpeg or options['ffmpeg']
        self.env = options.get('environment')
        self.settings = settings
        self.quota = quota or TestQuota(self.runtime/'test-video/quota.sqlite3')
        self.quota.recover()
        self.service = Service(Store(self.runtime))
        self.processes = {}

    async def acquire(self, ip, owner):
        if not self.asset.is_file(): raise TestDenied('test asset unavailable')
        grant = await asyncio.to_thread(self.quota.acquire,ip,owner)
        try:
            offset = max(0,math.floor(600-grant.remaining))
            url = (f'rtsp://ciallochat-test-publisher:{grant.publisher_password}@127.0.0.1:'
                   f'{self.settings.get("rtsp_internal_port", 18554)}/test/{grant.token}')
            process = await asyncio.create_subprocess_exec(self.ffmpeg,'-v','error','-nostdin',
                '-ss',str(offset),'-re','-i',str(self.asset),'-map','0:v:0','-map','0:a:0',
                '-c','copy','-f','rtsp','-rtsp_transport','tcp',url,
                stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,env=self.env)
            self.processes[owner] = process
            until = time.monotonic()+5
            while time.monotonic()<until:
                if process.returncode is not None: raise TestDenied('test publisher stopped')
                try:
                    paths = await asyncio.to_thread(self.service.api,'paths/list?itemsPerPage=10000')
                    if any(p['name']=='test/'+grant.token and p['ready'] for p in paths['items']):
                        return grant
                except OSError:
                    pass
                await asyncio.sleep(.05)
            raise TestDenied('test source startup timed out')
        except BaseException:
            await self.release(grant)
            raise

    async def remaining(self, grant):
        process = self.processes.get(grant.owner)
        if not process or process.returncode is not None: return 0
        return await asyncio.to_thread(self.quota.check,grant)

    async def release(self, grant):
        process = self.processes.pop(grant.owner,None)
        if process and process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(),2)
            except TimeoutError:
                process.kill(); await process.wait()
        await asyncio.to_thread(self.quota.release,grant)


def prepare(runtime, allow_pending=False):
    """Validate native prerequisites and record FFmpeg's local library paths."""
    import os
    import subprocess
    from .config import atomic_write, dump
    runtime = Path(runtime)
    directory = runtime/'test-video'; directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    binary = shutil.which('ffmpeg')
    environment = dict(os.environ)
    if not binary:
        binary = str(ROOT/'runtime/tools/ffmpeg-root/usr/bin/ffmpeg')
        library = ROOT/'runtime/tools/ffmpeg-root/usr/lib/x86_64-linux-gnu'
        environment['LD_LIBRARY_PATH'] = ':'.join(str(p) for p in (library,library/'pulseaudio',library/'samba'))
    if not Path(binary).is_file(): raise ValueError('测试频道需要 FFmpeg（仅复制码流）')
    # Store only required environment entries, never the shell's credentials.
    environment = {k:v for k,v in environment.items() if k in ('PATH','LD_LIBRARY_PATH','LANG')}
    if not (directory/'test.mp4').is_file():
        if not allow_pending:
            raise ValueError('请先运行 scripts/build-test-video.py 生成完整测试视频')
        atomic_write(directory/'config.json',dump(dict(ffmpeg=binary,environment=environment)))
        return False
    probe = str(Path(binary).with_name('ffprobe'))
    data = json.loads(subprocess.check_output([probe,'-v','error','-show_streams','-show_format','-of','json',
        str(directory/'test.mp4')],env=environment,timeout=5))
    videos = [s for s in data['streams'] if s['codec_type']=='video']
    audios = [s for s in data['streams'] if s['codec_type']=='audio']
    if (len(videos)!=1 or len(audios)!=1 or videos[0]['codec_name']!='h264'
            or (videos[0]['width'],videos[0]['height'])!=(2560,1440)
            or videos[0]['r_frame_rate']!='60/1' or videos[0]['has_b_frames']!=0
            or not 1580000<=int(videos[0]['bit_rate'])<=1620000
            or audios[0]['codec_name']!='aac' or abs(float(data['format']['duration'])-600)>.1):
        raise ValueError('测试视频必须为完整十分钟、2K/60 fps、1600 Kbps H264 无 B 帧和 AAC')
    atomic_write(directory/'config.json',dump(dict(ffmpeg=binary,environment=environment)))
    return True
