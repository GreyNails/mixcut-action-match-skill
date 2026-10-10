#!/usr/bin/env python3
"""Extract timestamped source frames for review; no generated or interpolated art.

Requires FFmpeg, FFprobe and Pillow. --crop uses x:y:width:height in source pixels;
metadata autorotation is disabled so these coordinates stay reproducible.
The requested source time is printed; FFmpeg returns the first decoded frame at
or after that seek position, which may differ by one source frame on VFR video.
"""

import argparse
import concurrent.futures
import io
import json
import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def extract(video, time, crop, width, height):
    filters = []
    if crop:
        x, y, crop_width, crop_height = crop
        filters.append(f"crop={crop_width}:{crop_height}:{x}:{y}:exact=1")
    filters.extend([f"scale={width}:{height}:force_original_aspect_ratio=decrease", "setsar=1",
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"])
    result = subprocess.run(["ffmpeg", "-v", "error", "-threads", "1", "-ss", f"{time:.9f}", "-noautorotate", "-i", str(video),
                             "-map", "0:v:0", "-an", "-frames:v", "1", "-vf", ",".join(filters),
                             "-f", "image2pipe", "-c:v", "png", "-threads", "1", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    if not result.stdout:
        raise ValueError(f"no decoded frame at {timestamp(time)}; choose an earlier --end")
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True, help="Exclusive end time in seconds")
    parser.add_argument("--step", type=float, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Contact sheet .png or .jpg path; JSON manifest saved beside it")
    parser.add_argument("--crop", help="x:y:width:height in original source pixels")
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--width", type=int, default=320, help="Thumbnail width; height uses 16:9 letterboxing")
    parser.add_argument("--max-frames", type=int, default=80, help="Refuse excessive requests; hard limit 240")
    args = parser.parse_args()
    if any(not math.isfinite(v) for v in (args.start, args.end, args.step)) or not 0 <= args.start < args.end or args.step <= 0:
        parser.error("use finite times with 0 <= start < end and step > 0")
    if not 1 <= args.columns <= 12 or not 80 <= args.width <= 640 or not 1 <= args.max_frames <= 240:
        parser.error("columns must be 1..12, width 80..640, and max-frames 1..240")
    if args.out.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        parser.error("--out must end in .png, .jpg or .jpeg")
    if not args.video.is_file():
        parser.error("--video must name an existing file")
    if any(shutil.which(tool) is None for tool in ("ffmpeg", "ffprobe")):
        parser.error("ffmpeg and ffprobe must be on PATH")
    requested_count = (args.end - args.start) / args.step
    if not math.isfinite(requested_count):
        parser.error("requested sampling interval is too large for this step")
    count = max(1, math.ceil(requested_count - 1e-10))
    if count > args.max_frames:
        parser.error(f"request contains {count} frames, exceeding --max-frames {args.max_frames}; narrow the interval or increase --step")
    times = [args.start + i * args.step for i in range(count)]
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height,duration:format=duration", "-of", "json", str(args.video)],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        info = json.loads(probe.stdout)
        stream = info["streams"][0]
    except (subprocess.CalledProcessError, KeyError, IndexError, json.JSONDecodeError) as error:
        parser.error(f"cannot probe a video stream: {error}")
    duration = stream.get("duration", info.get("format", {}).get("duration"))
    if duration is not None and duration != "N/A" and args.end > float(duration) + 1e-3:
        parser.error(f"--end exceeds source duration {float(duration):.3f}s")
    crop = None
    if args.crop:
        try:
            crop = tuple(map(int, args.crop.split(":")))
            x, y, width, height = crop
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > stream["width"] or y + height > stream["height"]:
                raise ValueError("crop must fit inside the original frame")
        except ValueError as error:
            parser.error(f"invalid --crop; expected x:y:width:height ({error})")
    width, height = args.width, round(args.width * 9 / 16)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            images = list(executor.map(lambda time: extract(args.video, time, crop, width, height), times))
    except (subprocess.CalledProcessError, ValueError, OSError) as error:
        detail = error.stderr.decode(errors="replace") if isinstance(error, subprocess.CalledProcessError) else str(error)
        parser.error(f"frame extraction failed: {detail}")
    columns = min(args.columns, len(images))
    rows = math.ceil(len(images) / columns)
    label_height, header_height = 28, 38
    sheet = Image.new("RGB", (columns * width, rows * (height + label_height) + header_height), "#121822")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", max(11, min(16, width // 20)))
    except OSError:
        font = ImageFont.load_default()
    # ASCII title remains portable even when the source filename uses unsupported glyphs.
    draw.text((8, 11), f"SOURCE FRAMES | seek timestamps | crop={args.crop or 'full'}", font=font, fill="white")
    for i, (time, image) in enumerate(zip(times, images)):
        x, y = i % columns * width, i // columns * (height + label_height) + header_height
        sheet.paste(image, (x, y))
        draw.text((x + 6, y + height + 6), f"{i + 1:03d}  {timestamp(time)}", font=font, fill="white")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out, **({"quality": 92} if args.out.suffix.lower() in (".jpg", ".jpeg") else {}))
    manifest = {"source_name": args.video.name, "source_width": stream["width"], "source_height": stream["height"],
                "crop_xywh": crop, "columns": columns, "frames": len(images),
                "timestamp_scope": "Requested source seek time; frame at or immediately after it. No frame interpolation; metadata autorotation disabled.",
                "seek_times": [round(time, 9) for time in times]}
    args.out.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"frames": len(images), "sheet": str(args.out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
