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
    return boto3.client(
        's3',
        endpoint_url=s['endpoint'] or None,
        aws_access_key_id=s['access_key'],
        aws_secret_access_key=s['secret_key'],
        region_name=s['region'],
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
    )


def bucket():
    return _settings()['bucket']


def presign_put(key, expires=900):
    """Presigned PUT URL (15 min). Content-Type intentionally unsigned."""
    return _client().generate_presigned_url(
        'put_object',
        Params={'Bucket': bucket(), 'Key': key},
        ExpiresIn=expires,
    )


def presign_get(key, download_name, expires=604800):
    """Presigned GET URL (7 days, the SigV4 max) that downloads as
    `download_name`. The long expiry keeps saved history links usable."""
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
