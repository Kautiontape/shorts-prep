def test_app_imports(client):
    # If app imports and a test client builds, the module is healthy.
    resp = client.get('/')
    assert resp.status_code == 200
