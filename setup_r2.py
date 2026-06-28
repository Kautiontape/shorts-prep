"""One-time Cloudflare R2 bucket setup: CORS (for browser PUTs) + lifecycle
(auto-expire job objects). Run locally with the R2_* env vars set:

    R2_ENDPOINT=... R2_BUCKET=... R2_ACCESS_KEY=... R2_SECRET_KEY=... \
        python setup_r2.py
"""
import r2

CORS = {
    'CORSRules': [{
        'AllowedOrigins': ['https://shorts.kautiontape.com', 'http://localhost:8080'],
        'AllowedMethods': ['PUT'],
        'AllowedHeaders': ['*'],
        'ExposeHeaders': ['ETag'],
        'MaxAgeSeconds': 3600,
    }]
}

# Sources (inputs/) are kept long enough that a past job can be re-processed
# from the browser's history without re-uploading; outputs/ are kept the same so
# a saved download link keeps working. Bump these if you want longer history (at
# the cost of more R2 storage).
LIFECYCLE = {
    'Rules': [
        {
            'ID': 'expire-inputs',
            'Status': 'Enabled',
            'Filter': {'Prefix': 'inputs/'},
            'Expiration': {'Days': 30},
        },
        {
            'ID': 'expire-outputs',
            'Status': 'Enabled',
            'Filter': {'Prefix': 'outputs/'},
            'Expiration': {'Days': 30},
        },
    ]
}


def main():
    client = r2._client()
    name = r2.bucket()
    client.put_bucket_cors(Bucket=name, CORSConfiguration=CORS)
    client.put_bucket_lifecycle_configuration(Bucket=name, LifecycleConfiguration=LIFECYCLE)
    print(f'Applied CORS + lifecycle to {name}')


if __name__ == '__main__':
    main()
