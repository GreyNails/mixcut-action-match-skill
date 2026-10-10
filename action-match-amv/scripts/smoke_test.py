#!/usr/bin/env python3
"""Generate self-contained synthetic media and exercise the real AMV pipeline.

No anime assets, music downloads, or user project changes are required.
Temporary media is deleted after success; --keep retains it for inspection.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def call(command, expected=0):
    completed = subprocess.run([str(v) for v in command], text=True, capture_output=True)
    if completed.returncode != expected:
        raise RuntimeError(f'Unexpected exit {completed.returncode}: {command}\n{completed.stdout}\n{completed.stderr}')
    print(completed.stdout, end='')
    return completed


def save(path, value):
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


def exercise(root):
    script = Path(__file__).with_name('amv.py')
    # At nominal 1x, tempo processing must preserve every sample and onset.
    spec = importlib.util.spec_from_file_location('amv_smoke_module', script)
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    if module.tempo(1.0) != 'anull' or module.tempo(.99999999999994) != 'anull':
        raise RuntimeError('Nominal 1x audio must use anull')
    for ratio in [.25, .5, 2, 4]:
        if 'atempo=1.0000000000' in module.tempo(ratio):
            raise RuntimeError('Tempo chain contains a redundant 1x stage')
    import numpy as np
    original_audio = np.zeros((24000, 2), dtype=np.float32)
    original_audio[4800:4864, 0] = np.hanning(64).astype(np.float32) * .7
    original_audio[12000:12064, 1] = np.hanning(64).astype(np.float32) * .5
    result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'f32le', '-ar', '48000', '-ac', '2', '-i', '-', '-af', module.tempo(1.0), '-f', 'f32le', '-ar', '48000', '-ac', '2', '-'], input=original_audio.tobytes(), capture_output=True, check=True)
    if result.stdout != original_audio.tobytes():
        raise RuntimeError('1x tempo changed samples or onset position')
    print('PASS: nominal 1x uses anull; short stereo transients are sample-identical.')
    media, recipe, project = root/'media', root/'recipe', root/'project'
    media.mkdir(); recipe.mkdir()
    for name, picture, frequency in [('one', 'testsrc2=s=320x180:r=30:d=3', 700), ('two', 'smptebars=s=320x180:r=30:d=3', 1000)]:
        call(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', picture, '-f', 'lavfi', '-i', f'sine=frequency={frequency}:sample_rate=48000:duration=3', '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', media/f'{name}.mp4'])
    call(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=48000:duration=3', '-c:a', 'pcm_s16le', root/'music.wav'])
    sources = {key: {'filename': f'{key}.mp4', 'duration': 3} for key in ['one', 'two']}
    timeline = {'fps': 30, 'width': 640, 'height': 360, 'active_height': 300, 'frames': 75, 'duration': 2.5, 'shots': [
        {'index': 0, 'source': 'one', 'source_in': .2, 'source_out': 1.2, 'source_span': 1., 'start_frame': 0, 'end_frame': 30, 'frames': 30, 'duration': 1., 'start': 0., 'speed': 1., 'hit': .6, 'anchor_offset': .4, 'anchor_timeline': .4, 'sfx': True, 'verified': True, 'note': 'Synthetic linear retime'},
        {'index': 1, 'source': 'two', 'source_in': 1., 'source_out': 2.5, 'source_span': 1.5, 'start_frame': 30, 'end_frame': 75, 'frames': 45, 'duration': 1.5, 'start': 1., 'speed': 1., 'hit': 1.6, 'anchor_offset': .4, 'anchor_timeline': 1.4, 'warp': [.6, .4, 1.5, .9/1.1], 'flip': True, 'sfx': True, 'verified': True, 'note': 'Synthetic piecewise retime'},
    ]}
    save(recipe/'sources.json', sources)
    save(recipe/'timeline.json', timeline)
    call([sys.executable, script, 'prepare', '--project', project, '--recipe', recipe, '--media-root', media, '--music', root/'music.wav'])
    timeline = json.loads((project/'timeline.json').read_text())
    call([sys.executable, script, 'render', '--project', project, '--encoder', 'cpu', '--jobs', '1'])
    call([sys.executable, script, 'render', '--project', project, '--encoder', 'cpu', '--jobs', '1'])
    call([sys.executable, script, 'mix', '--project', project, '--jobs', '1'])
    call([sys.executable, script, 'assemble', '--project', project])
    call([sys.executable, script, 'validate', '--project', project])
    def rejected(command, message):
        failure = call([sys.executable, script, command, '--project', project], expected=1)
        if message not in failure.stderr:
            raise RuntimeError(f'{command} failed for an unrelated reason: {failure.stderr}')

    # A structurally valid, same-length edit must not validate an earlier render.
    changed = json.loads(json.dumps(timeline))
    changed['shots'][0]['flip'] = True
    save(project/'timeline.json', changed)
    rejected('validate', 'final manifest is stale')
    save(project/'timeline.json', timeline)

    manifest = project/'reports/final.json'
    manifest_text = manifest.read_text()
    manifest.unlink()
    rejected('validate', 'final manifest is missing')
    manifest.write_text(manifest_text)

    # Touch each artifact independently and verify revision binding before decode.
    for artifact in [project/'audio/mix.wav', project/'output/amv.mp4']:
        before = artifact.stat()
        os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns+1_000_000_000))
        rejected('validate', 'final manifest is stale')
        os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns))

    # Re-render video after a source change, then ensure old audio is still refused.
    original = (media/'one.mp4').stat()
    os.utime(media/'one.mp4', ns=(original.st_atime_ns, original.st_mtime_ns+1_000_000_000))
    call([sys.executable, script, 'render', '--project', project, '--encoder', 'cpu', '--shots', '0', '--jobs', '1'])
    rejected('assemble', 'mixed audio is stale')
    os.utime(media/'one.mp4', ns=(original.st_atime_ns, original.st_mtime_ns))

    # This is a project preference, not a universal two-frame editorial rule.
    config_path = project/'project.json'
    config = json.loads(config_path.read_text())
    shifted = json.loads(json.dumps(timeline))
    shifted['shots'][0]['sfx_shift'] = .1
    save(project/'timeline.json', shifted)
    rejected('check', 'max_sfx_shift_frames=2')
    save(config_path, {**config, 'max_sfx_shift_frames': 4})
    call([sys.executable, script, 'check', '--project', project])
    for limit in [-1, float('nan'), float('inf')]:
        save(config_path, {**config, 'max_sfx_shift_frames': limit})
        rejected('check', 'max_sfx_shift_frames must be nonnegative and finite')
    save(config_path, config)
    save(project/'timeline.json', timeline)

    invalid = json.loads(json.dumps(timeline))
    invalid['shots'][0]['source_out'] = 99
    save(project/'timeline.json', invalid)
    failure = call([sys.executable, script, 'check', '--project', project], expected=1)
    if 'exceeds known duration' not in failure.stderr:
        raise RuntimeError('Invalid source range failed for an unrelated reason')
    save(project/'timeline.json', timeline)
    call([sys.executable, script, 'render', '--project', project, '--encoder', 'cpu', '--jobs', '1'])
    call([sys.executable, script, 'assemble', '--project', project])
    call([sys.executable, script, 'validate', '--project', project])
    print('PASS: complete 75-frame render/mix/decode; source bounds; stale source audio, final revisions, and missing manifest rejection; configurable finite sound-shift limits.')
    print(f'Project: {project}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args()
    if args.keep:
        exercise(Path(tempfile.mkdtemp(prefix='action-match-amv-smoke-')))
    else:
        with tempfile.TemporaryDirectory(prefix='action-match-amv-smoke-') as directory:
            exercise(Path(directory))
