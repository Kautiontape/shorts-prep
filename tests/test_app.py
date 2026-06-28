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


def test_process_runs_job_and_status_returns_download_url(client, monkeypatch):
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
    uploaded = {}
    monkeypatch.setattr(r2, 'upload_file',
                        lambda path, key, content_type='application/octet-stream': uploaded.update(key=key))
    monkeypatch.setattr(r2, 'presign_get', lambda key, name, expires=86400: f'https://signed/{key}')

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
    assert uploaded['key'] == 'outputs/abcdef012345/clip-shorts.mp4'
    assert result['download_url'] == 'https://signed/outputs/abcdef012345/clip-shorts.mp4'
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
