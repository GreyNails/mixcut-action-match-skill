#!/usr/bin/env python3
"""Reproducible, frame-based action-match AMV pipeline; run --help for commands."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

SR = 48000


def run(args, **kwargs):
    return subprocess.run([str(v) for v in args], check=True, **kwargs)


def probe(path, count=False):
    cmd = ['ffprobe', '-v', 'error']
    if count:
        cmd += ['-count_frames']
    cmd += ['-show_streams', '-show_format', '-of', 'json', str(path)]
    return json.loads(run(cmd, capture_output=True, text=True).stdout)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def resolve(root, name):
    path = Path(name).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def load(project):
    root = Path(project).expanduser().resolve()
    cfg = json.loads((root / 'project.json').read_text(encoding='utf-8'))
    timeline = json.loads(resolve(root, cfg.get('timeline', 'timeline.json')).read_text(encoding='utf-8'))
    sources = json.loads(resolve(root, cfg.get('sources', 'sources.json')).read_text(encoding='utf-8'))
    for key, value in sources.items():
        if not isinstance(value, dict):
            sources[key] = {'path': value}
        value = sources[key]
        if value.get('path'):
            value['path'] = str(resolve(root, value['path']))
    cfg['fps'] = int(cfg.get('fps', timeline.get('fps', 30)))
    cfg['width'] = int(cfg.get('width', timeline.get('width', 1920)))
    cfg['height'] = int(cfg.get('height', timeline.get('height', 1080)))
    cfg['active_height'] = int(cfg.get('active_height', timeline.get('active_height', cfg['height'])))
    cfg['music'] = str(resolve(root, cfg['music']))
    cfg['output'] = str(resolve(root, cfg.get('output', 'output/amv.mp4')))
    for directory in ['clips', 'audio', 'logs', 'reports']:
        (root / directory).mkdir(exist_ok=True)
    return root, cfg, timeline, sources


def file_stat(path):
    info = Path(path).stat()
    return [info.st_size, info.st_mtime_ns]


def source_files(shots, sources, sfx_only=False):
    keys = {x['source'] for x in shots if not sfx_only or x.get('sfx')}
    return {key: {'path': sources[key]['path'], 'stat': file_stat(sources[key]['path'])} for key in sorted(keys)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def span(x):
    return float(x['source_out']) - float(x['source_in'])


def duration(x, fps):
    return x['frames'] / fps


def source_origin(x):
    # A documented first decoded PTS can differ from the requested seek by part
    # of one native frame; resetting video PTS makes that frame output time zero.
    return float(x.get('first_decoded_source_pts', x['source_in']))


def mapped(x, source_offset, fps):
    source_offset -= source_origin(x) - float(x['source_in'])
    if x.get('warp'):
        a, at, v1, v2 = x['warp']
        return source_offset / v1 if source_offset < a else at + (source_offset - a) / v2
    return source_offset / (span(x) / duration(x, fps))


def check_project(root, cfg, timeline, sources, metadata_only=False):
    """Raise on structural/range/retime errors. Observations are not perceptual approval."""
    errors, warnings = [], []
    fps = cfg['fps']
    max_shift_frames = float(cfg.get('max_sfx_shift_frames', 2))
    if not math.isfinite(max_shift_frames) or max_shift_frames < 0:
        errors.append('max_sfx_shift_frames must be nonnegative and finite')
        max_shift_frames = 0
    shots = timeline.get('shots', [])
    if not shots or fps <= 0:
        raise ValueError('Timeline must contain shots and have a positive integer fps.')
    if float(timeline.get('fps', fps)) != fps:
        errors.append('project fps differs from timeline fps')
    if not 0 < cfg['active_height'] <= cfg['height']:
        errors.append('active_height must fit the output height')
    if any(cfg[k] % 2 for k in ('width', 'height', 'active_height')):
        errors.append('H.264 4:2:0 output dimensions must be even')
    limits, media = {}, {}
    used = {x.get('source') for x in shots}
    for key in sorted(used, key=str):
        item = sources.get(key)
        if item is None:
            errors.append(f'unknown source: {key}')
            continue
        limits[key] = float(item.get('duration', 0))
        if not metadata_only:
            path = Path(item.get('path', ''))
            if not path.is_file():
                errors.append(f'source does not exist: {key} ({path})')
                continue
            info = probe(path)
            media[key] = info
            videos = [s for s in info['streams'] if s['codec_type'] == 'video']
            if not videos:
                errors.append(f'no video stream: {key}')
            actual = float(info['format']['duration'])
            if limits[key] and abs(limits[key] - actual) > .25:
                warnings.append(f'{key}: metadata duration differs from file; checking actual duration')
            limits[key] = actual
    cursor, indices = 0, set()
    for x in shots:
        tag = f'shot {x.get("index", "?")}'
        try:
            n = x['frames']
            if not isinstance(n, int) or n <= 0:
                errors.append(f'{tag}: frames must be a positive integer')
                continue
            start, end = x['start_frame'], x['end_frame']
            if not isinstance(start, int) or not isinstance(end, int) or start != cursor or end - start != n:
                errors.append(f'{tag}: timeline gap/overlap or inconsistent frame count')
            cursor = end
            if not isinstance(x['index'], int) or x['index'] < 0 or x['index'] in indices:
                errors.append(f'{tag}: index must be a unique nonnegative integer')
            indices.add(x['index'])
            d, length = n / fps, span(x)
            source_in, source_out = float(x['source_in']), float(x['source_out'])
            if not all(math.isfinite(v) for v in [d, source_in, source_out]) or source_in < 0 or length <= 0:
                errors.append(f'{tag}: invalid source interval')
                continue
            limit = limits.get(x['source'], 0)
            if not source_in <= source_origin(x) < source_out:
                errors.append(f'{tag}: first_decoded_source_pts is outside the source interval')
            if limit <= 0 or source_out > limit + .001:
                errors.append(f'{tag}: source_out {source_out} exceeds known duration {limit}')
            for name, actual in [('duration', d), ('start', start / fps), ('source_span', length), ('speed', length / d)]:
                if name in x and abs(float(x[name]) - actual) > .002:
                    errors.append(f'{tag}: stale {name}; expected {actual:.9f}')
            if 'crop_box' in x:
                cx, cy, cw, ch = x['crop_box']
                if min(cx, cy) < 0 or min(cw, ch) <= 0 or cx + cw > 1920 or cy + ch > 1080:
                    errors.append(f'{tag}: crop_box must fit normalized 1920x1080 space')
            if x.get('warp'):
                a, at, v1, v2 = map(float, x['warp'])
                if not all(math.isfinite(v) for v in [a, at, v1, v2]) or not (0 < a < length and 0 < at < d and v1 > 0 and v2 > 0):
                    errors.append(f'{tag}: invalid two-segment warp')
                    continue
                if abs(a / v1 - at) > .002 or abs(at + (source_out - source_origin(x) - a) / v2 - d) > .002:
                    errors.append(f'{tag}: warp does not cover the exact shot duration')
            if x.get('hit') is not None:
                hit = float(x['hit'])
                if not source_in <= hit <= source_out:
                    errors.append(f'{tag}: hit is outside source interval')
                anchor = mapped(x, hit - source_in, fps)
                for key, expected in [('anchor_offset', anchor), ('anchor_timeline', start / fps + anchor)]:
                    if key in x and abs(float(x[key]) - expected) > .002:
                        errors.append(f'{tag}: {key} disagrees with the video time map')
                if x.get('warp') and abs(float(x['warp'][0]) - (hit - source_origin(x))) > .002:
                    errors.append(f'{tag}: warp knot and hit must identify the same source instant')
            elif x.get('sfx'):
                errors.append(f'{tag}: sfx requires a source hit')
            shift = float(x.get('sfx_shift', 0))
            if not math.isfinite(shift) or abs(shift) > max_shift_frames / fps + 1e-6:
                errors.append(f'{tag}: sound correction is non-finite or exceeds max_sfx_shift_frames={max_shift_frames:g}')
            if x.get('sfx') and x['source'] in media and not any(s['codec_type'] == 'audio' for s in media[x['source']]['streams']):
                errors.append(f'{tag}: sfx requested but source has no audio')
            for key in ('fade_in', 'fade_out'):
                if key in x and not 0 <= float(x[key]) <= d:
                    errors.append(f'{tag}: invalid {key}')
            if not x.get('verified', False):
                warnings.append(f'{tag}: source visual review is not marked verified')
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            errors.append(f'{tag}: malformed data: {exc}')
    if cursor != timeline.get('frames'):
        errors.append('timeline frames differs from final end_frame')
    if abs(float(timeline.get('duration', cursor / fps)) - cursor / fps) > .002:
        errors.append('timeline duration differs from frames/fps')
    overlaps = []
    for i, a in enumerate(shots):
        for b in shots[i + 1:]:
            if a.get('source') == b.get('source') and min(a['source_out'], b['source_out']) > max(a['source_in'], b['source_in']) + .001:
                overlaps.append([a['index'], b['index']])
    if overlaps and not cfg.get('allow_source_overlap', False):
        errors.append(f'source intervals overlap: {overlaps}; explicitly allow_source_overlap only for intentional reuse')
    if not metadata_only:
        music = Path(cfg['music'])
        if not music.is_file():
            errors.append(f'music does not exist: {music}')
        else:
            mi = probe(music)
            if not any(s['codec_type'] == 'audio' for s in mi['streams']):
                errors.append('music file has no audio stream')
            if float(cfg.get('music_offset', 0)) < 0:
                errors.append('music_offset must be nonnegative')
            if float(cfg.get('music_offset', 0)) + cursor / fps > float(mi['format']['duration']) + .05:
                errors.append('music is shorter than the requested timeline; trim the timeline or use a longer music file')
    report = {'ok': not errors, 'errors': errors, 'warnings': warnings, 'shots': len(shots), 'source_files': len(used), 'frames': cursor, 'fps': fps, 'source_overlaps': overlaps, 'media_probed': not metadata_only}
    dump(root / 'reports/check.json', report)
    if errors:
        raise ValueError('\n'.join(errors))
    return report


def prepare(args):
    root, recipe = Path(args.project).expanduser().resolve(), Path(args.recipe).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for filename in ('project.json', 'sources.json', 'timeline.json'):
        if (root / filename).exists():
            raise ValueError(f'{root / filename} exists; use a fresh project directory to preserve earlier work')
    t = json.loads((recipe / 'timeline.json').read_text(encoding='utf-8'))
    s = json.loads((recipe / 'sources.json').read_text(encoding='utf-8'))
    media_root = Path(args.media_root).expanduser().resolve()
    paths = {}
    for p in media_root.rglob('*'):
        if p.is_file():
            paths.setdefault(p.name, []).append(p)
    used = {x['source'] for x in t['shots']}
    bound = {}
    for key in sorted(used):
        item = s[key] if isinstance(s[key], dict) else {'filename': s[key]}
        filename = item.get('filename') or Path(item.get('path', '')).name
        matches = paths.get(filename, [])
        if len(matches) != 1:
            raise ValueError(f'{key}: expected one file named {filename!r}, found {len(matches)}; rename duplicates or bind sources.json manually')
        bound[key] = {**item, 'path': str(matches[0])}
    music = Path(args.music).expanduser().resolve()
    if not music.is_file():
        raise ValueError(f'music does not exist: {music}')
    cfg = {'timeline': 'timeline.json', 'sources': 'sources.json', 'music': str(music), 'output': 'output/amv.mp4', 'fps': t.get('fps', 30), 'width': t.get('width', 1920), 'height': t.get('height', 1080), 'active_height': t.get('active_height', 900), 'music_gain': .70, 'music_offset': 0}
    t['music'] = str(music)
    dump(root / 'project.json', cfg)
    dump(root / 'sources.json', bound)
    dump(root / 'timeline.json', t)
    print(json.dumps(check_project(*load(root)), ensure_ascii=False, indent=2))


def video_frames(info):
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    value = video.get('nb_read_frames') or video.get('nb_frames')
    return int(value) if value and value != 'N/A' else None


def select_encoder(requested, root):
    if requested == 'cpu':
        return 'cpu'
    command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=320x180:r=30:d=0.1', '-frames:v', '1', '-c:v', 'h264_nvenc', '-f', 'null', '-']
    result = subprocess.run(command, capture_output=True, text=True)
    (root / 'logs/nvenc_probe.log').write_text(result.stderr, encoding='utf-8')
    if result.returncode == 0:
        return 'nvenc'
    if requested == 'nvenc':
        raise ValueError('NVENC encoding test failed; see logs/nvenc_probe.log or select --encoder cpu')
    print('NVENC unavailable; using libx264.', flush=True)
    return 'cpu'


def filters(x, cfg, extra_frame=False):
    fps, w, h, ah = cfg['fps'], cfg['width'], cfg['height'], cfg['active_height']
    d = duration(x, fps)
    vf = [f'trim=duration={span(x):.10f}', 'setpts=PTS-STARTPTS', 'scale=1920:1080:flags=lanczos']
    if 'crop_box' in x:
        cx, cy, cw, ch = x['crop_box']
        vf.append(f'crop={cw}:{ch}:{cx}:{cy}')
    elif 'crop_y' in x:
        vf.append(f'crop=1920:900:0:{x["crop_y"]}')
    vf.append(f'scale={w}:{ah}:flags=lanczos')
    if x.get('flip'):
        vf.append('hflip')
    if x.get('warp'):
        a, at, v1, v2 = x['warp']
        vf.append(f"setpts='if(lt(PTS*TB,{a:.10f}),PTS/{v1:.10f},({at:.10f}+(PTS*TB-{a:.10f})/{v2:.10f})/TB)'")
    else:
        vf.append(f'setpts=PTS/{span(x)/d:.10f}')
    vf.append(f'fps={fps}:round=near:eof_action=pass')
    # At most one output frame may be duplicated to close a sampling-rounding gap.
    # Longer deficits fail; no long cloned tail is used to disguise missing action.
    if extra_frame:
        vf.append('tpad=stop_mode=clone:stop=1')
    vf += [f'trim=end_frame={x["frames"]}', 'setsar=1']
    at = float(x.get('anchor_offset', d * .45))
    if x.get('fx') == 'impact':
        vf.append(f"rgbashift=rh=3:bh=-3:enable='between(t,{max(0,at-1/fps):.8f},{min(d,at+2/fps):.8f})'")
        vf.append(f"eq=brightness='0.026*max(0,1-abs(t-{at:.8f})/0.085)':eval=frame")
    for key in ('fade_in', 'fade_out'):
        if x.get(key):
            st = 0 if key == 'fade_in' else d - x[key]
            vf.append(f'fade=t={"in" if key == "fade_in" else "out"}:st={st:.8f}:d={x[key]}')
    vf += [f'pad={w}:{h}:0:{(h-ah)//2}:black', 'format=yuv420p']
    return ','.join(vf)


def render(args):
    root, cfg, t, sources = load(args.project)
    check_project(root, cfg, t, sources)
    encoder = select_encoder(args.encoder, root)
    selected = t['shots']
    if args.shots:
        indices = set()
        for part in args.shots.split(','):
            lo, sep, hi = part.partition('-')
            indices.update(range(int(lo), int(hi) + 1) if sep else [int(lo)])
        missing = indices - {x['index'] for x in selected}
        if missing:
            raise ValueError(f'unknown shot indices: {sorted(missing)}')
        selected = [x for x in selected if x['index'] in indices]
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def one(x):
        dest = root / 'clips' / f'{x["index"]:04d}.mp4'
        record = dest.with_suffix('.json')
        src = Path(sources[x['source']]['path'])
        signature = hashlib.sha256(json.dumps([x, cfg, encoder, script_hash, str(src), src.stat().st_size, src.stat().st_mtime_ns], sort_keys=True).encode()).hexdigest()
        if dest.exists() and record.exists() and not args.force:
            prior = json.loads(record.read_text())
            if prior.get('signature') == signature and video_frames(probe(dest)) == x['frames']:
                return {'index': x['index'], 'status': 'cached', 'rounding_tail_frames': prior.get('rounding_tail_frames', 0)}
        tmp = dest.with_suffix('.part.mp4')
        opts = ['-c:v', 'h264_nvenc', '-preset', 'p6', '-rc', 'vbr', '-cq', '17', '-b:v', '0'] if encoder == 'nvenc' else ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-threads', '2']
        count, added = 0, 0
        for repair in [False, True]:
            cmd = ['ffmpeg', '-y', '-v', 'error', '-threads', '2', '-filter_threads', '1', '-ss', f'{source_origin(x):.10f}', '-t', f'{x["source_out"]-source_origin(x):.10f}', '-i', src, '-map', '0:v:0', '-an', '-vf', filters(x, cfg, repair), '-frames:v', str(x['frames']), *opts, '-pix_fmt', 'yuv420p', '-g', str(cfg['fps']*2), '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-video_track_timescale', str(cfg['fps']*512), '-movflags', '+faststart', tmp]
            with (root / 'logs' / f'render_{x["index"]:04d}.log').open('w') as log:
                run(cmd, stderr=log)
            count = video_frames(probe(tmp, count=True))
            if count == x['frames']:
                added = int(repair)
                break
            if repair or count != x['frames'] - 1:
                raise ValueError(f'shot {x["index"]}: decoded {count}, expected {x["frames"]} frames. Revise source range/time map; source is not extended beyond its out-point.')
        os.replace(tmp, dest)
        report = {'index': x['index'], 'signature': signature, 'encoder': encoder, 'frames': count, 'rounding_tail_frames': added, 'status': 'rendered', 'definition_digest': digest([x, cfg]), 'source_stat': [src.stat().st_size, src.stat().st_mtime_ns]}
        dump(record, report)
        return report

    reports = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for report in pool.map(one, selected):
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    dump(root / 'reports/render.json', {'encoder': encoder, 'shots': reports})


def tempo(value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError('tempo must be positive and finite')
    parts = []
    while value < .5:
        parts.append('atempo=0.5')
        value /= .5
    while value > 2:
        parts.append('atempo=2')
        value /= 2
    # Decimal source in/out subtraction may leave a nominal 1x rate within
    # the ten-decimal filter precision. Avoid atempo's analysis latency at 1x.
    if round(value, 10) != 1.0:
        parts.append(f'atempo={value:.10f}')
    return ','.join(parts) if parts else 'anull'


def mix(args):
    import numpy as np
    import scipy.signal as sig
    import scipy.ndimage as ndi
    import soundfile as sf
    root, cfg, t, sources = load(args.project)
    check_project(root, cfg, t, sources)
    fps, total = cfg['fps'], round(t['frames'] / cfg['fps'] * SR)

    def decode(cmd):
        raw = run(cmd + ['-f', 'f32le', '-ac', '2', '-ar', str(SR), '-'], capture_output=True).stdout
        return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()

    def cue(x):
        d, at = duration(x, fps), mapped(x, x['hit'] - x['source_in'], fps)
        n = round(d * SR)
        cmd = ['ffmpeg', '-v', 'error', '-threads', '1', '-ss', str(source_origin(x)), '-t', str(x['source_out']-source_origin(x)), '-i', sources[x['source']]['path'], '-vn']
        if x.get('warp'):
            a, at, v1, v2 = x['warp']
            graph = f'[0:a:0]asplit=2[a][b];[a]atrim=end={a},asetpts=PTS-STARTPTS,{tempo(v1)},apad,atrim=duration={at}[c];[b]atrim=start={a},asetpts=PTS-STARTPTS,{tempo(v2)},apad,atrim=duration={d-at}[e];[c][e]concat=n=2:v=0:a=1[out]'
            cmd += ['-filter_complex', graph, '-map', '[out]']
        else:
            cmd += ['-map', '0:a:0', '-af', f'{tempo(span(x)/d)},apad,atrim=duration={d}']
        raw = decode(cmd)
        raw = np.pad(raw, ((0, max(0, n-len(raw))), (0, 0)))[:n]
        shift = round(float(x.get('sfx_shift', 0)) * SR)
        if shift > 0:
            raw = np.pad(raw, ((shift, 0), (0, 0)))[:n]
        elif shift < 0:
            raw = np.pad(raw[-shift:], ((0, -shift), (0, 0)))[:n]
        out = raw.copy()
        # Masking reduces sustained music/dialogue; it is not source separation.
        if not args.raw_sfx:
            _, _, transform = sig.stft(raw.mean(axis=1), fs=SR, nperseg=1024, noverlap=768)
            magnitude = abs(transform)
            harmonic = ndi.median_filter(magnitude, size=(1, 25))
            percussive = ndi.median_filter(magnitude, size=(25, 1))
            mask = percussive**2 / (percussive**2 + (3*harmonic)**2 + 1e-15)
            for channel in range(2):
                _, _, matrix = sig.stft(raw[:, channel], fs=SR, nperseg=1024, noverlap=768)
                _, channel_audio = sig.istft(matrix*mask, fs=SR, nperseg=1024, noverlap=768)
                out[:, channel] = channel_audio[:n]
            out = .86*out + .14*raw
        out = sig.sosfilt(sig.butter(3, [100, 11000], btype='bandpass', fs=SR, output='sos'), out, axis=0)
        tt = np.arange(n)/SR - at
        envelope = np.zeros(n)
        rise = (tt >= -.20) & (tt < -.035)
        envelope[rise] = np.sin((tt[rise]+.20)/.165*np.pi/2)**2
        envelope[(tt >= -.035) & (tt <= .065)] = 1
        tail = (tt > .065) & (tt < .46)
        envelope[tail] = np.cos((tt[tail]-.065)/.395*np.pi/2)**2
        edge = np.minimum(np.arange(n)/(.012*SR), (n-1-np.arange(n))/(.025*SR))
        out *= (envelope*np.clip(edge, 0, 1))[:, None]
        peak, rms = float(np.max(abs(out))), float(np.sqrt(np.mean(raw**2)))
        target = float(x.get('sfx_peak', .20))
        gain = min(5, target/max(peak, 1e-6)) if rms > .002 else 0
        out *= gain
        return x, out, {'index': x['index'], 'gain': gain, 'input_rms': rms, 'sample_peak': float(np.max(abs(out))), 'sfx_shift_seconds': shift/SR, 'anchor_offset': at}

    music = decode(['ffmpeg', '-v', 'error', '-ss', str(cfg.get('music_offset', 0)), '-i', cfg['music'], '-t', str(total/SR), '-map', '0:a:0', '-vn'])
    if len(music) + round(.05*SR) < total:
        raise ValueError('decoded music is shorter than the timeline')
    music = np.pad(music, ((0, max(0, total-len(music))), (0, 0)))[:total]
    sfx, duck, reports = np.zeros((total, 2), np.float32), np.ones(total, np.float32), []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for x, audio, report in pool.map(cue, [x for x in t['shots'] if x.get('sfx')]):
            start = round(x['start_frame']/fps*SR)
            end = min(total, start+len(audio))
            sfx[start:end] += audio[:end-start]
            reports.append(report)
            anchor = start+round(report['anchor_offset']*SR)
            lo, hi = max(0, anchor-round(.04*SR)), min(total, anchor+round(.19*SR))
            relative = (np.arange(lo, hi)-anchor)/SR
            envelope = np.where(relative < 0, np.clip((relative+.04)/.04, 0, 1), np.exp(-relative/.065))
            duck[lo:hi] = np.minimum(duck[lo:hi], 1-.13*envelope)
            print(f'mixed cue {x["index"]}', flush=True)
    bed = music*float(cfg.get('music_gain', .70))*duck[:, None]
    mixed = bed+sfx
    gain = min(1, .96/max(float(abs(mixed).max()), 1e-9))
    for name, audio in [('music', bed), ('sfx', sfx), ('mix', mixed)]:
        sf.write(root / 'audio' / f'{name}.wav', audio*gain, SR, subtype='PCM_24')
    dump(root / 'reports/mix.json', {'definition_digest': digest([cfg, t]), 'music_stat': file_stat(cfg['music']), 'sfx_sources': source_files(t['shots'], sources, sfx_only=True), 'samples': total, 'sample_rate': SR, 'cues': reports, 'master_gain': gain, 'sample_peak': float(abs(mixed*gain).max()), 'limitations': 'Percussive masking is not isolated sound-effect extraction. Pitch-preserving atempo may smear transients. Listen and adjust only after measuring the resulting sound onset.'})


def assemble(args):
    root, cfg, t, sources = load(args.project)
    check_project(root, cfg, t, sources)
    files = []
    for x in t['shots']:
        clip = root / 'clips' / f'{x["index"]:04d}.mp4'
        if not clip.is_file() or video_frames(probe(clip)) != x['frames']:
            raise ValueError(f'missing or wrong-length clip: {clip}; run render')
        record_path = clip.with_suffix('.json')
        if not record_path.exists():
            raise ValueError(f'clip manifest missing: {record_path}; run render')
        record = json.loads(record_path.read_text(encoding='utf-8'))
        src_stat = Path(sources[x['source']]['path']).stat()
        if record.get('definition_digest') != digest([x, cfg]) or record.get('source_stat') != [src_stat.st_size, src_stat.st_mtime_ns]:
            raise ValueError(f'stale clip: {clip}; run render after timeline or source changes')
        files.append(f"file 'clips/{x['index']:04d}.mp4'\n")
    (root / 'concat.txt').write_text(''.join(files), encoding='utf-8')
    mixed = root / 'audio/mix.wav'
    if not mixed.is_file():
        raise ValueError('audio/mix.wav is missing; run mix')
    mix_report = root / 'reports/mix.json'
    if not mix_report.exists():
        raise ValueError('mixed-audio manifest is missing; run mix')
    mixed_manifest = json.loads(mix_report.read_text(encoding='utf-8'))
    if mixed_manifest.get('definition_digest') != digest([cfg, t]) or mixed_manifest.get('music_stat') != file_stat(cfg['music']) or mixed_manifest.get('sfx_sources') != source_files(t['shots'], sources, sfx_only=True):
        raise ValueError('mixed audio is stale; run mix after timeline, music, or source audio changes')
    output = Path(cfg['output'])
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.part.mp4')
    run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', root/'concat.txt', '-i', mixed, '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k', '-ar', str(SR), '-t', str(t['frames']/cfg['fps']), '-movflags', '+faststart', temporary])
    os.replace(temporary, output)
    dump(root / 'reports/final.json', {'definition_digest': digest([cfg, t]), 'mix_path': str(mixed), 'mix_stat': file_stat(mixed), 'output_path': str(output), 'output_stat': file_stat(output), 'sources': source_files(t['shots'], sources), 'music_stat': file_stat(cfg['music'])})
    with (root / 'shot_list.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['index', 'start_frame', 'end_frame', 'source', 'source_in', 'source_out', 'hit', 'note'])
        for x in t['shots']:
            writer.writerow([x.get(k, '') for k in ['index', 'start_frame', 'end_frame', 'source', 'source_in', 'source_out', 'hit', 'note']])
    print(output)


def validate(args):
    import numpy as np
    import soundfile as sf
    root, cfg, t, sources = load(args.project)
    check_project(root, cfg, t, sources)
    output = Path(cfg['output'])
    mixed = root / 'audio/mix.wav'
    manifest_path = root / 'reports/final.json'
    errors = []
    if not manifest_path.is_file():
        errors.append('final manifest is missing; run assemble')
    elif not output.is_file() or not mixed.is_file():
        errors.append('final output or mixed WAV is missing; run mix and assemble')
    else:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        expected = {'definition_digest': digest([cfg, t]), 'mix_path': str(mixed), 'mix_stat': file_stat(mixed), 'output_path': str(output), 'output_stat': file_stat(output), 'sources': source_files(t['shots'], sources), 'music_stat': file_stat(cfg['music'])}
        if manifest != expected:
            errors.append('final manifest is stale; rebuild changed inputs and run assemble')
    if errors:
        dump(root / 'reports/validation.json', {'ok': False, 'errors': errors, 'full_decode_exit': None})
        raise ValueError('; '.join(errors))
    info = probe(output, count=True)
    video = next((s for s in info['streams'] if s['codec_type'] == 'video'), None)
    audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    if video is None:
        errors.append('no video stream')
    else:
        if video_frames(info) != t['frames']:
            errors.append('final frame count differs from timeline')
        if Fraction(video['avg_frame_rate']) != cfg['fps']:
            errors.append('final frame rate differs from timeline')
        if (video['width'], video['height']) != (cfg['width'], cfg['height']):
            errors.append('final dimensions differ from project')
    if audio is None:
        errors.append('no audio stream')
    elif int(audio.get('sample_rate', 0)) != SR or int(audio.get('channels', 0)) != 2:
        errors.append('final audio is not 48 kHz stereo')
    command = ['ffmpeg', '-hide_banner', '-v', 'info', '-xerror', '-err_detect', 'explode', '-threads', '2', '-i', output, '-map', '0:v:0', '-map', '0:a:0', '-vf', f'crop={cfg["width"]}:{cfg["active_height"]}:0:{(cfg["height"]-cfg["active_height"])//2},blackdetect=d=0.15:pix_th=0.025:pic_th=0.98', '-af', 'ebur128=peak=true', '-f', 'null', '-']
    result = subprocess.run([str(s) for s in command], capture_output=True, text=True)
    (root / 'logs/full_decode.log').write_text(result.stderr, encoding='utf-8')
    if result.returncode:
        errors.append(f'full decode failed: {result.returncode}')
    stats = {}
    if audio is not None:
        raw = run(['ffmpeg', '-v', 'error', '-i', output, '-vn', '-f', 'f32le', '-ac', '2', '-ar', str(SR), '-'], capture_output=True).stdout
        decoded = np.frombuffer(raw, np.float32).reshape(-1, 2)
        stats['decoded_audio_peak'] = float(abs(decoded).max())
        stats['decoded_audio_samples'] = len(decoded)
        mixed = root/'audio/mix.wav'
        if mixed.exists():
            original, sr = sf.read(mixed, dtype='float32', always_2d=True)
            n = min(len(original), len(decoded))
            if sr != SR or abs(len(original)-t['frames']/cfg['fps']*SR) > 1:
                errors.append('mixed WAV has wrong rate or duration')
            if abs(len(decoded)-len(original)) > 2048:
                errors.append('decoded audio duration differs by more than two AAC frames')
            reference, actual = original[:n], decoded[:n]
            rms = float(np.sqrt(np.mean((reference-actual)**2)))
            if np.std(reference) > 1e-8 and np.std(actual) > 1e-8:
                correlation = float(np.corrcoef(reference[::10].ravel(), actual[::10].ravel())[0, 1])
                if not math.isfinite(correlation) or correlation < .98:
                    errors.append('zero-lag audio correlation below 0.98; investigate synchronization')
            else:
                correlation = None
                if rms > .0001:
                    errors.append('audio differs from silent/constant reference')
            stats.update(audio_correlation_zero_lag=correlation, audio_codec_rmse=rms)
    report = {'ok': not errors, 'errors': errors, 'frames': video_frames(info) if video else 0, 'full_decode_exit': result.returncode, 'black_intervals': [line.strip() for line in result.stderr.splitlines() if 'black_start:' in line], **stats, 'review_limit': 'Technical validation cannot establish action continuity, beat perception, or sound quality. Watch all transitions and listen to the complete mix separately.'}
    dump(root / 'reports/validation.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise ValueError('final validation failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['prepare', 'check', 'render', 'mix', 'assemble', 'validate']:
        sub = commands.add_parser(name)
        sub.add_argument('--project', required=True, help='Directory containing project.json (created by prepare)')
        if name == 'prepare':
            sub.add_argument('--recipe', required=True, help='Directory with sources.json and timeline.json')
            sub.add_argument('--media-root', required=True)
            sub.add_argument('--music', required=True)
        if name == 'check':
            sub.add_argument('--metadata-only', action='store_true', help='Skip media existence/probe checks; not final validation')
        if name in ['render', 'mix']:
            sub.add_argument('--jobs', type=int, default=2)
        if name == 'render':
            sub.add_argument('--shots', help='Comma-separated indices or ranges, e.g. 0,2,5-8')
            sub.add_argument('--encoder', choices=['auto', 'cpu', 'nvenc'], default='auto')
            sub.add_argument('--force', action='store_true')
        if name == 'mix':
            sub.add_argument('--raw-sfx', action='store_true', help='Skip percussive masking; envelope and bandpass remain')
    args = parser.parse_args()
    if hasattr(args, 'jobs') and args.jobs < 1:
        parser.error('--jobs must be positive')
    for binary in ['ffmpeg', 'ffprobe']:
        if not shutil.which(binary):
            parser.error(f'{binary} is not on PATH')
    try:
        if args.command == 'check':
            print(json.dumps(check_project(*load(args.project), metadata_only=args.metadata_only), ensure_ascii=False, indent=2))
        else:
            globals()[args.command](args)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, ImportError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
