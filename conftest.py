import os

# Dummy R2 settings so importing app/r2 never needs real credentials.
os.environ.setdefault('R2_ENDPOINT', 'https://acct.r2.cloudflarestorage.com')
os.environ.setdefault('R2_BUCKET', 'test-bucket')
os.environ.setdefault('R2_ACCESS_KEY', 'test-access')
os.environ.setdefault('R2_SECRET_KEY', 'test-secret')

import pytest


@pytest.fixture
def client():
    import app
    app.app.config['TESTING'] = True
    return app.app.test_client()
