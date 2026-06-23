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
