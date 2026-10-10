#!/usr/bin/env python3
"""Inspect spectral attacks; every event is measured, never a generated beat grid.

Requirements: FFmpeg on PATH, numpy, scipy, Pillow. Outputs are review aids:
voice consonants, musical attacks and sound effects can resemble drum transients.
"""

import argparse
import csv
import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage, signal

SR, HOP, NFFT = 22050, 256, 1024
BANDS = {"low": (35, 190), "body": (190, 1000),
         "mid": (1000, 5000), "high": (5000, 10500)}
COLORS = {"low": "#ef9961", "mid": "#65c8e9", "high": "#ead572", "multi": "#d39cea"}


def robust_score(values):
    window = int(3 * SR / HOP) | 1
    local = ndimage.median_filter(values, size=window, mode="nearest")
    mad = ndimage.median_filter(abs(values - local), size=window, mode="nearest")
    positive = values[values > 0]
    floor = float(np.percentile(positive, 55)) * .35 if positive.size else 1e-8
    return np.clip((values - local) / (1.4826 * mad + floor + 1e-8), 0, 16)


def nearest_frame(time, fps):
    return int(math.floor(time * fps + .5))


def font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def overview(path, times, scores, events, duration, region_end):
    # At most twenty rows for a ten-minute track; no huge unbounded canvas.
    row_seconds = max(1, math.ceil(duration)) if duration < 30 else max(30, math.ceil(duration / 20 / 10) * 10)
    rows = max(1, math.ceil(duration / row_seconds))
    left, right, row_height, top = 75, 1510, 190, 75
    picture = Image.new("RGB", (1600, top + rows * row_height + 30), "#101622")
    draw = ImageDraw.Draw(picture)
    regular, small = font(18), font(13)
    draw.text((left, 16), "Spectral attack candidates - review before calling them drum hits", fill="white", font=regular)
    draw.text((left, 43), "low / mid / high = frequency evidence; vertical lines = selected measured events", fill="#a7b8ce", font=small)
    for row in range(rows):
        start, end = row * row_seconds, min(duration, (row + 1) * row_seconds)
        y = top + row * row_height
        draw.text((8, y + 5), f"{start:g}s", fill="white", font=regular)
        for second in range(start, int(end) + 1, max(1, row_seconds // 15)):
            x = left + (second - start) / row_seconds * (right - left)
            draw.line((x, y + 18, x, y + 157), fill="#263343")
            draw.text((x, y + 165), str(second), fill="#8fa2bb", font=small)
        use = (times >= start) & (times < end)
        indices = np.flatnonzero(use)
        indices = indices[::max(1, len(indices) // (right - left))]
        for band, offset in [("low", 0), ("mid", 1), ("high", 2)]:
            baseline = y + 65 + 43 * offset
            points = [(left + (float(times[i]) - start) / row_seconds * (right - left),
                       baseline - min(float(scores[band][i]), 10) * 3.4) for i in indices]
            if len(points) > 1:
                draw.line(points, fill=COLORS[band], width=1)
            draw.text((right + 9, baseline - 13), band, fill=COLORS[band], font=small)
        for event in events:
            if start <= event["time"] < end:
                x = left + (event["time"] - start) / row_seconds * (right - left)
                draw.line((x, y + 14, x, y + 154), fill=COLORS[event["band"]], width=1)
        if start <= region_end < end:
            x = left + (region_end - start) / row_seconds * (right - left)
            draw.line((x, y + 5, x, y + 158), fill="white", width=2)
            draw.text((max(left, min(x, right - 260)), y), "selection ends; later attacks excluded", fill="white", font=small)
    picture.save(path)


def analyze(audio, fps, region_end, min_spacing):
    frequencies, times, spectrum = signal.stft(
        audio, fs=SR, nperseg=NFFT, noverlap=NFFT - HOP,
        boundary="zeros", padded=True)
    magnitude = abs(spectrum).astype(np.float32)
    del spectrum
    harmonic = ndimage.median_filter(magnitude, size=(1, 25), mode="nearest")
    percussive = ndimage.median_filter(magnitude, size=(17, 1), mode="nearest")
    attack = magnitude * percussive ** 2 / (percussive ** 2 + harmonic ** 2 + 1e-12)
    del harmonic, percussive
    fluxes, scores, ratios = {}, {}, {}
    for band, (lo, hi) in BANDS.items():
        use = (frequencies >= lo) & (frequencies < hi)
        original, transient = magnitude[use], attack[use]
        log_spectrum = np.log1p(transient * 350)
        flux = np.mean(np.maximum(0, np.diff(log_spectrum, axis=1, prepend=log_spectrum[:, :1])), axis=0)
        fluxes[band] = ndimage.gaussian_filter1d(flux, .7)
        scores[band] = robust_score(fluxes[band])
        ratios[band] = np.sum(transient, axis=0) / (np.sum(original, axis=0) + 1e-9)
    combined = np.maximum(scores["low"] * .92, np.maximum(scores["mid"] * .76, scores["high"] * .69))
    combined += np.minimum(scores["low"], scores["mid"]) * .16
    energy = np.sqrt(np.mean(magnitude ** 2, axis=0))
    floor = max(float(np.max(energy)) * 1e-5, 1e-10)
    peaks, _ = signal.find_peaks(combined, height=1., prominence=.7, distance=max(1, round(.09 * SR / HOP)))
    candidates = []
    duration = len(audio) / SR
    for index in peaks:
        if times[index] >= duration or energy[index] <= floor:
            continue
        values = {band: float(scores[band][index]) for band in BANDS}
        lead = max(("low", "mid", "high"), key=lambda band: values[band])
        band = "multi" if values["low"] >= 2.6 and max(values["mid"], values["high"]) >= 2.8 else lead
        vocal_risk = lead == "mid" and values["body"] > max(values["low"], values["high"]) * 1.6 and values["high"] < 1.6 and values["low"] < 1.8
        support = sum(values[name] >= 2 for name in ("low", "mid", "high"))
        confidence = .26 + .045 * min(float(combined[index]), 10) + .17 * float(ratios[lead][index]) + .045 * (support - 1)
        confidence = float(np.clip(confidence - (.16 if vocal_risk else 0), .05, .98))
        # Walk back to the rising edge; an STFT peak itself can lag the attack.
        left = int(index)
        while left > max(0, index - 4) and fluxes[lead][left - 1] >= fluxes[lead][index] * .35:
            left -= 1
        onset = max(0., float(times[left]) - .004)
        frame = nearest_frame(onset, fps)
        eligible = confidence >= .48 and not vocal_risk and onset < region_end
        candidates.append({"time": round(onset, 6), "frame": frame, "frame_time": round(frame / fps, 6),
                           "peak_time": round(float(times[index]), 6), "band": band,
                           "strength": round(float(combined[index]), 3), "confidence": round(confidence, 3),
                           "vocal_risk": bool(vocal_risk), "eligible": bool(eligible),
                           "region": "music" if onset < region_end else "excluded_after_music_region",
                           "band_scores": {name: round(value, 3) for name, value in values.items()}})
    selected = []
    for event in sorted((e for e in candidates if e["eligible"]), key=lambda e: e["confidence"] + .014 * min(e["strength"], 12), reverse=True):
        if all(abs(event["time"] - other["time"]) >= min_spacing and event["frame"] != other["frame"] for other in selected):
            selected.append(dict(event))
    selected.sort(key=lambda e: e["time"])
    for number, event in enumerate(selected):
        event["event_id"] = number
    return candidates, selected, times, scores


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--music", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--music-region-end", type=float, help="Exclude later attacks from selected events (e.g. spoken coda)")
    parser.add_argument("--min-spacing", type=float, default=.145, help="Minimum spacing of selected candidates, seconds")
    parser.add_argument("--max-duration", type=float, default=600, help="Reject longer input instead of silently truncating it")
    args = parser.parse_args()
    for name in ("fps", "min_spacing", "max_duration"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    if args.fps > 240:
        parser.error("--fps must be at most 240")
    if args.music_region_end is not None and (not math.isfinite(args.music_region_end) or args.music_region_end <= 0):
        parser.error("--music-region-end must be finite and positive")
    if not args.music.is_file():
        parser.error("--music must name an existing file")
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg must be on PATH")
    try:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(args.music), "-t", str(args.max_duration + .1),
                              "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    except subprocess.CalledProcessError as error:
        parser.error(error.stderr.decode(errors="replace").strip())
    audio = np.frombuffer(raw, dtype="<f4").copy()
    duration = len(audio) / SR
    if duration > args.max_duration + 1 / SR:
        parser.error("input exceeds --max-duration; analyze a deliberate excerpt or raise the limit (memory grows with duration)")
    if len(audio) < NFFT or not np.isfinite(audio).all():
        parser.error("input is too short or has non-finite audio samples")
    region_end = min(duration, args.music_region_end if args.music_region_end is not None else duration)
    candidates, events, times, scores = analyze(audio, args.fps, region_end, args.min_spacing)
    metadata = {"source_name": args.music.name, "duration": duration, "fps": args.fps,
                "sample_rate": SR, "hop_samples": HOP, "music_region_end": region_end,
                "method": "Median spectral attack mask + band-limited positive spectral flux; no inferred metronome grid",
                "confidence_scope": "Heuristic spectral evidence, not calibrated probability or verified drum identity. Voice and effects can produce false positives; listen and review.",
                "timing_scope": "11.6 ms analysis hop; nearest output-frame quantization; acoustic onset estimates require spot checks.",
                "selected_count": len(events), "candidate_count": len(candidates)}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "transient_events.json").write_text(json.dumps({"metadata": metadata, "events": events, "candidates": candidates}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["event_id", "time", "frame", "frame_time", "band", "strength", "confidence", "vocal_risk"]
    with (args.out / "transient_events.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(events)
    overview(args.out / "rhythm_overview.png", times, scores, events, duration, region_end)
    print(json.dumps({"duration": duration, "candidates": len(candidates), "selected": len(events), "output": str(args.out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
