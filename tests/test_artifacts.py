from artifacts import ArtifactStore


def test_artifact_is_retrievable_once_after_success():
    store = ArtifactStore(max_bytes=10, max_count=2, ttl=5)
    key = store.put({"data": b"abc", "filename": "a.txt", "mime_type": "text/plain"})
    assert store.start(key)["data"] == b"abc"
    assert store.start(key) is None
    store.complete(key)
    assert store.start(key) is None


def test_interrupted_artifact_can_be_released_and_retried():
    store = ArtifactStore(max_bytes=10, max_count=2, ttl=5)
    key = store.put({"data": b"abc", "filename": "a.txt", "mime_type": "text/plain"})
    assert store.start(key) is not None
    store.release(key)
    assert store.start(key) is not None


def test_expiry_removes_artifact_and_frees_capacity():
    now = [0.0]
    store = ArtifactStore(max_bytes=3, max_count=1, ttl=5, clock=lambda: now[0])
    key = store.put({"data": b"abc", "filename": "a.txt", "mime_type": "text/plain"})
    now[0] = 6
    store.purge_expired()
    assert store.start(key) is None
    assert store.put({"data": b"xyz", "filename": "b.txt", "mime_type": "text/plain"})
