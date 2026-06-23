# Direct-to-R2 Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route large video uploads/downloads browser ↔ Cloudflare R2 via presigned URLs so they bypass Cloudflare's 100 MB request-body cap, while the Flask app keeps doing the ffmpeg work on a local copy.

**Architecture:** The browser PUTs the file straight to R2 (storage API, no 100 MB cap). The app hands out presigned PUT URLs, then on `/analyze` downloads the object to local temp (free R2→origin egress) and runs the existing probe/checks, deletes the R2 input, and on `/process` runs the existing ffmpeg pipeline, uploads the result to R2, and returns a presigned GET URL for the QR/phone download. All R2 I/O is isolated in a new `r2.py`. The app never receives file bodies anymore.

**Tech Stack:** Python 3.14, Flask, gunicorn, boto3 (R2 via S3 API), pytest + moto for tests. Spec: `docs/superpowers/specs/2026-06-23-direct-to-r2-upload-design.md`.

---

## File Structure

- **Create `r2.py`** — all R2/S3 I/O (presign, upload, download, delete). Only file importing boto3.
- **Create `setup_r2.py`** — one-time script applying bucket CORS + lifecycle. Not part of the running app.
- **Create `requirements.txt`** — runtime deps (flask, gunicorn, boto3).
- **Create `requirements-dev.txt`** — test deps (pytest, moto) + runtime.
- **Create `pytest.ini`** and **`conftest.py`** (repo root) — test config + dummy R2 env so imports never need real creds.
- **Create `.env.example`** — documents the four `R2_*` vars (no secrets).
- **Create `tests/test_r2.py`, `tests/test_app.py`** — unit/endpoint tests.
- **Modify `app.py`** — add `/create-upload`; rewrite `/analyze` (R2 download); update `/process` (R2 upload + presigned GET); add JSON error handlers; make the `BSF_AVAILABLE` check robust; remove `/download` and `MAX_CONTENT_LENGTH`; rewrite the frontend upload JS.
- **Modify `Dockerfile`** — install from `requirements.txt`, copy `r2.py`.
- **Modify `docker-compose.yml`** — add `env_file: .env`.

### Intentional deviation from the spec
The spec listed `presign_put(key, content_type, ...)`. We implement `presign_put(key, expires)` **without** a signed `Content-Type`. Signing `Content-Type` forces the browser to send a byte-exact matching header or R2 rejects the signature (the gotcha flagged in spec review). Omitting it from the signature removes that failure mode entirely; the input object's content-type is irrelevant since we only download it server-side.

---

## Task 1: Dependencies & test scaffolding

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `conftest.py`, `tests/test_smoke.py`
- Modify: `app.py:18-20` (robust BSF check)

- [ ] **Step 1: Create `requirements.txt`**

```
flask
gunicorn
boto3
```

- [ ] **Step 2: Create `requirements-dev.txt`**

```
-r requirements.txt
pytest
moto
```

- [ ] **Step 3: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
```

- [ ] **Step 4: Create `conftest.py` (repo root)**

```python
import os

# Dummy R2 settings so importing app/r2 never needs real credentials.
os.environ.setdefault('R2_ENDPOINT', 'https://acct.r2.cloudflarestorage.com')
os.environ.setdefault('R2_BUCKET', 'test-bucket')
os.environ.setdefault('R2_ACCESS_KEY', 'test-access')
os.environ.setdefault('R2_SECRET_KEY', 'test-secret')

import pytest


@pytest.fixture
def client():
    import app
    app.app.config['TESTING'] = True
    return app.app.test_client()
```

- [ ] **Step 5: Install dev dependencies**

Run: `pip install -r requirements-dev.txt`
Expected: installs flask, gunicorn, boto3, pytest, moto.

- [ ] **Step 6: Make the `BSF_AVAILABLE` check importable without ffmpeg**

In `app.py`, replace lines 18-20:

```python
# Check if h264_metadata bitstream filter is available
_bsf_check = subprocess.run(['ffmpeg', '-bsfs'], capture_output=True, text=True)
BSF_AVAILABLE = 'h264_metadata' in _bsf_check.stdout
```

with:

```python
# Check if h264_metadata bitstream filter is available (degrade gracefully if
# ffmpeg is absent, e.g. in the test environment).
try:
    _bsf_check = subprocess.run(['ffmpeg', '-bsfs'], capture_output=True, text=True)
    BSF_AVAILABLE = 'h264_metadata' in _bsf_check.stdout
except (OSError, FileNotFoundError):
    BSF_AVAILABLE = False
```

- [ ] **Step 7: Write a smoke test**

Create `tests/test_smoke.py`:

```python
def test_app_imports(client):
    # If app imports and a test client builds, the module is healthy.
    resp = client.get('/')
    assert resp.status_code == 200
```

- [ ] **Step 8: Run the smoke test**

Run: `pytest tests/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add requirements.txt requirements-dev.txt pytest.ini conftest.py tests/test_smoke.py app.py
git commit -m "chore: add test scaffolding and robust ffmpeg bsf check"
```

---

## Task 2: `r2.py` module (TDD)

**Files:**
- Create: `r2.py`
- Test: `tests/test_r2.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_r2.py`:

```python
import boto3
import pytest
from moto import mock_aws

import r2


def test_presign_put_contains_bucket_and_key():
    url = r2.presign_put('inputs/abc123.mp4')
    assert 'test-bucket' in url
    assert 'inputs/abc123.mp4' in url
    assert 'X-Amz-Signature' in url


def test_presign_get_contains_key_and_signature():
    url = r2.presign_get('outputs/abc/clip-shorts.mp4', 'clip-shorts.mp4')
    assert 'outputs/abc/clip-shorts.mp4' in url
    assert 'X-Amz-Signature' in url


@mock_aws
def test_upload_download_delete_roundtrip(tmp_path, monkeypatch):
    # Empty endpoint -> default AWS endpoint so moto intercepts; region must be
    # a real one for the default resolver.
    monkeypatch.setenv('R2_ENDPOINT', '')
    monkeypatch.setenv('R2_REGION', 'us-east-1')
    control = boto3.client('s3', region_name='us-east-1')
    control.create_bucket(Bucket='test-bucket')

    src = tmp_path / 'src.bin'
    src.write_bytes(b'hello world')
    r2.upload_file(src, 'inputs/x.bin', content_type='application/octet-stream')

    dst = tmp_path / 'dst.bin'
    r2.download_to('inputs/x.bin', dst)
    assert dst.read_bytes() == b'hello world'

    r2.delete('inputs/x.bin')
    assert control.list_objects_v2(Bucket='test-bucket').get('KeyCount', 0) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_r2.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'r2'` (or attribute errors).

- [ ] **Step 3: Implement `r2.py`**

```python
"""All Cloudflare R2 (S3 API) I/O. The only module that imports boto3.

Configuration comes from environment variables:
    R2_ENDPOINT   e.g. https://<account>.r2.cloudflarestorage.com (optional in tests)
    R2_BUCKET     bucket name
    R2_ACCESS_KEY S3 access key id
    R2_SECRET_KEY S3 secret access key
    R2_REGION     defaults to 'auto'
"""
import os

import boto3
from botocore.client import Config


def _settings():
    return {
        'endpoint': os.environ.get('R2_ENDPOINT', ''),
        'bucket': os.environ['R2_BUCKET'],
        'access_key': os.environ['R2_ACCESS_KEY'],
        'secret_key': os.environ['R2_SECRET_KEY'],
        'region': os.environ.get('R2_REGION', 'auto'),
    }


def _client():
    s = _settings()
    kwargs = dict(
        aws_access_key_id=s['access_key'],
        aws_secret_access_key=s['secret_key'],
        region_name=s['region'],
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
    )
    if s['endpoint']:
        kwargs['endpoint_url'] = s['endpoint']
    return boto3.client('s3', **kwargs)


def bucket():
    return _settings()['bucket']


def presign_put(key, expires=900):
    """Presigned PUT URL (15 min). Content-Type intentionally unsigned."""
    return _client().generate_presigned_url(
        'put_object',
        Params={'Bucket': bucket(), 'Key': key},
        ExpiresIn=expires,
    )


def presign_get(key, download_name, expires=86400):
    """Presigned GET URL (24 h) that downloads as `download_name`."""
    return _client().generate_presigned_url(
        'get_object',
        Params={
            'Bucket': bucket(),
            'Key': key,
            'ResponseContentDisposition': f'attachment; filename="{download_name}"',
        },
        ExpiresIn=expires,
    )


def download_to(key, local_path):
    _client().download_file(bucket(), key, str(local_path))


def upload_file(local_path, key, content_type='application/octet-stream'):
    _client().upload_file(
        str(local_path), bucket(), key,
        ExtraArgs={'ContentType': content_type},
    )


def delete(key):
    _client().delete_object(Bucket=bucket(), Key=key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_r2.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add r2.py tests/test_r2.py
git commit -m "feat: add r2 module for presigned R2 uploads/downloads"
```

---

## Task 3: `/create-upload` endpoint (TDD)

**Files:**
- Modify: `app.py` (imports near top; new route)
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_app.py`:

```python
def test_create_upload_returns_presigned_url(client):
    resp = client.post('/create-upload', json={'filename': 'My Clip.mp4'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body['job_id']) == 12
    assert body['key'].startswith('inputs/') and body['key'].endswith('.mp4')
    assert 'X-Amz-Signature' in body['put_url']


def test_create_upload_rejects_bad_extension(client):
    resp = client.post('/create-upload', json={'filename': 'clip.avi'})
    assert resp.status_code == 400


def test_create_upload_requires_filename(client):
    resp = client.post('/create-upload', json={})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_app.py -v`
Expected: FAIL with 404 (route not defined) / assertion errors.

- [ ] **Step 3: Add imports**

In `app.py`, add `import re` with the other stdlib imports, and `import r2` after the Flask import. Then add these module-level constants near the top (after `STANDARD_FRAMERATES`):

```python
JOB_ID_RE = re.compile(r'^[0-9a-f]{12}$')
ALLOWED_EXT = {'.mov', '.mp4'}
```

- [ ] **Step 4: Add the `/create-upload` route**

Add above the existing `/analyze` route:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_app.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: add /create-upload presigned URL endpoint"
```

---

## Task 4: JSON error handlers (TDD)

**Files:**
- Modify: `app.py` (import + handlers)
- Test: `tests/test_app.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_app.py`:

```python
def test_unknown_route_returns_json_not_html(client):
    resp = client.get('/no-such-route')
    assert resp.status_code == 404
    assert resp.get_json() is not None
    assert 'error' in resp.get_json()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_app.py::test_unknown_route_returns_json_not_html -v`
Expected: FAIL — `get_json()` is None because Flask returns an HTML 404.

- [ ] **Step 3: Add the error handlers**

In `app.py`, add near the top after `app = Flask(__name__)`:

```python
from werkzeug.exceptions import HTTPException


@app.errorhandler(HTTPException)
def _json_http_error(e):
    return jsonify(error=e.description, status=e.code), e.code


@app.errorhandler(Exception)
def _json_error(e):
    return jsonify(error=str(e) or 'Internal server error', status=500), 500
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_app.py::test_unknown_route_returns_json_not_html -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: return JSON for all error responses"
```

---

## Task 5: Rewrite `/analyze` to read from R2 (TDD)

**Files:**
- Modify: `app.py` (replace the `/analyze` route body)
- Test: `tests/test_app.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_app.py`:

```python
from pathlib import Path


def test_analyze_downloads_probes_and_deletes_input(client, monkeypatch):
    import app, r2
    written = {}
    def fake_download(key, path):
        Path(path).write_bytes(b'\x00' * 1024)
        written['key'] = key
    deleted = {}
    monkeypatch.setattr(r2, 'download_to', fake_download)
    monkeypatch.setattr(r2, 'delete', lambda key: deleted.update(key=key))
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    fake_checks = {k: {'ok': True, 'value': 'x', 'expected': 'x'} for k in app.CHECK_LABELS}
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: fake_checks)
    monkeypatch.setattr(app, 'recommend_mode', lambda c: 'quick_fix')

    resp = client.post('/analyze', json={'job_id': 'abcdef012345', 'filename': 'clip.mp4'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['id'] == 'abcdef012345'
    assert body['recommended_mode'] == 'quick_fix'
    assert written['key'] == 'inputs/abcdef012345.mp4'
    assert deleted['key'] == 'inputs/abcdef012345.mp4'


def test_analyze_rejects_bad_job_id(client):
    resp = client.post('/analyze', json={'job_id': 'BAD', 'filename': 'clip.mp4'})
    assert resp.status_code == 400


def test_analyze_download_failure_returns_502(client, monkeypatch):
    import r2
    def boom(key, path):
        raise RuntimeError('nope')
    monkeypatch.setattr(r2, 'download_to', boom)
    resp = client.post('/analyze', json={'job_id': 'abcdef012345', 'filename': 'clip.mp4'})
    assert resp.status_code == 502
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_app.py -k analyze -v`
Expected: FAIL — current `/analyze` expects a multipart file upload.

- [ ] **Step 3: Replace the `/analyze` route**

Replace the entire existing `analyze()` function with:

```python
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

    # The local copy is the working copy now; drop the R2 input.
    try:
        r2.delete(key)
    except Exception:
        pass

    return jsonify(
        id=job_id,
        filename=filename,
        file_size_mb=file_size,
        checks=checks,
        recommended_mode=rec,
        bsf_available=BSF_AVAILABLE,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_app.py -k analyze -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: analyze pulls input from R2 instead of multipart upload"
```

---

## Task 6: Update `/process` to upload result + return presigned GET (TDD)

**Files:**
- Modify: `app.py` (replace the `/process` route body)
- Test: `tests/test_app.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_app.py`:

```python
def _seed_job(app, job_id='abcdef012345', orig='clip.mp4'):
    job_dir = app.UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / 'input.mp4').write_bytes(b'\x00' * 10)
    (job_dir / '.original_name').write_text(orig)
    return job_dir


def test_process_uploads_result_and_returns_download_url(client, monkeypatch):
    import app, r2
    job_dir = _seed_job(app)

    def fake_run(cmd, capture_output=True, text=True):
        Path(cmd[-1]).write_bytes(b'\x00' * 20)  # cmd[-1] is the output path
        class R:
            returncode = 0
            stderr = ''
        return R()
    monkeypatch.setattr(app.subprocess, 'run', fake_run)
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    fake_checks = {k: {'ok': True, 'value': 'x', 'expected': 'x'} for k in app.CHECK_LABELS}
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: fake_checks)
    uploaded = {}
    monkeypatch.setattr(r2, 'upload_file',
                        lambda path, key, content_type='application/octet-stream': uploaded.update(key=key))
    monkeypatch.setattr(r2, 'presign_get', lambda key, name, expires=86400: f'https://signed/{key}')

    resp = client.post('/process/abcdef012345', json={'mode': 'quick_fix'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['output_name'] == 'clip-shorts.mp4'
    assert uploaded['key'] == 'outputs/abcdef012345/clip-shorts.mp4'
    assert body['download_url'] == 'https://signed/outputs/abcdef012345/clip-shorts.mp4'
    assert not job_dir.exists()  # local temp cleaned up


def test_process_invalid_mode_returns_400(client):
    import app
    _seed_job(app, job_id='abcdef999999')
    resp = client.post('/process/abcdef999999', json={'mode': 'nope'})
    assert resp.status_code == 400


def test_process_ffmpeg_failure_returns_500(client, monkeypatch):
    import app
    _seed_job(app, job_id='abcdeffailed')
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: {})
    def fail_run(cmd, capture_output=True, text=True):
        class R:
            returncode = 1
            stderr = 'boom'
        return R()
    monkeypatch.setattr(app.subprocess, 'run', fail_run)
    resp = client.post('/process/abcdeffailed', json={'mode': 'quick_fix'})
    assert resp.status_code == 500
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_app.py -k process -v`
Expected: FAIL — current `/process` has no `download_url` / R2 upload / cleanup.

- [ ] **Step 3: Replace the `/process` route**

Replace the entire existing `process()` function with:

```python
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
    input_path = inputs[0]

    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'quick_fix')
    if mode not in ('quick_fix', 'metadata_fix', 're_encode'):
        return jsonify(error='Invalid mode'), 400
    if mode == 'metadata_fix' and not BSF_AVAILABLE:
        return jsonify(error='Metadata fix not available in this ffmpeg build'), 400

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
        return jsonify(error=f'ffmpeg failed: {result.stderr[-500:]}'), 500

    # Probe after
    after_probe = probe_detailed(str(output_path))
    after_checks = run_checks(after_probe, str(output_path))
    out_size = output_path.stat().st_size / (1024 * 1024)

    # Store the result in R2 and hand back a presigned download URL.
    out_key = f'outputs/{job_id}/{output_name}'
    try:
        r2.upload_file(output_path, out_key, content_type='video/mp4')
    except Exception as e:
        return jsonify(error=f'Could not store result: {e}'), 502
    download_url = r2.presign_get(out_key, output_name)

    # Clean up local working files.
    shutil.rmtree(job_dir, ignore_errors=True)

    return jsonify(
        id=job_id,
        output_name=output_name,
        mode_used=mode,
        before_checks=before_checks,
        after_checks=after_checks,
        file_size_mb=out_size,
        download_url=download_url,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_app.py -k process -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: process uploads result to R2 and returns presigned download URL"
```

---

## Task 7: Remove `/download` and `MAX_CONTENT_LENGTH` (TDD)

**Files:**
- Modify: `app.py` (delete the download route; delete the config line)
- Test: `tests/test_app.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_app.py`:

```python
def test_download_route_removed(client):
    resp = client.get('/download/abcdef012345')
    assert resp.status_code == 404
    assert resp.get_json() is not None  # JSON 404, not HTML
```

- [ ] **Step 2: Run test to verify current behavior**

Run: `pytest tests/test_app.py::test_download_route_removed -v`
Expected: FAIL — the `/download/<job_id>` route still exists (returns 404 via that route's own logic but as a different shape) or 200.

- [ ] **Step 3: Delete the `/download` route**

In `app.py`, delete the entire `@app.route('/download/<job_id>')` function (the `download()` def and its decorator).

- [ ] **Step 4: Remove the upload size cap**

In `app.py`, delete this line (the app no longer receives file bodies):

```python
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB
```

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "refactor: drop /download route and MAX_CONTENT_LENGTH (R2 serves results)"
```

---

## Task 8: Frontend upload rewrite (manual verification — no JS test harness)

**Files:**
- Modify: `app.py` (the `HTML` string: `analyzeFile`, add helpers, `showResults`)

> Note: the JS lives inside a Python triple-quoted string, so backslashes are doubled (e.g. `\\.`, `\\u2014`, `\\s+`), matching the existing code.

- [ ] **Step 1: Replace `analyzeFile` and add helpers**

Replace the entire existing `async function analyzeFile(file) { ... }` with the following (which adds `readJson` and `putToR2`):

```javascript
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
    const anResp = await fetch('/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({job_id: cu.job_id, filename: file.name}),
    });
    const data = await readJson(anResp);
    if (!anResp.ok) throw new Error(data.error || ('HTTP ' + anResp.status));
    currentJobId = data.id;
    showDiagnostics(data);
  } catch (err) {
    showError(err.message);
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
```

- [ ] **Step 2: Point the result download at the presigned URL**

In `showResults(result)`, replace these lines:

```javascript
  // Download link
  const dlPath = '/download/' + result.id;
  const fullUrl = window.location.origin + dlPath;
  downloadBtn.href = dlPath;
  downloadBtn.download = result.output_name;
  linkUrl.textContent = fullUrl;
```

with:

```javascript
  // Download link (presigned R2 GET URL)
  const fullUrl = result.download_url;
  downloadBtn.href = fullUrl;
  downloadBtn.download = result.output_name;
  linkUrl.textContent = fullUrl;
```

- [ ] **Step 3: Sanity-check the page still serves**

Run: `pytest tests/test_smoke.py -v`
Expected: PASS (the `/` route returns 200 with the HTML).

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: upload directly to R2 with progress, stall watchdog, robust errors"
```

---

## Task 9: R2 bucket CORS + lifecycle script

**Files:**
- Create: `setup_r2.py`

- [ ] **Step 1: Create `setup_r2.py`**

```python
"""One-time Cloudflare R2 bucket setup: CORS (for browser PUTs) + lifecycle
(auto-expire job objects). Run locally with the R2_* env vars set:

    R2_ENDPOINT=... R2_BUCKET=... R2_ACCESS_KEY=... R2_SECRET_KEY=... \
        python setup_r2.py
"""
import r2

CORS = {
    'CORSRules': [{
        'AllowedOrigins': ['https://shorts.kautiontape.com', 'http://localhost:8080'],
        'AllowedMethods': ['PUT'],
        'AllowedHeaders': ['*'],
        'ExposeHeaders': ['ETag'],
        'MaxAgeSeconds': 3600,
    }]
}

LIFECYCLE = {
    'Rules': [{
        'ID': 'expire-jobs',
        'Status': 'Enabled',
        'Filter': {'Prefix': ''},
        'Expiration': {'Days': 1},
    }]
}


def main():
    client = r2._client()
    name = r2.bucket()
    client.put_bucket_cors(Bucket=name, CORSConfiguration=CORS)
    client.put_bucket_lifecycle_configuration(Bucket=name, LifecycleConfiguration=LIFECYCLE)
    print(f'Applied CORS + lifecycle to {name}')


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Run it against the real bucket** (needs real credentials)

Run (substitute real values from `.credentials`):

```bash
R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com \
R2_BUCKET=shorts-prep \
R2_ACCESS_KEY=<access> \
R2_SECRET_KEY=<secret> \
python setup_r2.py
```

Expected: `Applied CORS + lifecycle to shorts-prep`

- [ ] **Step 3: Commit**

```bash
git add setup_r2.py
git commit -m "chore: add one-time R2 CORS + lifecycle setup script"
```

---

## Task 10: Packaging — Dockerfile, compose, env example

**Files:**
- Modify: `Dockerfile`, `docker-compose.yml`
- Create: `.env.example`

- [ ] **Step 1: Update `Dockerfile`**

Replace the whole file with:

```dockerfile
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py r2.py ./

EXPOSE 8080
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--timeout", "600", "--workers", "2", "app:app"]
```

- [ ] **Step 2: Update `docker-compose.yml`**

Add `env_file: .env` under the `app` service (above `ports`):

```yaml
services:
  app:
    image: ghcr.io/kautiontape/shorts-prep:latest
    container_name: shorts_prep
    env_file: .env
    ports:
      - "127.0.0.1:8103:8080"
    volumes:
      - shorts_prep_data:/tmp/shorts-prep
    restart: always

volumes:
  shorts_prep_data:
```

- [ ] **Step 3: Create `.env.example`**

```
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET=shorts-prep
R2_ACCESS_KEY=
R2_SECRET_KEY=
# R2_REGION=auto
```

- [ ] **Step 4: Build the image locally to confirm it builds**

Run: `docker build -t shorts-prep:test .`
Expected: build succeeds (boto3 installed, app.py + r2.py copied).

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example
git commit -m "chore: install boto3, ship r2.py, load R2 env from .env"
```

---

## Task 11: Deploy & end-to-end verification

> This is the verification-before-completion gate. The auto-deploy fires on push to `main`, and `docker compose up` will fail if `.env` is missing — so the ktn `.env` and the R2 CORS/lifecycle MUST be in place before merge.

- [ ] **Step 1: Create `/opt/services/shorts-prep/.env` on ktn**

On ktn, create the file with the four `R2_*` values from `.credentials` (KEY=value form). Keep it unreadable by others: `chmod 600 .env`.

- [ ] **Step 2: Confirm R2 CORS + lifecycle were applied** (Task 9 Step 2 ran successfully).

- [ ] **Step 3: Local smoke run (optional but recommended)**

With a real local `.env`, run `python app.py`, open `http://localhost:8080`, and upload a small `.mp4`. Confirm: create-upload → PUT (progress bar moves) → analyze → process → the download button uses an R2 URL and downloads the file.

- [ ] **Step 4: Merge to `main` to trigger build + deploy**

```bash
git checkout main
git merge --no-ff r2-upload
git push origin main
```

Watch the `build & publish` then `Deploy to ktn` workflows complete.

- [ ] **Step 5: Real end-to-end through the domain**

On `https://shorts.kautiontape.com`, upload a **>100 MB** `.mov`/`.mp4`. Confirm the upload completes (no 1% stall, no "got `<`"), analysis + processing succeed, and the QR/download link downloads the finished file from R2.

- [ ] **Step 6: Confirm the original 250 MB file now works** — the symptom that started this is resolved.

---

## Self-Review

**Spec coverage:** presigned PUT bypass (T2/T3/T8); browser PUT direct with progress + stall watchdog (T8); `/analyze` downloads from R2 + deletes input (T5); `/process` uploads result + presigned GET (T6); remove `/download` + `MAX_CONTENT_LENGTH` (T7); `r2.py` module (T2); JSON error handlers (T4); robust client parsing (T8); boto3 / requirements (T10); compose `env_file` (T10); CORS + lifecycle via boto3 (T9); secrets in env only (T10 `.env.example`, conftest dummy creds); 24 h GET / 15 min PUT (T2 defaults); manual e2e (T11). All spec sections map to a task.

**Placeholder scan:** No TBD/TODO; every code/test step contains complete code; `<account>` / real secret values are deliberately left as substitutions in shell commands that run against live infrastructure (not code).

**Type consistency:** `r2.presign_put(key)`, `presign_get(key, name)`, `download_to(key, path)`, `upload_file(path, key, content_type=...)`, `delete(key)` are defined in T2 and called with matching signatures in T3/T5/T6/T9. `JOB_ID_RE` and `ALLOWED_EXT` defined in T3 and reused in T5/T6. Response field `download_url` produced in T6 and consumed in T8. Keys `inputs/<job_id><ext>` and `outputs/<job_id>/<output_name>` are consistent across create/analyze/process.
