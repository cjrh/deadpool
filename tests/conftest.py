import logging

import pytest

import deadpool

FORMAT = (
    "%(levelname)10s %(relativeCreated)10d %(threadName)s "
    "%(message)s [%(filename)s:%(lineno)d]"
)
logging.basicConfig(level="DEBUG", format=FORMAT)


@pytest.fixture(scope="session", autouse=True)
def _prime_forkserver():
    """Start the forkserver once, up front, with a pristine environment.

    The ``forkserver`` is a process-wide singleton: it snapshots
    ``os.environ`` when it first starts, and every worker is forked from
    it. Some tests (notably ``test_env_fails``) assert that a variable set
    on the *parent* after pool creation does NOT reach workers. That is
    only true if the forkserver was already running before the variable
    was set; if a test is the very first to start the forkserver *after*
    setting the variable, the forkserver snapshots it and the assertion
    fails.

    Priming the forkserver here, before any test has had a chance to
    mutate ``os.environ``, makes those tests independent of collection
    order (e.g. running ``pytest -k env`` in isolation). Constructing a
    throwaway pool is enough to start the forkserver via Deadpool's normal
    machinery, without reaching into multiprocessing internals.
    """
    with deadpool.Deadpool(max_workers=1):
        pass
