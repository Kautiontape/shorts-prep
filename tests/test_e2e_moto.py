"""End-to-end through the REAL r2 module, with moto standing in for R2.

The committed round-trip test replaces every r2 function with a dict-backed
fake, so it proves the two routes agree on key strings but exercises none of
the actual boto3 code: real key handling, real presigning, and the real
ClientError -> NotFound translation all go untested together.
"""
from pathlib import Path

import boto3
import pytest
from moto import mock_aws


@mock_aws
def test_process_then_download_through_real_r2(client, monkeypatch, tmp_path):
    import app, r2
    monkeypatch.setenv('R2_ENDPOINT', '')
    monkeypatch.setenv('R2_REGION', 'us-east-1')
    control = boto3.client('s3', region_name='us-east-1')
    control.create_bucket(Bucket='test-bucket')

    def fake_run(cmd, capture_output=True, text=True):
        Path(cmd[-1]).write_bytes(b'\x00' * 20)
        class R:
            returncode = 0
            stderr = ''
        return R()
    monkeypatch.setattr(app.subprocess, 'run', fake_run)
    monkeypatch.setattr(app, 'probe_detailed', lambda p: {'streams': [], 'format': {}})
    checks = {k: {'ok': True, 'value': 'x', 'expected': 'x'} for k in app.CHECK_LABELS}
    monkeypatch.setattr(app, 'run_checks', lambda probe, p: checks)

    job = 'abcdef0123ff'
    d = app.UPLOAD_DIR / job
    d.mkdir(parents=True, exist_ok=True)
    (d / 'input.mp4').write_bytes(b'\x00' * 10)
    (d / '.original_name').write_text('holiday.mp4')

    resp = client.post(f'/process/{job}', json={'mode': 'quick_fix'})
    assert resp.status_code == 200, resp.get_data()
    path = resp.get_json()['download_path']
    assert path == f'/d/{job}'

    # Real objects really landed under the deterministic keys.
    keys = {o['Key'] for o in control.list_objects_v2(Bucket='test-bucket').get('Contents', [])}
    assert keys == {f'outputs/{job}.mp4', f'outputs/{job}.name'}, keys

    # Follow the link exactly as the QR code would.
    follow = client.get(path)
    assert follow.status_code == 302
    loc = follow.headers['Location']
    assert f'outputs/{job}.mp4' in loc
    assert 'X-Amz-Signature' in loc
    assert 'holiday-shorts.mp4' in loc  # Content-Disposition name survived
    assert follow.headers['Cache-Control'] == 'no-store'

    # After the lifecycle reaps the objects, the same link shows the HTML page.
    control.delete_object(Bucket='test-bucket', Key=f'outputs/{job}.name')
    gone = client.get(path)
    assert gone.status_code == 404
    assert gone.content_type.startswith('text/html')
    assert b'expired' in gone.data.lower()
