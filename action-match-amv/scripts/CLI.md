# Rendering CLI

Requires Python 3.10+, `ffmpeg`/`ffprobe` on PATH. Audio mixing and final audio validation additionally use `numpy`, `scipy`, and `soundfile`. H.264 CPU encoding uses libx264. `--encoder auto` performs an actual NVENC encode test before choosing GPU or CPU; `--encoder nvenc` fails explicitly if that test fails.

Run from the package directory:

```bash
python scripts/amv.py prepare --project /path/to/new-project \
  --recipe examples/v2 --media-root /path/to/episodes \
  --music '/path/to/music.mp3'
python scripts/amv.py check --project /path/to/new-project
python scripts/amv.py render --project /path/to/new-project --shots 0,1,3-5 --encoder auto
# Watch these clips and correct the timeline before rendering the entire edit.
python scripts/amv.py render --project /path/to/new-project --encoder auto --jobs 2
python scripts/amv.py mix --project /path/to/new-project --jobs 2
python scripts/amv.py assemble --project /path/to/new-project
python scripts/amv.py validate --project /path/to/new-project
python scripts/smoke_test.py
```

`prepare` requires unique filename matches under the supplied media directory and refuses to overwrite an existing project. The exact episode release must match the recipe's source times. Different releases require new in/out/hit annotations; filename rebinding alone does not establish compatibility.

## Project contract

`project.json`:

```json
{
  "timeline": "timeline.json",
  "sources": "sources.json",
  "music": "media/music.mp3",
  "output": "output/amv.mp4",
  "fps": 30,
  "width": 1920,
  "height": 1080,
  "active_height": 900,
  "music_offset": 0,
  "music_gain": 0.70,
  "allow_source_overlap": false,
  "max_sfx_shift_frames": 2
}
```

Paths are relative to the project, or absolute local paths. `sources.json` maps source IDs to objects containing `path` and `duration`; an unbound recipe substitutes `filename` for `path`. `timeline.json` contains `fps`, `frames`, `duration`, and `shots`.

Required shot fields: `index`, `source`, `source_in`, `source_out`, `start_frame`, `end_frame`, `frames`. Timeline frame intervals are half-open and contiguous. Source times are seconds. `duration`, `start`, `source_span`, and `speed`, when present, must agree with the canonical values. `verified` records manual visual review; an absent flag yields a warning, never an automatic visual approval.

Optional fields: `hit` (source seconds), `anchor_offset`, `anchor_timeline`, `warp`, `sfx`, `sfx_shift`, `sfx_peak`, `flip`, `crop_box`, `crop_y`, `fx`, `fade_in`, `fade_out`, `note`. `crop_box=[x,y,width,height]` uses normalized 1920×1080 coordinates. Without a crop the source is scaled to the active picture. `fx="impact"` enables the restrained three-frame RGB shift; it does not determine where a hit occurs.

`warp=[source_offset,output_offset,speed_before,speed_after]` is a two-segment map anchored at the annotated hit. Each segment must end at the same output time used by the timeline. Constant-speed clips derive speed from `(source_out-source_in)/(frames/fps)`. The audio uses the same positive rates with chained pitch-preserving `atempo` filters. `first_decoded_source_pts`, when explicitly measured, identifies a fractional-frame seek offset; picture and sound start at that source time. It is not a guessed offset.

`sfx_shift` is a measured residual correction. Its conservative default limit is two output frames; set the project’s finite, nonnegative `max_sfx_shift_frames` to a different measured limit when appropriate. It must not substitute for reviewing the original effect's onset. The optional `sfx_peak` sets a cue's pre-master sample-peak target (default 0.20). Percussive masking, bandpass, short envelopes, and music ducking are applied; this is not guaranteed clean sound-effect separation. `mix --raw-sfx` disables the percussive mask only.

## Output and verification

The project receives `clips/`, `audio/{music,sfx,mix}.wav`, `logs/`, `reports/`, `shot_list.csv`, and the configured final MP4. Rendered clips cache the shot definition and source size/modification time; changed definitions invalidate the cache. The mixed-audio manifest records each effect source’s path, size, and modification time. Assemble rejects stale clips and outdated mixes, including source-audio or music changes. Assemble writes `reports/final.json`, binding the current project/timeline, source files, mixed WAV, and final MP4; validate rejects a missing or stale final manifest before decoding. This command validates a pipeline-produced project, not an unrelated external MP4.

Rendering strictly limits decoding to each source interval. It first tries without any cloned tail. Exactly one missing output frame may be padded for sampling-rounding closure, with `rounding_tail_frames: 1` recorded. Larger deficits fail and require correcting source bounds or timing. This limit prevents disguising missing action with long frozen tails.

`check` verifies source bounds, overlaps, contiguous integer frames, map rates, anchors, source audio availability, and music length. `--metadata-only` skips media checks and cannot validate a finished work. `validate` counts output frames, checks rate/resolution/audio format, fully decodes both streams with fatal decoder errors enabled, records black intervals and loudness diagnostics, and compares decoded AAC against the mixed WAV at zero offset. It does not establish perceived smoothness or replace watching and listening.

The smoke test generates two synthetic clips and a tone bed in a temporary directory, exercises constant and piecewise timing, renders and caches CPU clips, mixes original audio, assembles an MP4, validates 75 frames and zero-offset audio, and checks that out-of-bounds source edits, stale audio sources, stale final revisions, missing manifests, and invalid shift limits are rejected. It also verifies an explicitly raised sound-shift limit. `--keep` retains the temporary files for inspection. No source anime or commercial music is included.
