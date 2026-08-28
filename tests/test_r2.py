import boto3
import pytest
from botocore.exceptions import ClientError
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


@mock_aws
def test_get_text_propagates_missing_bucket_rather_than_not_found(monkeypatch):
    """A missing bucket is a misconfiguration, not an expired object. It must
    not be reported as NotFound, or every download link would render as
    'expired' when the deploy is simply pointing at the wrong bucket."""
    monkeypatch.setenv('R2_ENDPOINT', '')
    monkeypatch.setenv('R2_REGION', 'us-east-1')
    # Deliberately do NOT create the bucket.

    with pytest.raises(ClientError):
        r2.get_text('outputs/anything.name')
