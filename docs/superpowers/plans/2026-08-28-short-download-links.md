# Short Download Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 491-character presigned R2 URL in the result QR code with a 45-character `/d/<job_id>` link that redirects to a freshly signed R2 download.

**Architecture:** `job_id` is already unique and unguessable, so it doubles as the short code — no database. The R2 key becomes fully deterministic (`outputs/<job_id>.mp4`) with the display filename in a sidecar object (`outputs/<job_id>.name`). A new `GET /d/<code>` route reads the sidecar, mints a 5-minute presigned URL, and 302s to it.

**Tech Stack:** Python 3, Flask, boto3 (Cloudflare R2 via S3 API), pytest + moto. The single-file app is `app.py` (HTML/CSS/JS live in the `HTML` string constant); all boto3 access is confined to `r2.py`.

**Design spec:** `docs/superpowers/specs/2026-08-28-short-download-links-design.md`

---

## Background for the implementer

Read these before starting:

- `r2.py` — the entire R2 layer, ~75 lines. Every boto3 call in the project lives here; do not import boto3 anywhere else.
- `app.py:864-995` — the routes. `app.py:19-27` has global error handlers that turn *every* `HTTPException` into JSON, which is why the new route must return its 404 body directly rather than calling `abort(404)`.
- `app.py:290-860` — the `HTML` constant. The client JS is inside it, near the bottom.
- `tests/test_app.py:_seed_job` — helper that creates the local job directory the `/process` route expects.

Run the suite with `.venv/bin/python -m pytest -q` from the repo root. It should say `15 passed` before you start.

Two conventions this codebase follows, worth matching:

- Tests monkeypatch `r2` functions by name (`monkeypatch.setattr(r2, 'presign_get', ...)`) rather than mocking boto3. Follow that for route tests.
- `tests/test_r2.py` uses `moto`'s `@mock_aws` with `R2_ENDPOINT` blanked and `R2_REGION` set to `us-east-1`, because moto only intercepts the default AWS endpoint. Follow that for `r2` tests.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `r2.py` | Modify | Add `NotFound`, `put_text`, `get_text`. Stays the sole boto3 boundary. |
| `app.py` | Modify | Add `safe_download_name`, the `EXPIRED_HTML` constant, the `/d/<code>` route; change `/process` key layout and response; change two lines of client JS. |
| `tests/test_r2.py` | Modify | Cover the text sidecar helpers. |
| `tests/test_app.py` | Modify | Cover the redirect route; update the `/process` assertions. |

No new files. The app is deliberately single-file and `r2.py` is small and focused; splitting either would be unrelated refactoring.

---

### Task 1: Text sidecar helpers in `r2.py`

**Files:**
- Modify: `r2.py` (imports at top; new functions at end)
- Test: `tests/test_r2.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_r2.py`:

```python
@mock_aws
def test_put_text_get_text_roundtrip(monkeypatch):
    monkeypatch.setenv('R2_ENDPOINT', '')
    monkeypatch.setenv('R2_REGION', 'us-east-1')
    boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='test-bucket')

    r2.put_text('outputs/abcdef012345.name', 'clip-shorts.mp4')
    assert r2.get_text('outputs/abcdef012345.name') == 'clip-shorts.mp4'


@mock_aws
def test_get_text_raises_not_found_for_missing_key(monkeypatch):
    monkeypatch.setenv('R2_ENDPOINT', '')
    monkeypatch.setenv('R2_REGION', 'us-east-1')
    boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='test-bucket')

    with pytest.raises(r2.NotFound):
        r2.get_text('outputs/nosuchjob.name')


@mock_aws
def test_put_text_get_text_roundtrip_non_ascii(monkeypatch):
    monkeypatch.setenv('R2_ENDPOINT', '')
    monkeypatch.setenv('R2_REGION', 'us-east-1')
    boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='test-bucket')

    r2.put_text('outputs/abcdef012346.name', 'café-shorts.mp4')
    assert r2.get_text('outputs/abcdef012346.name') == 'café-shorts.mp4'
```

`pytest` is not yet imported in this file. Add it to the imports at the top of `tests/test_r2.py`:

```python
import boto3
import pytest
from moto import mock_aws

import r2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_r2.py -q`

Expected: 3 failures with `AttributeError: module 'r2' has no attribute 'put_text'` (and `NotFound`).

- [ ] **Step 3: Implement**

In `r2.py`, change the import block from:

```python
import boto3
from botocore.client import Config
```

to:

```python
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


class NotFound(Exception):
    """Object does not exist — typically reaped by the bucket lifecycle rule."""
```

Then append these two functions to the end of `r2.py`:

```python
def put_text(key, text):
    """Store a small UTF-8 text object (used for download-name sidecars)."""
    _client().put_object(
        Bucket=bucket(),
        Key=key,
        Body=text.encode('utf-8'),
        ContentType='text/plain; charset=utf-8',
    )


def get_text(key):
    """Read a small UTF-8 text object. Raises NotFound if the key is gone."""
    try:
        obj = _client().get_object(Bucket=bucket(), Key=key)
    except ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', 'NoSuchBucket', '404'):
            raise NotFound(key) from e
        raise
    return obj['Body'].read().decode('utf-8')
```

Note the typed `NotFound` exception. The route in Task 3 must distinguish "this job expired" from "R2 is misconfigured", and catching a bare `Exception` there would hide real failures — but importing `botocore` into `app.py` would break the rule that `r2.py` is the only boto3 consumer. A module-level exception satisfies both.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_r2.py -q`

Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add r2.py tests/test_r2.py
git commit -m "feat: add R2 text-object helpers for download-name sidecars"
```

---

### Task 2: `safe_download_name` in `app.py`

`presign_get` interpolates the filename into `attachment; filename="..."` (`r2.py:56`). A filename containing a double quote or a control character corrupts that header. This is a latent bug today; sanitize once, at the point where the name is created, so every later reader gets a safe value.

**Files:**
- Modify: `app.py` (new function near the other module-level helpers, after `JOB_ID_RE`/`ALLOWED_EXT` at `app.py:42-43`)
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py`:

```python
def test_safe_download_name_strips_quotes_and_control_chars():
    import app
    assert app.safe_download_name('my "clip"-shorts.mp4') == 'my clip-shorts.mp4'
    assert app.safe_download_name('clip\r\n-shorts.mp4') == 'clip-shorts.mp4'


def test_safe_download_name_strips_path_components():
    import app
    assert app.safe_download_name('../../etc/passwd-shorts.mp4') == 'passwd-shorts.mp4'
    assert app.safe_download_name('a\\b-shorts.mp4') == 'ab-shorts.mp4'


def test_safe_download_name_falls_back_when_empty():
    import app
    assert app.safe_download_name('""') == 'shorts-ready.mp4'
    assert app.safe_download_name('') == 'shorts-ready.mp4'


def test_safe_download_name_keeps_non_ascii():
    import app
    assert app.safe_download_name('café-shorts.mp4') == 'café-shorts.mp4'
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_app.py -q -k safe_download_name`

Expected: 4 failures with `AttributeError: module 'app' has no attribute 'safe_download_name'`.

- [ ] **Step 3: Implement**

In `app.py`, immediately after the `ALLOWED_EXT` line (`app.py:43`), add:

```python
# Characters that would corrupt a Content-Disposition header, plus the
# backslash path separator that Path().name does not strip on POSIX.
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f"\\]')


def safe_download_name(name):
    """Make a user-supplied filename safe to embed in Content-Disposition."""
    cleaned = _UNSAFE_NAME_CHARS.sub('', Path(name).name).strip()
    return cleaned or 'shorts-ready.mp4'
```

`re` and `Path` are already imported (`app.py:2`, `app.py:9`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_app.py -q -k safe_download_name`

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: sanitize download filenames for Content-Disposition"
```

---

### Task 3: The `/d/<code>` redirect route

**Files:**
- Modify: `app.py` (import line 11; new `EXPIRED_HTML` constant and route after the `index` route at `app.py:864-866`)
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py`:

```python
def test_download_redirect_302s_to_presigned_url(client, monkeypatch):
    import app, r2
    signed = {}
    monkeypatch.setattr(r2, 'get_text', lambda key: 'clip-shorts.mp4')
    def fake_presign(key, name, expires=86400):
        signed.update(key=key, name=name, expires=expires)
        return f'https://signed/{key}'
    monkeypatch.setattr(r2, 'presign_get', fake_presign)

    resp = client.get('/d/abcdef012345')
    assert resp.status_code == 302
    assert resp.headers['Location'] == 'https://signed/outputs/abcdef012345.mp4'
    assert signed['key'] == 'outputs/abcdef012345.mp4'
    assert signed['name'] == 'clip-shorts.mp4'
    assert signed['expires'] == 300


def test_download_redirect_reads_the_name_sidecar(client, monkeypatch):
    import app, r2
    read = {}
    def fake_get_text(key):
        read['key'] = key
        return 'clip-shorts.mp4'
    monkeypatch.setattr(r2, 'get_text', fake_get_text)
    monkeypatch.setattr(r2, 'presign_get', lambda key, name, expires=86400: 'https://signed/x')

    client.get('/d/abcdef012345')
    assert read['key'] == 'outputs/abcdef012345.name'


def test_download_redirect_is_not_cacheable(client, monkeypatch):
    import app, r2
    monkeypatch.setattr(r2, 'get_text', lambda key: 'clip-shorts.mp4')
    monkeypatch.setattr(r2, 'presign_get', lambda key, name, expires=86400: 'https://signed/x')

    resp = client.get('/d/abcdef012345')
    assert 'no-store' in resp.headers['Cache-Control']


def test_download_redirect_expired_returns_html_404(client, monkeypatch):
    import app, r2
    def raise_not_found(key):
        raise r2.NotFound(key)
    monkeypatch.setattr(r2, 'get_text', raise_not_found)

    resp = client.get('/d/abcdef012345')
    assert resp.status_code == 404
    assert resp.content_type.startswith('text/html')
    assert b'expired' in resp.data.lower()


def test_download_redirect_malformed_code_returns_html_404(client):
    resp = client.get('/d/NOT-A-JOB-ID')
    assert resp.status_code == 404
    assert resp.content_type.startswith('text/html')


def test_download_redirect_404s_are_indistinguishable(client, monkeypatch):
    """A bad code and an expired code must look identical, so the route
    never reveals whether a given job ever existed."""
    import app, r2
    def raise_not_found(key):
        raise r2.NotFound(key)
    monkeypatch.setattr(r2, 'get_text', raise_not_found)

    expired = client.get('/d/abcdef012345')
    malformed = client.get('/d/NOT-A-JOB-ID')
    assert expired.data == malformed.data
    assert expired.status_code == malformed.status_code


def test_download_redirect_propagates_unexpected_r2_errors(client, monkeypatch):
    """Misconfiguration must not be silently reported as 'expired'."""
    import app, r2
    def boom(key):
        raise RuntimeError('credentials are wrong')
    monkeypatch.setattr(r2, 'get_text', boom)

    resp = client.get('/d/abcdef012345')
    assert resp.status_code == 500
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_app.py -q -k download_redirect`

Expected: 7 failures. Most report `404` with a JSON body (the catch-all handler at `app.py:19`) instead of the expected redirect or HTML.

- [ ] **Step 3: Implement**

First change the Flask import at `app.py:11` from:

```python
from flask import Flask, request, jsonify
```

to:

```python
from flask import Flask, request, jsonify, redirect
```

Then, immediately after the `index` route (`app.py:864-866`), add:

```python
EXPIRED_HTML = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link expired</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #1a1a1a; color: #eee;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  .box { max-width: 22rem; padding: 2rem; text-align: center; }
  h1 { font-size: 1.25rem; margin: 0 0 0.75rem; }
  p { margin: 0; color: #999; line-height: 1.5; }
</style>
</head>
<body>
  <div class="box">
    <h1>This link has expired</h1>
    <p>Processed files are kept for 24 hours. Upload your video again to get a
       fresh download link.</p>
  </div>
</body>
</html>'''


def _expired_page():
    # Returned directly rather than via abort(404): the global HTTPException
    # handler renders JSON, which is wrong for a page a phone browser lands on.
    return EXPIRED_HTML, 404, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/d/<code>')
def download_redirect(code):
    """Short link for the QR code. Re-signs on every visit, so the QR itself
    carries no credential and never goes stale while the file exists."""
    if not JOB_ID_RE.match(code):
        return _expired_page()
    try:
        download_name = r2.get_text(f'outputs/{code}.name')
    except r2.NotFound:
        return _expired_page()
    url = r2.presign_get(f'outputs/{code}.mp4', download_name, expires=300)
    resp = redirect(url, code=302)
    # The target expires in 5 minutes; a cached 302 would outlive it.
    resp.headers['Cache-Control'] = 'no-store'
    return resp
```

The `Cache-Control: no-store` header is not in the design spec and is a necessary addition: the site sits behind Cloudflare, and a cached 302 would keep serving a presigned URL after its 300-second signature expired.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_app.py -q -k download_redirect`

Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: add /d/<code> short link that re-signs R2 downloads"
```

---

### Task 4: `/process` writes the new key layout and returns `download_path`

**Files:**
- Modify: `app.py:955-995` (the `process` route's output-name and R2-storage sections)
- Test: `tests/test_app.py:75-101` (rewrite the existing `/process` test)

- [ ] **Step 1: Update the existing test and add new ones**

Replace `test_process_uploads_result_and_returns_download_url` in `tests/test_app.py` entirely with:

```python
def test_process_uploads_result_and_returns_short_path(client, monkeypatch):
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
    uploaded, texts = {}, {}
    monkeypatch.setattr(r2, 'upload_file',
                        lambda path, key, content_type='application/octet-stream': uploaded.update(key=key))
    monkeypatch.setattr(r2, 'put_text', lambda key, text: texts.update({key: text}))

    resp = client.post('/process/abcdef012345', json={'mode': 'quick_fix'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['output_name'] == 'clip-shorts.mp4'
    assert uploaded['key'] == 'outputs/abcdef012345.mp4'
    assert texts == {'outputs/abcdef012345.name': 'clip-shorts.mp4'}
    assert body['download_path'] == '/d/abcdef012345'
    assert 'download_url' not in body
    assert not job_dir.exists()  # local temp cleaned up


def test_process_sanitizes_the_stored_download_name(client, monkeypatch):
    import app, r2
    _seed_job(app, job_id='abcdef012399', orig='my "weird" clip.mp4')

    def fake_run(cmd, capture_output=True, text=True):
        Path(cmd[-1]).write_bytes(b'\x00' * 20)
        class R:
            returncode = 0
            stderr = ''
        return R()
    monkeypatch.setattr(app.subprocess, 'run', fake_run)
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    fake_checks = {k: {'ok': True, 'value': 'x', 'expected': 'x'} for k in app.CHECK_LABELS}
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: fake_checks)
    texts = {}
    monkeypatch.setattr(r2, 'upload_file',
                        lambda path, key, content_type='application/octet-stream': None)
    monkeypatch.setattr(r2, 'put_text', lambda key, text: texts.update({key: text}))

    resp = client.post('/process/abcdef012399', json={'mode': 'quick_fix'})
    assert resp.status_code == 200
    assert texts['outputs/abcdef012399.name'] == 'my weird clip-shorts.mp4'


def test_process_returns_502_when_sidecar_write_fails(client, monkeypatch):
    import app, r2
    _seed_job(app, job_id='abcdef0123aa')

    def fake_run(cmd, capture_output=True, text=True):
        Path(cmd[-1]).write_bytes(b'\x00' * 20)
        class R:
            returncode = 0
            stderr = ''
        return R()
    monkeypatch.setattr(app.subprocess, 'run', fake_run)
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    fake_checks = {k: {'ok': True, 'value': 'x', 'expected': 'x'} for k in app.CHECK_LABELS}
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: fake_checks)
    monkeypatch.setattr(r2, 'upload_file',
                        lambda path, key, content_type='application/octet-stream': None)
    def boom(key, text):
        raise RuntimeError('r2 down')
    monkeypatch.setattr(r2, 'put_text', boom)

    resp = client.post('/process/abcdef0123aa', json={'mode': 'quick_fix'})
    assert resp.status_code == 502
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_app.py -q -k test_process`

Expected: 3 failures. The first reports `uploaded['key'] == 'outputs/abcdef012345/clip-shorts.mp4'` where `'outputs/abcdef012345.mp4'` was expected.

- [ ] **Step 3: Implement**

In the `process` route, replace this block (`app.py:955-961`):

```python
    name_file = job_dir / '.original_name'
    if name_file.exists():
        orig = Path(name_file.read_text().strip()).stem
        output_name = f'{orig}-shorts.mp4'
    else:
        output_name = 'shorts-ready.mp4'

    output_path = job_dir / output_name
```

with:

```python
    name_file = job_dir / '.original_name'
    if name_file.exists():
        orig = Path(name_file.read_text().strip()).stem
        output_name = safe_download_name(f'{orig}-shorts.mp4')
    else:
        output_name = 'shorts-ready.mp4'

    output_path = job_dir / output_name
```

Then replace this block (`app.py:974-981`):

```python
    # Store the result in R2 and hand back a presigned download URL.
    out_key = f'outputs/{job_id}/{output_name}'
    try:
        r2.upload_file(output_path, out_key, content_type='video/mp4')
    except Exception as e:
        return jsonify(error=f'Could not store result: {e}'), 502
    download_url = r2.presign_get(out_key, output_name)
```

with:

```python
    # Store the result in R2 under a key derivable from job_id alone, plus a
    # sidecar holding the display name. /d/<job_id> re-signs from those two.
    try:
        r2.upload_file(output_path, f'outputs/{job_id}.mp4', content_type='video/mp4')
        r2.put_text(f'outputs/{job_id}.name', output_name)
    except Exception as e:
        return jsonify(error=f'Could not store result: {e}'), 502
```

Finally, in the `jsonify(...)` return at the end of the route (`app.py:987-995`), replace the line:

```python
        download_url=download_url,
```

with:

```python
        download_path=f'/d/{job_id}',
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_app.py -q -k test_process`

Expected: `5 passed` (the 3 above plus the two pre-existing `test_process_invalid_mode_returns_400` and `test_process_ffmpeg_failure_returns_500`).

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_app.py
git commit -m "feat: store results under deterministic keys and return a short download path"
```

---

### Task 5: Client uses the short link

**Files:**
- Modify: `app.py:837-846` (inside the `HTML` string constant)

- [ ] **Step 1: Update the JS**

Replace this block (`app.py:837-846`):

```javascript
  // Download link (presigned R2 GET URL)
  const fullUrl = result.download_url;
  downloadBtn.href = fullUrl;
  downloadBtn.download = result.output_name;
  linkUrl.textContent = fullUrl;

  // QR code
  qrCode.innerHTML = '';
  new QRCode(qrCode, { text: fullUrl, width: 160, height: 160,
    colorDark: '#ffffff', colorLight: '#1a1a1a', correctLevel: QRCode.CorrectLevel.L });
```

with:

```javascript
  // Download link — short path on this origin; /d/<id> redirects to R2.
  const fullUrl = location.origin + result.download_path;
  downloadBtn.href = fullUrl;
  downloadBtn.download = result.output_name;
  linkUrl.textContent = fullUrl;

  // QR code. At ~45 chars this fits a version-4 symbol (33x33 modules) even at
  // the M error-correction level, versus 89x89 for the old presigned URL.
  qrCode.innerHTML = '';
  new QRCode(qrCode, { text: fullUrl, width: 160, height: 160,
    colorDark: '#ffffff', colorLight: '#1a1a1a', correctLevel: QRCode.CorrectLevel.M });
```

- [ ] **Step 2: Verify the served page contains the new code**

Run:

```bash
.venv/bin/python -c "
import app
h = app.HTML
assert 'result.download_path' in h, 'download_path missing'
assert 'result.download_url' not in h, 'stale download_url reference'
assert 'CorrectLevel.M' in h, 'QR error correction not raised'
assert 'CorrectLevel.L' not in h, 'stale CorrectLevel.L'
print('client JS OK')
"
```

Expected: `client JS OK`.

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "feat: point the QR code at the short link and raise QR error correction"
```

---

### Task 6: Full-suite check and manual verification

**Files:** none modified unless a failure turns one up.

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`

Expected: `31 passed`.

The arithmetic, so you can spot a silently-skipped test: 15 before this plan,
plus 3 from Task 1, 4 from Task 2, 7 from Task 3, and 2 from Task 4 (which
rewrites one existing test in place and adds two). If the count differs,
reconcile it before continuing rather than assuming it is fine.

- [ ] **Step 2: Confirm the URL is actually short**

Run:

```bash
.venv/bin/python -c "
url = 'https://shorts.kautiontape.com/d/a1b2c3d4e5f6'
print(len(url), url)
assert len(url) < 50
"
```

Expected: `45 https://shorts.kautiontape.com/d/a1b2c3d4e5f6`.

- [ ] **Step 3: Exercise the redirect against a local server**

```bash
.venv/bin/python app.py &
sleep 2
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/d/NOT-A-JOB-ID
curl -s http://127.0.0.1:8080/d/000000000000 | grep -o 'This link has expired'
kill %1
```

Expected: `404`, then `This link has expired`. (`000000000000` matches `JOB_ID_RE` but has no sidecar in R2, so it exercises the `NotFound` path against the real bucket.)

- [ ] **Step 4: End-to-end on the deployed site**

This step needs a real deploy and cannot be verified locally. On `https://shorts.kautiontape.com`, upload a `.mov`/`.mp4`, let it process, then confirm:

1. The link shown under the QR reads `https://shorts.kautiontape.com/d/<12 hex>`, not an `r2.cloudflarestorage.com` URL.
2. The QR is visibly coarser than before — roughly 33 blocks across rather than 89.
3. Scanning it with a phone camera downloads the file with the original name plus `-shorts.mp4`.
4. Scanning the same QR again a few minutes later still works (this is what proves the re-signing works and the 302 is not being cached by Cloudflare).

Do not mark the plan complete until step 4 has actually been run. Report the result rather than inferring it.

- [ ] **Step 5: Commit any fixes**

Only if steps 1-4 turned up problems.

```bash
git add -A
git commit -m "fix: <what was actually wrong>"
```

---

## Notes for the implementer

- **Do not add a database, a base62 encoder, or a second domain.** All three were considered and rejected in the design spec. `job_id` is the short code.
- **Do not use `abort(404)` in the `/d/` route.** The global handler at `app.py:19` would turn it into JSON, and this route is loaded directly by phone browsers.
- **Keep both 404 bodies byte-identical.** `test_download_redirect_404s_are_indistinguishable` enforces this. Do not "improve" the malformed-code case with a distinct message.
- **Do not broaden the `except r2.NotFound` to `except Exception`.** `test_download_redirect_propagates_unexpected_r2_errors` enforces this; a bare catch would report a credentials failure as an expired link.
- **`setup_r2.py` needs no change.** Its lifecycle rule uses `Prefix: ''`, so it already covers both `outputs/<id>.mp4` and `outputs/<id>.name`, which are written together and therefore expire together.
- **Links created before this deploy will break**, since the key layout changes. This is expected — jobs live at most ~48 hours under the current 1-day lifecycle rule.
