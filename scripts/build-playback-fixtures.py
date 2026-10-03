#!/usr/bin/env python3
"""Pre-encode labelled fixtures for an authenticated playback comparison."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess


def matrix():
    base = dict(codec="h264", profile="high", width=2560, height=1440, fps=60,
                video_kbps=12000, keyframe_seconds=2, slices=1, bframes=0,
                audio=True, audio_kbps=128, audio_channels=2)
    variants = [
        ("01", "reference 60fps high bitrate 2s", {}),
        ("02", "30fps comparison", dict(fps=30)),
        ("03", "low bitrate comparison", dict(video_kbps=6000)),
        ("04", "automatic keyframes comparison", dict(keyframe_seconds=0)),
    ]
    return [dict(base, **changes, id="T" + number, name="vrc-test-" + number, label=label)
            for number, label, changes in variants]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--font", default="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    parser.add_argument("--duration", type=int, default=12)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.duration < 8 or args.duration % 4 or not 1 <= args.workers <= 4:
        parser.error("duration must be >= 8 and divisible by 4; workers must be 1..4")
    args.output.mkdir(parents=True, exist_ok=True)
    fixtures = matrix()

    def encode(item):
        suffix = ".mp4"
        target = args.output / (item["name"] + suffix)
        size = f'{item["width"]}x{item["height"]}'
        cmd = [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "lavfi", "-i", f'testsrc2=size={size}:rate={item["fps"]}']
        if item["audio"]:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0", "-an"]
        font_size = 42 if item["height"] == 1440 else 24
        title = f'{item["id"]} {item["codec"].upper()} {item["profile"].upper()} {size} {item["fps"]}FPS {item["video_kbps"]}KBPS'
        keyframe_label = str(item["keyframe_seconds"]) + "s" if item["keyframe_seconds"] else "AUTO"
        detail = f'KEY {keyframe_label}   SLICES 1   B 0   AAC STEREO'
        filters = [
            f"drawtext=fontfile='{args.font}':text='{title}':x=24:y=24:fontsize={font_size}:fontcolor=white:box=1:boxcolor=black@0.85",
            f"drawtext=fontfile='{args.font}':text='{detail}':x=24:y={font_size+40}:fontsize={font_size}:fontcolor=white:box=1:boxcolor=black@0.85",
            f"drawtext=fontfile='{args.font}':text='FRAME %{{n}}':x=24:y=h-{font_size*5}:fontsize={font_size*2}:fontcolor=white:box=1:boxcolor=black@0.85",
            f"drawtext=fontfile='{args.font}':text='CLIP %{{pts\\:hms}}':x=24:y=h-{font_size*2+24}:fontsize={font_size}:fontcolor=white:box=1:boxcolor=black@0.85",
        ]
        rate = str(item["video_kbps"]) + "k"
        gop = item["fps"] * item["keyframe_seconds"]
        cmd += ["-vf", ",".join(filters), "-pix_fmt", "yuv420p", "-b:v", rate,
                "-maxrate", rate, "-bufsize", rate, "-threads", "2"]
        if gop:
            cmd += ["-g", str(gop)]
        params = []
        if gop:
            params += [f"keyint={gop}", f"min-keyint={gop}", "scenecut=0"]
        params += [f'slices={item["slices"]}',
                      "sliced-threads=0", f'bframes={item["bframes"]}', "b-adapt=0", "b-pyramid=0",
                      "rc-lookahead=0", "sync-lookahead=0", "ref=1", "nal-hrd=none", "filler=1", "force-cfr=1",
                      "8x8dct=" + ("1" if item["profile"] == "high" else "0"),
                      "cabac=" + ("0" if item["profile"] == "baseline" else "1")]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                    "-profile:v", item["profile"], "-level:v", "3.1" if item["height"] == 720 else "5.1",
                    "-x264-params", ":".join(params)]
        if item["audio"]:
            cmd += ["-c:a", "aac", "-b:a", str(item["audio_kbps"]) + "k", "-ac", "2", "-ar", "48000"]
        cmd += ["-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-t", str(args.duration), "-avoid_negative_ts", "make_zero"]
        if suffix == ".mp4":
            cmd += ["-movflags", "+faststart"]
        cmd += [str(target)]
        subprocess.run(cmd, check=True, timeout=600)
        result = subprocess.run([args.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target)],
                                capture_output=True, text=True, check=True)
        probe = json.loads(result.stdout)
        video = next(x for x in probe["streams"] if x["codec_type"] == "video")
        assert video["codec_name"] == item["codec"]
        assert (video["width"], video["height"], video["pix_fmt"]) == (item["width"], item["height"], "yuv420p")
        audio = [x for x in probe["streams"] if x["codec_type"] == "audio"]
        assert len(audio) == int(item["audio"])
        if audio:
            assert audio[0]["codec_name"] == "aac" and audio[0]["channels"] == 2
        final = dict(item, file=target.name, sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                     duration_seconds=float(probe["format"]["duration"]), actual_profile=video.get("profile"),
                     file_bitrate_kbps=round(int(probe["format"]["bit_rate"])/1000, 1),
                     actual_bframes=video.get("has_b_frames"))
        print(json.dumps({"ready": item["id"], "file": target.name, "profile": final["actual_profile"],
                          "file_bitrate_kbps": final["file_bitrate_kbps"]}), flush=True)
        return final

    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        output = list(pool.map(encode, fixtures))
    (args.output / "manifest.json").write_text(json.dumps(dict(schema=1, loop_seconds=args.duration, fixtures=output), indent=2) + "\n")
    print(json.dumps({"fixtures": len(output), "manifest": str(args.output / "manifest.json")}), flush=True)


if __name__ == "__main__":
    main()
