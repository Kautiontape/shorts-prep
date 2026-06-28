import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path

from flask import Flask, request, jsonify
import r2

app = Flask(__name__)

from werkzeug.exceptions import HTTPException


@app.errorhandler(HTTPException)
def _json_http_error(e):
    return jsonify(error=e.description, status=e.code), e.code


@app.errorhandler(Exception)
def _json_error(e):
    # Flask logs the traceback server-side; don't leak internals to the client.
    return jsonify(error='Internal server error', status=500), 500

UPLOAD_DIR = Path(tempfile.gettempdir()) / 'shorts-prep'
UPLOAD_DIR.mkdir(exist_ok=True)

# Check if h264_metadata bitstream filter is available (degrade gracefully if
# ffmpeg is absent, e.g. in the test environment).
try:
    _bsf_check = subprocess.run(['ffmpeg', '-bsfs'], capture_output=True, text=True)
    BSF_AVAILABLE = 'h264_metadata' in _bsf_check.stdout
except (OSError, FileNotFoundError):
    BSF_AVAILABLE = False

STANDARD_FRAMERATES = {24, 25, 30, 48, 50, 60}

JOB_ID_RE = re.compile(r'^[0-9a-f]{12}$')
ALLOWED_EXT = {'.mov', '.mp4'}

# Labels for the compatibility checks
CHECK_LABELS = {
    'resolution': 'Resolution',
    'pix_fmt': 'Pixel format',
    'video_codec': 'Video codec',
    'audio_codec': 'Audio codec',
    'color_primaries': 'Color primaries',
    'color_transfer': 'Color transfer',
    'color_space': 'Color space',
    'framerate': 'Frame rate',
    'stream_count': 'Stream count',
    'timecode_tag': 'Timecode tag',
    'sar': 'Sample aspect ratio',
    'faststart': 'Fast start (moov)',
}

# Which checks each mode can fix
MODE_FIXES = {
    'quick_fix': {'stream_count', 'timecode_tag', 'faststart'},
    'metadata_fix': {'stream_count', 'timecode_tag', 'faststart',
                     'color_primaries', 'color_transfer', 'color_space'},
    're_encode': set(CHECK_LABELS.keys()),
}


def probe_detailed(path):
    """Run ffprobe and return parsed JSON with all stream/format info."""
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-print_format', 'json',
         '-show_streams', '-show_format', str(path)],
        capture_output=True, text=True
    )
    return json.loads(r.stdout) if r.returncode == 0 else {}


def check_faststart(path):
    """Check if moov atom comes before mdat (required for streaming)."""
    try:
        with open(path, 'rb') as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], 'big')
                atom_type = header[4:8]
                if atom_type == b'moov':
                    return True
                if atom_type == b'mdat':
                    return False
                if size == 1:  # 64-bit extended size
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = int.from_bytes(ext, 'big')
                    f.seek(size - 16, 1)
                elif size < 8:
                    break
                else:
                    f.seek(size - 8, 1)
    except (OSError, ValueError):
        pass
    return False


def parse_framerate(rate_str):
    """Parse ffprobe frame rate string like '30/1' or '24000/1001' to float."""
    try:
        frac = Fraction(rate_str)
        return float(frac)
    except (ValueError, ZeroDivisionError):
        return 0.0


def run_checks(probe_data, path):
    """Evaluate YouTube Shorts compatibility checks against probe data."""
    checks = {}
    streams = probe_data.get('streams', [])

    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    total_streams = len(streams)

    # Resolution
    if video:
        w, h = video.get('width', 0), video.get('height', 0)
        checks['resolution'] = {
            'ok': w == 1080 and h == 1920,
            'value': f'{w}x{h}',
            'expected': '1080x1920',
        }
    else:
        checks['resolution'] = {'ok': False, 'value': 'no video', 'expected': '1080x1920'}

    # Pixel format
    pf = video.get('pix_fmt', 'unknown') if video else 'unknown'
    checks['pix_fmt'] = {'ok': pf == 'yuv420p', 'value': pf, 'expected': 'yuv420p'}

    # Video codec
    vc = video.get('codec_name', 'unknown') if video else 'unknown'
    checks['video_codec'] = {'ok': vc == 'h264', 'value': vc, 'expected': 'h264'}

    # Audio codec
    ac = audio.get('codec_name', 'unknown') if audio else 'unknown'
    checks['audio_codec'] = {'ok': ac == 'aac', 'value': ac, 'expected': 'aac'}

    # Color primaries
    cp = video.get('color_primaries', 'unknown') if video else 'unknown'
    checks['color_primaries'] = {
        'ok': cp == 'bt709',
        'value': cp,
        'expected': 'bt709',
    }

    # Color transfer
    ct = video.get('color_transfer', 'unknown') if video else 'unknown'
    checks['color_transfer'] = {
        'ok': ct == 'bt709',
        'value': ct,
        'expected': 'bt709',
    }

    # Color space
    cs = video.get('color_space', 'unknown') if video else 'unknown'
    checks['color_space'] = {
        'ok': cs == 'bt709',
        'value': cs,
        'expected': 'bt709',
    }

    # Frame rate
    if video:
        fps = parse_framerate(video.get('r_frame_rate', '0/1'))
        fps_rounded = round(fps)
        checks['framerate'] = {
            'ok': fps_rounded in STANDARD_FRAMERATES,
            'value': f'{fps:.2f} fps',
            'expected': '24/25/30/50/60 fps',
        }
    else:
        checks['framerate'] = {'ok': False, 'value': 'unknown', 'expected': '24/25/30/50/60 fps'}

    # Stream count
    checks['stream_count'] = {
        'ok': total_streams == 2,
        'value': str(total_streams),
        'expected': '2 (video + audio)',
    }

    # Timecode tag on video stream
    vtags = video.get('tags', {}) if video else {}
    tc = vtags.get('timecode', '')
    checks['timecode_tag'] = {
        'ok': tc == '',
        'value': tc if tc else 'none',
        'expected': 'none',
    }

    # SAR
    sar = video.get('sample_aspect_ratio', 'unknown') if video else 'unknown'
    checks['sar'] = {
        'ok': sar in ('1:1', 'N/A'),
        'value': sar,
        'expected': '1:1',
    }

    # Fast start
    fs = check_faststart(path)
    checks['faststart'] = {
        'ok': fs,
        'value': 'yes' if fs else 'no',
        'expected': 'yes',
    }

    return checks


def recommend_mode(checks):
    """Recommend the lightest processing mode that fixes all issues."""
    failing = {k for k, v in checks.items() if not v['ok']}
    if not failing:
        return 'quick_fix'  # Nothing wrong, but still clean up just in case
    if failing <= MODE_FIXES['quick_fix']:
        return 'quick_fix'
    if failing <= MODE_FIXES['metadata_fix'] and BSF_AVAILABLE:
        return 'metadata_fix'
    return 're_encode'


def build_ffmpeg_cmd(mode, input_path, output_path):
    """Build the ffmpeg command for the given processing mode."""
    if mode == 'quick_fix':
        return [
            'ffmpeg', '-y', '-i', str(input_path),
            '-map', '0:v:0', '-map', '0:a:0',
            '-c', 'copy',
            '-map_metadata', '-1',
            '-movflags', '+faststart',
            str(output_path),
        ]
    elif mode == 'metadata_fix':
        return [
            'ffmpeg', '-y', '-i', str(input_path),
            '-map', '0:v:0', '-map', '0:a:0',
            '-c', 'copy',
            '-map_metadata', '-1',
            '-bsf:v', 'h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1',
            '-movflags', '+faststart',
            str(output_path),
        ]
    else:  # re_encode
        return [
            'ffmpeg', '-y', '-i', str(input_path),
            '-map', '0:v:0', '-map', '0:a:0',
            '-map_metadata', '-1',
            '-vf', 'scale=1080:1920:flags=lanczos,format=yuv420p,setsar=1',
            '-c:v', 'libx264', '-profile:v', 'high', '-level', '4.1',
            '-preset', 'medium', '-crf', '18',
            '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
            '-r', '30', '-g', '30', '-bf', '2',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
            '-movflags', '+faststart',
            str(output_path),
        ]


def _status_path(job_dir):
    return job_dir / 'status.json'


def _write_status(job_dir, data):
    """Atomically record a job's state so any gunicorn worker can report it."""
    tmp = job_dir / 'status.json.tmp'
    tmp.write_text(json.dumps(data))
    tmp.replace(_status_path(job_dir))


def _read_status(job_dir):
    try:
        return json.loads(_status_path(job_dir).read_text())
    except (OSError, ValueError):
        return None


def _prune_job_files(job_dir):
    """Drop the large media files once a job finishes, keeping status.json so
    /status can still report the result."""
    for f in job_dir.iterdir():
        if f.name == 'status.json':
            continue
        try:
            f.unlink()
        except OSError:
            pass


def _run_job(job_id, mode):
    """Run the ffmpeg pipeline in the background, recording progress to
    status.json. A long re-encode can far exceed Cloudflare's ~100s proxy
    limit, so /process can't hold the request open until it's done."""
    job_dir = UPLOAD_DIR / job_id
    try:
        inputs = list(job_dir.glob('input.*'))
        if not inputs:
            _write_status(job_dir, {'state': 'error', 'error': 'Input file not found'})
            return
        input_path = inputs[0]

        # Probe before
        before_probe = probe_detailed(str(input_path))
        before_checks = run_checks(before_probe, str(input_path))

        # Use original filename with -shorts suffix
        name_file = job_dir / '.original_name'
        if name_file.exists():
            orig = Path(name_file.read_text().strip()).stem
            output_name = f'{orig}-shorts.mp4'
        else:
            output_name = 'shorts-ready.mp4'
        output_path = job_dir / output_name

        cmd = build_ffmpeg_cmd(mode, input_path, output_path)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            _write_status(job_dir, {'state': 'error',
                                    'error': f'ffmpeg failed: {result.stderr[-500:]}'})
            return

        # Probe after
        after_probe = probe_detailed(str(output_path))
        after_checks = run_checks(after_probe, str(output_path))
        out_size = output_path.stat().st_size / (1024 * 1024)

        # Store the result in R2 and hand back a presigned download URL.
        out_key = f'outputs/{job_id}/{output_name}'
        try:
            r2.upload_file(output_path, out_key, content_type='video/mp4')
        except Exception as e:
            _write_status(job_dir, {'state': 'error', 'error': f'Could not store result: {e}'})
            return
        download_url = r2.presign_get(out_key, output_name)

        _write_status(job_dir, {
            'state': 'done',
            'result': {
                'id': job_id,
                'output_name': output_name,
                'mode_used': mode,
                'before_checks': before_checks,
                'after_checks': after_checks,
                'file_size_mb': out_size,
                'download_url': download_url,
            },
        })
    except Exception:
        # Don't leak internals; the traceback is logged server-side.
        app.logger.exception('job %s failed', job_id)
        _write_status(job_dir, {'state': 'error', 'error': 'Internal server error'})
    finally:
        _prune_job_files(job_dir)


def cleanup_old_jobs(max_age=7200):
    """Remove job directories older than max_age seconds."""
    now = time.time()
    for d in UPLOAD_DIR.iterdir():
        if d.is_dir():
            try:
                if now - d.stat().st_mtime > max_age:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


# ──────────────────────────────────────────────
# HTML Frontend
# ──────────────────────────────────────────────

HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shorts Prep</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: #0f0f0f; color: #e1e1e1;
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; padding: 2rem;
  }
  h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 0.25rem; color: #fff; }
  .subtitle { font-size: 0.85rem; color: #888; margin-bottom: 2rem; }
  .panel {
    width: 100%; max-width: 520px; display: none;
  }
  .panel.visible { display: block; }

  /* Drop zone */
  .drop-zone {
    width: 100%; max-width: 520px; border: 2px dashed #333;
    border-radius: 12px; padding: 3rem 2rem; text-align: center;
    cursor: pointer; transition: border-color 0.2s, background 0.2s;
  }
  .drop-zone:hover, .drop-zone.drag-over {
    border-color: #ff4444; background: rgba(255, 68, 68, 0.05);
  }
  .drop-zone.processing { pointer-events: none; border-color: #555; opacity: 0.5; }
  .drop-icon { font-size: 2.5rem; margin-bottom: 0.75rem; opacity: 0.6; }
  .drop-label { font-size: 0.95rem; color: #aaa; }
  .drop-label strong { color: #ff4444; }
  .file-types { font-size: 0.75rem; color: #666; margin-top: 0.5rem; }
  input[type="file"] { display: none; }

  /* Progress */
  .file-name { font-size: 0.85rem; color: #ccc; margin-bottom: 0.75rem; word-break: break-all; }
  .progress-bar {
    width: 100%; height: 4px; background: #222;
    border-radius: 2px; overflow: hidden; margin-bottom: 0.75rem;
  }
  .progress-fill {
    height: 100%; background: #ff4444; width: 0%;
    transition: width 0.3s; border-radius: 2px;
  }
  .progress-fill.indeterminate {
    width: 30%; animation: slide 1.2s infinite ease-in-out;
  }
  @keyframes slide {
    0% { margin-left: 0; } 50% { margin-left: 70%; } 100% { margin-left: 0; }
  }
  .status-text { font-size: 0.8rem; color: #888; }

  /* Checklist */
  .section-label {
    font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: #666; margin-bottom: 0.5rem; margin-top: 1.25rem;
  }
  .checklist {
    background: #1a1a1a; border-radius: 8px; padding: 0.75rem;
    margin-bottom: 1rem;
  }
  .check-item {
    display: flex; align-items: center; gap: 0.5rem;
    padding: 0.3rem 0; font-size: 0.8rem;
  }
  .check-icon { width: 1.2rem; text-align: center; flex-shrink: 0; }
  .check-icon.pass { color: #4caf50; }
  .check-icon.fail { color: #f44336; }
  .check-icon.fixed { color: #4caf50; }
  .check-label { color: #aaa; min-width: 110px; }
  .check-value { color: #ccc; font-family: monospace; font-size: 0.75rem; }
  .check-value.fail { color: #f44336; }
  .check-value.fixed { color: #4caf50; }
  .check-expected {
    color: #666; font-size: 0.7rem; margin-left: auto;
    font-family: monospace;
  }

  /* Mode selector */
  .mode-cards { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 1.25rem; }
  .mode-card {
    background: #1a1a1a; border: 2px solid #333; border-radius: 8px;
    padding: 0.75rem 1rem; cursor: pointer; transition: border-color 0.2s;
    position: relative;
  }
  .mode-card:hover { border-color: #555; }
  .mode-card.selected { border-color: #ff4444; }
  .mode-card.disabled { opacity: 0.4; pointer-events: none; }
  .mode-card input { display: none; }
  .mode-title {
    font-size: 0.85rem; color: #fff; font-weight: 600;
    display: flex; align-items: center; gap: 0.5rem;
  }
  .mode-badge {
    font-size: 0.6rem; background: #ff4444; color: #fff;
    padding: 0.15rem 0.4rem; border-radius: 3px; text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .mode-desc { font-size: 0.75rem; color: #888; margin-top: 0.25rem; }
  .mode-fixes { font-size: 0.7rem; color: #4caf50; margin-top: 0.25rem; }
  .mode-nofixes { font-size: 0.7rem; color: #666; margin-top: 0.15rem; }

  /* Buttons */
  .btn {
    display: inline-block; padding: 0.6rem 1.5rem; background: #ff4444;
    color: #fff; border: none; border-radius: 6px; font-size: 0.9rem;
    cursor: pointer; transition: background 0.2s; text-decoration: none;
  }
  .btn:hover { background: #e03030; }
  .btn-secondary {
    background: transparent; border: 1px solid #444;
    color: #aaa; margin-left: 0.5rem;
  }
  .btn-secondary:hover { border-color: #666; color: #ccc; background: transparent; }
  .btn-row { text-align: center; margin-top: 1rem; }

  /* Results */
  .result-header { text-align: center; margin-bottom: 1rem; }
  .result-check { font-size: 2rem; color: #4caf50; }
  .result-text { font-size: 0.9rem; color: #aaa; margin-top: 0.25rem; }
  .result-warn { color: #ff9800; }
  .result-meta {
    font-size: 0.75rem; color: #666; text-align: center;
    margin-bottom: 1rem;
  }

  /* Before/After */
  .ba-grid {
    display: grid; grid-template-columns: 1fr auto 1fr;
    gap: 0 0.5rem; font-size: 0.75rem; margin-bottom: 1rem;
    background: #1a1a1a; border-radius: 8px; padding: 0.75rem;
  }
  .ba-header {
    font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: #666; padding-bottom: 0.4rem; border-bottom: 1px solid #2a2a2a;
    margin-bottom: 0.3rem;
  }
  .ba-label { color: #aaa; padding: 0.2rem 0; }
  .ba-arrow { color: #555; padding: 0.2rem 0; text-align: center; }
  .ba-val { font-family: monospace; padding: 0.2rem 0; }
  .ba-val.pass { color: #4caf50; }
  .ba-val.fail { color: #f44336; }

  /* Link + QR */
  .link-row {
    display: flex; align-items: center; gap: 0.5rem;
    background: #1a1a1a; border-radius: 8px; padding: 0.5rem 0.75rem;
    margin-bottom: 1.25rem;
  }
  .link-url {
    flex: 1; font-size: 0.75rem; color: #aaa; font-family: monospace;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    user-select: all;
  }
  .copy-btn {
    background: #333; border: none; color: #ccc; padding: 0.3rem 0.6rem;
    border-radius: 4px; font-size: 0.75rem; cursor: pointer;
    white-space: nowrap; transition: background 0.2s;
  }
  .copy-btn:hover { background: #444; }
  .copy-btn.copied { background: #2e7d32; color: #fff; }
  #qrCode { margin-bottom: 1.25rem; text-align: center; }
  #qrCode canvas { border-radius: 8px; }
  .phone-hint { font-size: 0.7rem; color: #555; margin-bottom: 1rem; text-align: center; }

  .error-text {
    color: #ff6b6b; font-size: 0.85rem; margin-top: 1rem; display: none;
    max-width: 520px; word-break: break-all;
  }
  .error-text.visible { display: block; }

  /* History */
  .history { width: 100%; max-width: 520px; margin-top: 2rem; display: none; }
  .history.visible { display: block; }
  .history-head {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 0.5rem;
  }
  .history-head .section-label { margin: 0; }
  .history-clear {
    background: none; border: none; color: #666; font-size: 0.7rem;
    cursor: pointer; text-decoration: underline;
  }
  .history-clear:hover { color: #999; }
  .history-item {
    display: flex; align-items: center; gap: 0.6rem;
    background: #1a1a1a; border-radius: 8px; padding: 0.55rem 0.75rem;
    margin-bottom: 0.5rem;
  }
  .history-info { flex: 1; min-width: 0; }
  .history-name {
    font-size: 0.8rem; color: #ddd; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .history-meta { font-size: 0.68rem; color: #666; margin-top: 0.1rem; }
  .history-actions { display: flex; gap: 0.4rem; flex-shrink: 0; }
  .history-btn {
    background: #333; border: none; color: #ccc; padding: 0.3rem 0.6rem;
    border-radius: 4px; font-size: 0.72rem; cursor: pointer;
    white-space: nowrap; transition: background 0.2s; text-decoration: none;
  }
  .history-btn:hover { background: #444; }
  .history-btn.primary { background: #ff4444; color: #fff; }
  .history-btn.primary:hover { background: #e03030; }
  .history-remove {
    background: none; border: none; color: #555; font-size: 1rem;
    cursor: pointer; line-height: 1; padding: 0 0.2rem;
  }
  .history-remove:hover { color: #999; }
</style>
</head>
<body>

<h1>Shorts Prep</h1>
<p class="subtitle">Fix DaVinci Resolve exports for YouTube Shorts</p>

<!-- STEP 1: Drop zone -->
<div class="drop-zone" id="dropZone">
  <div class="drop-icon">&#x1F4F9;</div>
  <div class="drop-label">Drop your video here or <strong>browse</strong></div>
  <div class="file-types">.mov .mp4</div>
</div>
<input type="file" id="fileInput" accept=".mov,.mp4,video/quicktime,video/mp4">

<!-- Upload progress -->
<div class="panel" id="uploadPanel">
  <div class="file-name" id="fileName"></div>
  <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
  <div class="status-text" id="statusText"></div>
</div>

<!-- STEP 2: Diagnostics + mode selection -->
<div class="panel" id="diagPanel">
  <div class="file-name" id="diagFileName"></div>
  <div class="section-label">YouTube Shorts Compatibility</div>
  <div class="checklist" id="checklist"></div>
  <div class="section-label">Processing Mode</div>
  <div class="mode-cards" id="modeCards"></div>
  <div class="btn-row">
    <button class="btn" id="processBtn">Process</button>
    <button class="btn btn-secondary" id="cancelBtn">Cancel</button>
  </div>
</div>

<!-- Processing progress -->
<div class="panel" id="processPanel">
  <div class="file-name" id="procFileName"></div>
  <div class="progress-bar"><div class="progress-fill indeterminate" id="procProgress"></div></div>
  <div class="status-text" id="procStatus">Processing...</div>
</div>

<!-- STEP 3: Results -->
<div class="panel" id="resultPanel">
  <div class="result-header">
    <div class="result-check" id="resultIcon">&#x2713;</div>
    <div class="result-text" id="resultText">Ready for YouTube Shorts</div>
  </div>
  <div class="result-meta" id="resultMeta"></div>
  <div class="section-label">Before / After</div>
  <div class="ba-grid" id="baGrid"></div>
  <div class="link-row">
    <span class="link-url" id="linkUrl"></span>
    <button class="copy-btn" id="copyBtn">Copy link</button>
  </div>
  <div id="qrCode"></div>
  <div class="phone-hint">Scan QR or paste link on your phone to download directly</div>
  <div class="btn-row">
    <a class="btn" id="downloadBtn" href="#">Download</a>
    <button class="btn btn-secondary" id="resetBtn">Process another</button>
  </div>
</div>

<p class="error-text" id="errorText"></p>

<!-- Past jobs (localStorage only — never leaves this browser) -->
<div class="history" id="historySection">
  <div class="history-head">
    <div class="section-label">Recent jobs</div>
    <button class="history-clear" id="historyClear">Clear all</button>
  </div>
  <div id="historyList"></div>
</div>

<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
<script>
// Elements
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const uploadPanel = document.getElementById('uploadPanel');
const fileName = document.getElementById('fileName');
const progressFill = document.getElementById('progressFill');
const statusText = document.getElementById('statusText');
const diagPanel = document.getElementById('diagPanel');
const diagFileName = document.getElementById('diagFileName');
const checklist = document.getElementById('checklist');
const modeCards = document.getElementById('modeCards');
const processBtn = document.getElementById('processBtn');
const cancelBtn = document.getElementById('cancelBtn');
const processPanel = document.getElementById('processPanel');
const procFileName = document.getElementById('procFileName');
const procStatus = document.getElementById('procStatus');
const resultPanel = document.getElementById('resultPanel');
const resultIcon = document.getElementById('resultIcon');
const resultText = document.getElementById('resultText');
const resultMeta = document.getElementById('resultMeta');
const baGrid = document.getElementById('baGrid');
const linkUrl = document.getElementById('linkUrl');
const copyBtn = document.getElementById('copyBtn');
const qrCode = document.getElementById('qrCode');
const downloadBtn = document.getElementById('downloadBtn');
const resetBtn = document.getElementById('resetBtn');
const errorText = document.getElementById('errorText');
const historySection = document.getElementById('historySection');
const historyList = document.getElementById('historyList');
const historyClear = document.getElementById('historyClear');

const CHECK_LABELS = {
  resolution: 'Resolution', pix_fmt: 'Pixel format', video_codec: 'Video codec',
  audio_codec: 'Audio codec', color_primaries: 'Color primaries',
  color_transfer: 'Color transfer', color_space: 'Color space',
  framerate: 'Frame rate', stream_count: 'Streams', timecode_tag: 'Timecode tag',
  sar: 'SAR', faststart: 'Fast start',
};

const CHECK_ORDER = ['resolution', 'pix_fmt', 'video_codec', 'audio_codec',
  'color_primaries', 'color_transfer', 'color_space', 'framerate',
  'stream_count', 'timecode_tag', 'sar', 'faststart'];

let currentJobId = null;
let selectedMode = null;
let currentFilename = null;
let currentFileSizeMb = null;

// Drop zone events
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files.length) analyzeFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) analyzeFile(fileInput.files[0]);
});

copyBtn.addEventListener('click', () => {
  navigator.clipboard.writeText(linkUrl.textContent).then(() => {
    copyBtn.textContent = 'Copied!';
    copyBtn.classList.add('copied');
    setTimeout(() => { copyBtn.textContent = 'Copy link'; copyBtn.classList.remove('copied'); }, 2000);
  });
});

cancelBtn.addEventListener('click', resetAll);
resetBtn.addEventListener('click', resetAll);

historyClear.addEventListener('click', () => {
  saveHistory([]);
  renderHistory(true);
});

processBtn.addEventListener('click', async () => {
  if (!currentJobId || !selectedMode) return;
  showPanel('processPanel');
  procFileName.textContent = diagFileName.textContent;
  procStatus.textContent = selectedMode === 're_encode' ? 'Re-encoding (this may take a few minutes)...' : 'Processing...';
  try {
    // Kick off processing. It runs in the background on the server so the
    // request returns immediately (a long re-encode would otherwise blow past
    // Cloudflare's ~100s proxy timeout and 524).
    const resp = await fetch('/process/' + currentJobId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: selectedMode}),
    });
    const start = await readJson(resp);
    if (!resp.ok) throw new Error(start.error || ('HTTP ' + resp.status));

    // Poll until the job finishes. Each poll is a quick request, so no 524.
    const result = await pollStatus(currentJobId);
    showResults(result);
  } catch (err) {
    showError(err.message);
    showPanel(null);
  }
});

// Poll /status until the job is done or errors. Returns the result object.
function pollStatus(jobId) {
  const POLL_MS = 3000;
  const MAX_MS = 30 * 60 * 1000;  // give a long re-encode up to 30 minutes
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (Date.now() - start > MAX_MS) {
        reject(new Error('Processing timed out. Please try again.'));
        return;
      }
      try {
        const resp = await fetch('/status/' + jobId);
        const data = await readJson(resp);
        if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
        if (data.state === 'done') { resolve(data.result); return; }
        if (data.state === 'error') { reject(new Error(data.error || 'Processing failed')); return; }
        setTimeout(tick, POLL_MS);  // still processing
      } catch (err) {
        reject(err);
      }
    };
    tick();
  });
}

function resetAll() {
  showPanel(null);
  dropZone.classList.remove('processing');
  progressFill.style.width = '0%';
  progressFill.classList.remove('indeterminate');
  fileInput.value = '';
  qrCode.innerHTML = '';
  linkUrl.textContent = '';
  copyBtn.textContent = 'Copy link';
  copyBtn.classList.remove('copied');
  errorText.classList.remove('visible');
  currentJobId = null;
  selectedMode = null;
  currentFilename = null;
  currentFileSizeMb = null;
}

function showPanel(id) {
  ['uploadPanel', 'diagPanel', 'processPanel', 'resultPanel'].forEach(p => {
    document.getElementById(p).classList.toggle('visible', p === id);
  });
  dropZone.classList.toggle('processing', id !== null);
  // History only shows in the idle state (drop zone visible, no active step).
  renderHistory(id === null);
}

// ── Job history (localStorage only — nothing is sent to the server) ──
// Each entry: { jobId, filename, fileSizeMb, mode, outputName, downloadUrl, ts }
const HISTORY_KEY = 'shortsPrepJobs';
const HISTORY_MAX = 25;

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) {
    return [];
  }
}

function saveHistory(list) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX)));
  } catch (e) { /* storage full or disabled — history is best-effort */ }
}

// Record (or refresh) a finished job at the top of the list.
function recordJob(entry) {
  const list = loadHistory().filter(j => j.jobId !== entry.jobId);
  list.unshift(entry);
  saveHistory(list);
}

function removeJob(jobId) {
  saveHistory(loadHistory().filter(j => j.jobId !== jobId));
  renderHistory(true);
}

function renderHistory(idle) {
  const list = loadHistory();
  historySection.classList.toggle('visible', !!idle && list.length > 0);
  if (!idle || !list.length) return;
  historyList.innerHTML = '';
  list.forEach(entry => {
    const when = new Date(entry.ts).toLocaleString();
    const size = entry.fileSizeMb != null ? entry.fileSizeMb.toFixed(1) + ' MB · ' : '';
    const item = document.createElement('div');
    item.className = 'history-item';

    const info = document.createElement('div');
    info.className = 'history-info';
    info.innerHTML =
      '<div class="history-name">' + escapeHtml(entry.filename) + '</div>' +
      '<div class="history-meta">' + size + escapeHtml(entry.mode || '') + ' · ' + escapeHtml(when) + '</div>';

    const actions = document.createElement('div');
    actions.className = 'history-actions';

    const dl = document.createElement('a');
    dl.className = 'history-btn';
    dl.textContent = 'Download';
    dl.href = entry.downloadUrl || '#';
    if (entry.outputName) dl.download = entry.outputName;

    const redo = document.createElement('button');
    redo.className = 'history-btn primary';
    redo.textContent = 'Re-process';
    redo.addEventListener('click', () => reprocessJob(entry));

    const del = document.createElement('button');
    del.className = 'history-remove';
    del.innerHTML = '&times;';
    del.title = 'Remove from history';
    del.addEventListener('click', () => removeJob(entry.jobId));

    actions.appendChild(dl);
    actions.appendChild(redo);
    item.appendChild(info);
    item.appendChild(actions);
    item.appendChild(del);
    historyList.appendChild(item);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

async function analyzeFile(file) {
  if (!file.name.match(/\\.(mov|mp4)$/i)) { showError('Please use a .mov or .mp4 file'); return; }
  errorText.classList.remove('visible');
  showPanel('uploadPanel');
  fileName.textContent = file.name;
  progressFill.classList.remove('indeterminate');
  progressFill.style.width = '0%';

  try {
    // 1. Get a presigned upload URL
    statusText.textContent = 'Preparing upload...';
    const cuResp = await fetch('/create-upload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filename: file.name}),
    });
    const cu = await readJson(cuResp);
    if (!cuResp.ok) throw new Error(cu.error || ('HTTP ' + cuResp.status));

    // 2. PUT the file straight to R2 (bypasses the proxy size cap)
    await putToR2(cu.put_url, file);

    // 3. Analyze the uploaded object
    statusText.textContent = 'Analyzing...';
    progressFill.classList.add('indeterminate');
    await runAnalyze(cu.job_id, file.name);
  } catch (err) {
    showError(err.message);
    showPanel(null);
  }
}

// Analyze an object already in R2 (used by a fresh upload and by re-processing a
// past job from history, which skips the upload entirely).
async function runAnalyze(jobId, filename) {
  const anResp = await fetch('/analyze', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({job_id: jobId, filename: filename}),
  });
  const data = await readJson(anResp);
  if (!anResp.ok) throw new Error(data.error || ('HTTP ' + anResp.status));
  currentJobId = data.id;
  showDiagnostics(data);
}

// Re-run a past job: re-analyze its retained R2 source (no re-upload) and drop
// the user back on the diagnostics panel to pick a mode.
async function reprocessJob(entry) {
  errorText.classList.remove('visible');
  showPanel('uploadPanel');
  fileName.textContent = entry.filename;
  progressFill.classList.add('indeterminate');
  statusText.textContent = 'Loading previous upload...';
  try {
    await runAnalyze(entry.jobId, entry.filename);
  } catch (err) {
    showError(err.message + ' (the source may have expired — re-upload to process again)');
    showPanel(null);
  }
}

// Read a response as text and parse JSON defensively, so an HTML error page
// (e.g. from a proxy) surfaces as a readable message instead of a parse crash.
async function readJson(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch (e) {
    const excerpt = text.replace(/\\s+/g, ' ').trim().slice(0, 200);
    throw new Error('HTTP ' + resp.status + ': ' + (excerpt || resp.statusText));
  }
}

// PUT a file to a presigned URL with progress reporting and a stall watchdog
// that aborts if no bytes move for 30s (so it never sits silently at 1%).
function putToR2(url, file) {
  const STALL_MS = 30000;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let lastLoaded = 0;
    let lastMove = Date.now();
    const watchdog = setInterval(() => {
      if (Date.now() - lastMove > STALL_MS) {
        clearInterval(watchdog);
        xhr.abort();
        reject(new Error('Upload stalled \\u2014 no progress for 30s. Check your connection and retry.'));
      }
    }, 5000);
    xhr.upload.addEventListener('progress', e => {
      if (e.lengthComputable) {
        if (e.loaded !== lastLoaded) { lastLoaded = e.loaded; lastMove = Date.now(); }
        const pct = Math.round((e.loaded / e.total) * 100);
        progressFill.style.width = pct + '%';
        statusText.textContent = 'Uploading... ' + pct + '%';
      }
    });
    xhr.addEventListener('load', () => {
      clearInterval(watchdog);
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error('Upload failed (HTTP ' + xhr.status + ')'));
    });
    xhr.addEventListener('error', () => { clearInterval(watchdog); reject(new Error('Upload failed \\u2014 network error')); });
    xhr.addEventListener('abort', () => { clearInterval(watchdog); });
    xhr.open('PUT', url);
    xhr.send(file);
  });
}

function showDiagnostics(data) {
  currentFilename = data.filename;
  currentFileSizeMb = data.file_size_mb;
  diagFileName.textContent = data.filename + '  (' + data.file_size_mb.toFixed(1) + ' MB)';
  const checks = data.checks;
  const rec = data.recommended_mode;
  const failingKeys = CHECK_ORDER.filter(k => checks[k] && !checks[k].ok);

  // Build checklist
  checklist.innerHTML = '';
  CHECK_ORDER.forEach(key => {
    const c = checks[key];
    if (!c) return;
    const div = document.createElement('div');
    div.className = 'check-item';
    div.innerHTML =
      '<span class="check-icon ' + (c.ok ? 'pass' : 'fail') + '">' + (c.ok ? '&#x2713;' : '&#x2717;') + '</span>' +
      '<span class="check-label">' + CHECK_LABELS[key] + '</span>' +
      '<span class="check-value ' + (c.ok ? '' : 'fail') + '">' + c.value + '</span>' +
      (c.ok ? '' : '<span class="check-expected">need ' + c.expected + '</span>');
    checklist.appendChild(div);
  });

  // Build mode cards
  const modes = [
    { id: 'quick_fix', title: 'Quick Fix', desc: 'Remux only — instant',
      fixes: ['stream_count', 'timecode_tag', 'faststart'] },
    { id: 'metadata_fix', title: 'Metadata Fix', desc: 'Remux + fix color metadata — fast',
      fixes: ['stream_count', 'timecode_tag', 'faststart', 'color_primaries', 'color_transfer', 'color_space'] },
    { id: 're_encode', title: 'Full Re-encode', desc: 'Complete re-encode — slower but fixes everything',
      fixes: CHECK_ORDER.slice() },
  ];

  modeCards.innerHTML = '';
  modes.forEach(m => {
    const disabled = m.id === 'metadata_fix' && !data.bsf_available;
    const isRec = m.id === rec;
    const canFix = m.fixes.filter(f => failingKeys.includes(f));
    const cantFix = failingKeys.filter(f => !m.fixes.includes(f));

    const card = document.createElement('div');
    card.className = 'mode-card' + (isRec ? ' selected' : '') + (disabled ? ' disabled' : '');
    card.innerHTML =
      '<div class="mode-title">' + m.title +
        (isRec ? ' <span class="mode-badge">Recommended</span>' : '') +
        (disabled ? ' <span style="font-size:0.7rem;color:#666">(unavailable)</span>' : '') +
      '</div>' +
      '<div class="mode-desc">' + m.desc + '</div>' +
      (canFix.length ? '<div class="mode-fixes">Fixes: ' + canFix.map(f => CHECK_LABELS[f]).join(', ') + '</div>' : '') +
      (cantFix.length ? '<div class="mode-nofixes">Cannot fix: ' + cantFix.map(f => CHECK_LABELS[f]).join(', ') + '</div>' : '');

    if (!disabled) {
      card.addEventListener('click', () => {
        modeCards.querySelectorAll('.mode-card').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        selectedMode = m.id;
      });
    }
    modeCards.appendChild(card);
  });

  selectedMode = rec;
  showPanel('diagPanel');
}

function showResults(result) {
  const before = result.before_checks;
  const after = result.after_checks;
  const allPass = CHECK_ORDER.every(k => after[k] && after[k].ok);

  resultIcon.textContent = allPass ? '\\u2713' : '!';
  resultIcon.className = 'result-check' + (allPass ? '' : ' result-warn');
  resultText.textContent = allPass ? 'Ready for YouTube Shorts' : 'Some checks still failing — try Full Re-encode';
  resultText.className = 'result-text' + (allPass ? '' : ' result-warn');
  resultMeta.textContent = 'Mode: ' + result.mode_used + '  |  Size: ' + result.file_size_mb.toFixed(1) + ' MB';

  // Before/after grid — show only items that changed or failed
  baGrid.innerHTML =
    '<div class="ba-header">Before</div><div class="ba-header"></div><div class="ba-header">After</div>';
  CHECK_ORDER.forEach(key => {
    const b = before[key], a = after[key];
    if (!b || !a) return;
    if (b.ok && a.ok) return;  // Skip items that were fine and stayed fine
    baGrid.innerHTML +=
      '<div class="ba-val ' + (b.ok ? 'pass' : 'fail') + '">' + b.value + '</div>' +
      '<div class="ba-arrow">&#x2192;</div>' +
      '<div class="ba-val ' + (a.ok ? 'pass' : 'fail') + '">' + a.value + '</div>';
    // Add label row
    const label = document.createElement('div');
    label.className = 'ba-label';
    label.style.gridColumn = '1 / -1';
    label.style.fontSize = '0.65rem';
    label.style.color = '#555';
    label.style.marginTop = '-0.2rem';
    label.textContent = CHECK_LABELS[key];
  });

  // Rebuild grid properly with labels
  baGrid.innerHTML = '';
  CHECK_ORDER.forEach(key => {
    const b = before[key], a = after[key];
    if (!b || !a) return;
    if (b.ok && a.ok) return;
    const row = document.createElement('div');
    row.style.display = 'contents';
    row.innerHTML =
      '<div class="ba-label">' + CHECK_LABELS[key] + '</div>' +
      '<div class="ba-val ' + (b.ok ? 'pass' : 'fail') + '">' + b.value + '</div>' +
      '<div class="ba-val ' + (a.ok ? 'pass' : 'fail') + '">' + a.value + '</div>';
    baGrid.appendChild(row);
  });

  // Fix grid to 3 cols: label, before, after
  baGrid.style.gridTemplateColumns = 'auto 1fr 1fr';

  // Download link (presigned R2 GET URL)
  const fullUrl = result.download_url;
  downloadBtn.href = fullUrl;
  downloadBtn.download = result.output_name;
  linkUrl.textContent = fullUrl;

  // QR code
  qrCode.innerHTML = '';
  new QRCode(qrCode, { text: fullUrl, width: 160, height: 160,
    colorDark: '#ffffff', colorLight: '#1a1a1a', correctLevel: QRCode.CorrectLevel.L });

  // Remember this job so it can be re-downloaded or re-processed without a
  // re-upload. localStorage only — nothing is sent to the server.
  recordJob({
    jobId: result.id,
    filename: currentFilename || result.output_name,
    fileSizeMb: currentFileSizeMb,
    mode: result.mode_used,
    outputName: result.output_name,
    downloadUrl: result.download_url,
    ts: Date.now(),
  });

  showPanel('resultPanel');
}

function showError(msg) {
  errorText.textContent = msg;
  errorText.classList.add('visible');
}

// Show any saved jobs on load (idle state).
renderHistory(true);
</script>
</body>
</html>'''


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route('/')
def index():
    return HTML


@app.route('/create-upload', methods=['POST'])
def create_upload():
    cleanup_old_jobs()
    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    if not filename:
        return jsonify(error='No filename provided'), 400
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error='Please use a .mov or .mp4 file'), 400
    job_id = uuid.uuid4().hex[:12]
    key = f'inputs/{job_id}{ext}'
    put_url = r2.presign_put(key)
    return jsonify(job_id=job_id, put_url=put_url, key=key)


@app.route('/analyze', methods=['POST'])
def analyze():
    cleanup_old_jobs()
    data = request.get_json(silent=True) or {}
    job_id = (data.get('job_id') or '').strip()
    filename = (data.get('filename') or '').strip()
    if not JOB_ID_RE.match(job_id):
        return jsonify(error='Invalid job id'), 400
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error='Invalid file type'), 400

    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f'input{ext}'
    key = f'inputs/{job_id}{ext}'

    try:
        r2.download_to(key, input_path)
    except Exception as e:
        return jsonify(error=f'Could not fetch upload: {e}'), 502

    # Save original filename for the output name later.
    (job_dir / '.original_name').write_text(filename)

    probe = probe_detailed(str(input_path))
    checks = run_checks(probe, str(input_path))
    rec = recommend_mode(checks)
    file_size = input_path.stat().st_size / (1024 * 1024)

    # Keep the R2 input around (the lifecycle rule expires it later). This lets
    # the browser re-analyze and re-process a past job from its history without
    # re-uploading the source.

    return jsonify(
        id=job_id,
        filename=filename,
        file_size_mb=file_size,
        checks=checks,
        recommended_mode=rec,
        bsf_available=BSF_AVAILABLE,
    )


@app.route('/process/<job_id>', methods=['POST'])
def process(job_id):
    if not JOB_ID_RE.match(job_id):
        return jsonify(error='Invalid job id'), 400
    job_dir = UPLOAD_DIR / job_id
    if not job_dir.exists():
        return jsonify(error='Job not found'), 404

    inputs = list(job_dir.glob('input.*'))
    if not inputs:
        return jsonify(error='Input file not found'), 404

    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'quick_fix')
    if mode not in ('quick_fix', 'metadata_fix', 're_encode'):
        return jsonify(error='Invalid mode'), 400
    if mode == 'metadata_fix' and not BSF_AVAILABLE:
        return jsonify(error='Metadata fix not available in this ffmpeg build'), 400

    # A full re-encode can run for minutes — far past Cloudflare's ~100s proxy
    # timeout (524). Do the work in a background thread and let the browser poll
    # /status, so every HTTP request returns immediately.
    _write_status(job_dir, {'state': 'processing', 'mode': mode})
    threading.Thread(target=_run_job, args=(job_id, mode), daemon=True).start()
    return jsonify(id=job_id, state='processing'), 202


@app.route('/status/<job_id>', methods=['GET'])
def status(job_id):
    if not JOB_ID_RE.match(job_id):
        return jsonify(error='Invalid job id'), 400
    st = _read_status(UPLOAD_DIR / job_id)
    if st is None:
        return jsonify(error='Job not found'), 404
    return jsonify(st)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
