import boto3
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
