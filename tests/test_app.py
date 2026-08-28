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
    assert texts == {'outputs/abcdef012399.name': 'my weird clip-shorts.mp4'}


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
    resp = client.post('/process/abcdef0123ee', json={'mode': 'quick_fix'})
    assert resp.status_code == 200
    download_path = resp.get_json()['download_path']

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


def test_process_ffmpeg_failure_returns_500(client, monkeypatch):
    import app
    _seed_job(app, job_id='abcdef000bad')
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: {})
    def fail_run(cmd, capture_output=True, text=True):
        class R:
            returncode = 1
            stderr = 'boom'
        return R()
    monkeypatch.setattr(app.subprocess, 'run', fail_run)
    resp = client.post('/process/abcdef000bad', json={'mode': 'quick_fix'})
    assert resp.status_code == 500


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
