from pathlib import Path


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


def test_unknown_route_returns_json_not_html(client):
    resp = client.get('/no-such-route')
    assert resp.status_code == 404
    assert resp.get_json() is not None
    assert 'error' in resp.get_json()


def test_analyze_downloads_probes_and_keeps_input(client, monkeypatch):
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
    # The R2 input is retained so the job can be re-processed from history.
    assert deleted == {}


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


def _seed_job(app, job_id='abcdef012345', orig='clip.mp4'):
    job_dir = app.UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / 'input.mp4').write_bytes(b'\x00' * 10)
    (job_dir / '.original_name').write_text(orig)
    return job_dir


class _InlineThread:
    """Stand-in for threading.Thread that runs the target synchronously on
    start(), so the async /process flow is deterministic under test."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def test_process_uploads_result_and_status_returns_short_path(client, monkeypatch):
    import app, r2
    job_dir = _seed_job(app)
    monkeypatch.setattr(app.threading, 'Thread', _InlineThread)

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

    # /process returns immediately with 202; the job runs inline via _InlineThread.
    resp = client.post('/process/abcdef012345', json={'mode': 'quick_fix'})
    assert resp.status_code == 202
    assert resp.get_json()['state'] == 'processing'

    # /status now reports the finished result.
    sresp = client.get('/status/abcdef012345')
    assert sresp.status_code == 200
    body = sresp.get_json()
    assert body['state'] == 'done'
    result = body['result']
    assert result['output_name'] == 'clip-shorts.mp4'
    assert uploaded['key'] == 'outputs/abcdef012345.mp4'
    assert texts == {'outputs/abcdef012345.name': 'clip-shorts.mp4'}
    assert result['download_path'] == '/d/abcdef012345'
    assert 'download_url' not in result
    # Large media pruned, but status.json is kept so the poll can read it.
    assert (job_dir / 'status.json').exists()
    assert not list(job_dir.glob('input.*'))


def test_process_returns_202_before_job_finishes(client, monkeypatch):
    import app
    _seed_job(app, job_id='abcdef555555')

    # Default real threading.Thread, but make the work block so the job is still
    # 'processing' when /process returns — proving we don't hold the request.
    gate = {'released': False}

    def slow_run(cmd, capture_output=True, text=True):
        while not gate['released']:
            pass
        class R:
            returncode = 0
            stderr = ''
        return R()
    monkeypatch.setattr(app.subprocess, 'run', slow_run)
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: {})

    resp = client.post('/process/abcdef555555', json={'mode': 're_encode'})
    assert resp.status_code == 202

    sresp = client.get('/status/abcdef555555')
    assert sresp.get_json()['state'] == 'processing'
    gate['released'] = True  # let the background thread finish and exit cleanly


def test_process_sanitizes_the_stored_download_name(client, monkeypatch):
    import app, r2
    _seed_job(app, job_id='abcdef012399', orig='my "weird" clip.mp4')
    monkeypatch.setattr(app.threading, 'Thread', _InlineThread)

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
    assert resp.status_code == 202
    assert texts == {'outputs/abcdef012399.name': 'my weird clip-shorts.mp4'}


def test_process_records_an_error_when_the_sidecar_write_fails(client, monkeypatch):
    """The upload half can succeed while the name sidecar fails, leaving an
    output /d/<code> could never resolve. The job must report that, not 'done'."""
    import app, r2
    _seed_job(app, job_id='abcdef0123aa')
    monkeypatch.setattr(app.threading, 'Thread', _InlineThread)

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
    assert resp.status_code == 202

    body = client.get('/status/abcdef0123aa').get_json()
    assert body['state'] == 'error'
    assert 'Could not store result' in body['error']


def test_process_then_download_redirect_round_trip(client, monkeypatch):
    """The two halves of the feature must agree on the R2 key strings.

    Every other test mocks one side or the other, so a divergence between what
    /process writes and what /d/<code> reads would pass the whole suite while
    shipping a feature that never works. This drives both routes against one
    shared fake bucket.
    """
    import app, r2
    store = {}

    def fake_upload(path, key, content_type='application/octet-stream'):
        store[key] = Path(path).read_bytes()
    def fake_put_text(key, text):
        store[key] = text
    def fake_get_text(key):
        if key not in store:
            raise r2.NotFound(key)
        return store[key]
    def fake_presign(key, name, expires=86400):
        if key not in store:
            raise AssertionError(f'presigned a key that was never written: {key}')
        return f'https://signed.example/{key}?name={name}'

    monkeypatch.setattr(r2, 'upload_file', fake_upload)
    monkeypatch.setattr(r2, 'put_text', fake_put_text)
    monkeypatch.setattr(r2, 'get_text', fake_get_text)
    monkeypatch.setattr(r2, 'presign_get', fake_presign)

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

    _seed_job(app, job_id='abcdef0123ee', orig='holiday.mp4')
    monkeypatch.setattr(app.threading, 'Thread', _InlineThread)
    resp = client.post('/process/abcdef0123ee', json={'mode': 'quick_fix'})
    assert resp.status_code == 202
    download_path = client.get('/status/abcdef0123ee').get_json()['result']['download_path']

    # Follow the very path the client will put in the QR code.
    follow = client.get(download_path)
    assert follow.status_code == 302
    assert follow.headers['Location'] == (
        'https://signed.example/outputs/abcdef0123ee.mp4?name=holiday-shorts.mp4'
    )


def test_download_redirect_after_lifecycle_reaps_the_job(client, monkeypatch):
    """Once the bucket lifecycle deletes the objects, the same link 404s."""
    import app, r2
    def raise_not_found(key):
        raise r2.NotFound(key)
    monkeypatch.setattr(r2, 'get_text', raise_not_found)

    resp = client.get('/d/abcdef0123ee')
    assert resp.status_code == 404
    assert b'expired' in resp.data.lower()


def test_process_invalid_mode_returns_400(client):
    import app
    _seed_job(app, job_id='abcdef999999')
    resp = client.post('/process/abcdef999999', json={'mode': 'nope'})
    assert resp.status_code == 400


def test_process_missing_job_returns_404(client):
    resp = client.post('/process/abcdef111111', json={'mode': 'quick_fix'})
    assert resp.status_code == 404


def test_process_ffmpeg_failure_sets_error_status(client, monkeypatch):
    import app
    _seed_job(app, job_id='abcdef000bad')
    monkeypatch.setattr(app.threading, 'Thread', _InlineThread)
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: {})
    def fail_run(cmd, capture_output=True, text=True):
        class R:
            returncode = 1
            stderr = 'boom'
        return R()
    monkeypatch.setattr(app.subprocess, 'run', fail_run)

    resp = client.post('/process/abcdef000bad', json={'mode': 'quick_fix'})
    assert resp.status_code == 202

    body = client.get('/status/abcdef000bad').get_json()
    assert body['state'] == 'error'
    assert 'ffmpeg failed' in body['error']


def test_status_unknown_job_returns_404(client):
    resp = client.get('/status/abcdef222222')
    assert resp.status_code == 404
    assert resp.get_json() is not None


def test_status_rejects_bad_job_id(client):
    resp = client.get('/status/BAD')
    assert resp.status_code == 400


def test_download_route_removed(client):
    resp = client.get('/download/abcdef012345')
    assert resp.status_code == 404
    assert resp.get_json() is not None  # JSON 404, not HTML


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


def test_download_redirect_sanitizes_the_sidecar_name(client, monkeypatch):
    """The sidecar is data read back from R2. Even though Task 4 sanitizes on
    write, the read side must not trust it to build a Content-Disposition."""
    import app, r2
    signed = {}
    monkeypatch.setattr(r2, 'get_text', lambda key: 'ev"il\r\n-shorts.mp4')
    def fake_presign(key, name, expires=86400):
        signed['name'] = name
        return 'https://signed/x'
    monkeypatch.setattr(r2, 'presign_get', fake_presign)

    client.get('/d/abcdef012345')
    assert signed['name'] == 'evil-shorts.mp4'


def test_download_redirect_falls_back_when_sidecar_is_empty(client, monkeypatch):
    import app, r2
    signed = {}
    monkeypatch.setattr(r2, 'get_text', lambda key: '   ')
    def fake_presign(key, name, expires=86400):
        signed['name'] = name
        return 'https://signed/x'
    monkeypatch.setattr(r2, 'presign_get', fake_presign)

    client.get('/d/abcdef012345')
    assert signed['name'] == 'shorts-ready.mp4'


def test_index_html_uses_the_short_download_path():
    """The client must read the field /process actually returns. These two
    drifting apart is invisible server-side: every route test would still
    pass while the result page silently rendered 'undefined' in the QR."""
    import app
    assert 'result.download_path' in app.HTML
    assert 'result.download_url' not in app.HTML


def test_index_html_raises_qr_error_correction():
    """The old 491-char URL needed level L just to fit. The short link has
    headroom for M, which scans far more reliably off a screen."""
    import app
    assert 'CorrectLevel.M' in app.HTML
    assert 'CorrectLevel.L' not in app.HTML


def test_internal_errors_are_logged_server_side(client, monkeypatch, caplog):
    """Registering an errorhandler for Exception stops Flask logging the
    traceback itself, so the handler must log it. Without this the /d/ route's
    deliberate 'misconfiguration surfaces as a 500' behavior is invisible in
    production -- the operator sees a generic 500 and no cause.
    """
    import app, r2
    def boom(key):
        raise RuntimeError('diagnostic-marker-xyz')
    monkeypatch.setattr(r2, 'get_text', boom)

    with caplog.at_level('ERROR'):
        resp = client.get('/d/abcdef012345')

    assert resp.status_code == 500
    assert resp.get_json()['error'] == 'Internal server error'  # still no leak
    assert 'diagnostic-marker-xyz' in caplog.text


# QR byte-mode capacity at error-correction level M. Version 4 (33x33 modules)
# holds 62 bytes; version 5 jumps to 37x37 and starts shrinking each module
# below the ~4.8px that makes the code scannable on the 160px canvas.
QR_V4_M_CAPACITY = 62

SHORT_LINK_ORIGIN = 'https://shorts.kautiontape.com'


def test_short_link_stays_within_one_qr_version():
    """The whole feature exists to keep the QR scannable. Nothing else pins
    the length that justification rests on, so a longer domain, an extra query
    parameter, or a wider job id could silently push the code to a denser
    version and only be noticed when a phone failed to scan it.
    """
    import app
    job_id = 'a' * 12  # job_id is uuid4().hex[:12]
    assert app.JOB_ID_RE.match(job_id)  # the path shape really is this wide
    url = f'{SHORT_LINK_ORIGIN}/d/{job_id}'
    assert len(url.encode()) <= QR_V4_M_CAPACITY, (
        f'{len(url)}-byte link exceeds QR v4-M capacity; the QR would get denser'
    )
