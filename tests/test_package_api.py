import deadpool


def test_package_preserves_local_api():
    assert deadpool.__version__ == "2026.6.1"
    assert deadpool.Deadpool.__module__ == "deadpool._pool"
    assert deadpool.PrioritizedItem is deadpool._pool.PrioritizedItem
    assert callable(deadpool.trim_memory)


def test_remote_api_is_importable():
    from deadpool.remote import DeadpoolClient, DeadpoolServer, RemoteFuture

    assert DeadpoolClient
    assert DeadpoolServer
    assert RemoteFuture
