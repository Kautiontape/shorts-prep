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
from botocore.exceptions import ClientError


class NotFound(Exception):
    """Object does not exist — typically reaped by the bucket lifecycle rule."""


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
        # Only a genuinely absent object is NotFound. Anything else --
        # a missing bucket, bad credentials -- must propagate, so
        # misconfiguration surfaces as a 500 instead of a "link expired" page.
        if e.response['Error']['Code'] == 'NoSuchKey':
            raise NotFound(key) from e
        raise
    return obj['Body'].read().decode('utf-8')
