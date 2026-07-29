import os
import time

import pytest

import deadpool
from deadpool.remote import RemoteForkedProcessError


def delayed(delay):
    time.sleep(delay)
    return delay


def test_execution_timeout_retires_worker_and_pool_continues(remote_pair):
    _, client = remote_pair
    with pytest.raises(deadpool.TimeoutError):
        client.submit(delayed, 0.3, deadpool_timeout=0.05).result(timeout=5)
    assert client.submit(delayed, 0.01).result(timeout=5) == 0.01


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_inherited_future_is_invalid_in_child(remote_pair):
    _, client = remote_pair
    future = client.submit(delayed, 0.15)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            future.done()
        except RemoteForkedProcessError:
            result = client.submit(delayed, 0.01).result(timeout=5)
            os.write(write_fd, f"invalid:{result}".encode())
        else:
            os.write(write_fd, b"shared")
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 16)
    os.close(read_fd)
    os.waitpid(pid, 0)
    assert result == b"invalid:0.01"
    assert future.result(timeout=5) == 0.15
