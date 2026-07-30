import deadpool


def test_package_preserves_local_api():
    assert isinstance(deadpool.__version__, str)
    assert deadpool.Deadpool.__module__ == "deadpool._pool"
    assert deadpool.PrioritizedItem is deadpool._pool.PrioritizedItem
    assert callable(deadpool.trim_memory)


def test_remote_api_is_importable():
    from deadpool.remote import DeadpoolClient, DeadpoolServer, RemoteFuture

    assert DeadpoolClient
    assert DeadpoolServer
    assert RemoteFuture
