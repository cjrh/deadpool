.. image:: https://github.com/cjrh/deadpool/workflows/Python%20application/badge.svg
    :target: https://github.com/cjrh/deadpool/actions

.. image:: https://coveralls.io/repos/github/cjrh/deadpool/badge.svg?branch=main
    :target: https://coveralls.io/github/cjrh/deadpool?branch=main

.. image:: https://img.shields.io/pypi/pyversions/deadpool-executor.svg
    :target: https://pypi.python.org/pypi/deadpool-executor

.. image:: https://img.shields.io/github/tag/cjrh/deadpool.svg
    :target: https://img.shields.io/github/tag/cjrh/deadpool.svg

.. image:: https://img.shields.io/badge/install-pip%20install%20deadpool_executor-ff69b4.svg
    :target: https://img.shields.io/badge/install-pip%20install%20deadpool_executor-ff69b4.svg

.. image:: https://img.shields.io/pypi/v/deadpool-executor.svg
    :target: https://pypi.org/project/deadpool-executor/

.. image:: https://img.shields.io/badge/calver-YYYY.MM.MINOR-22bfda.svg
    :alt: This project uses calendar-based versioning scheme
    :target: http://calver.org/

.. image:: https://pepy.tech/badge/deadpool-executor
    :alt: Downloads
    :target: https://pepy.tech/project/deadpool-executor

.. image:: https://img.shields.io/badge/code%20style-black-000000.svg
    :alt: This project uses the "black" style formatter for Python code
    :target: https://github.com/python/black


deadpool
========

``Deadpool`` is a process pool that is really hard to kill.

.. warning ::

   The python package name is ``deadpool-executor``, so to install
   you must type ``$ pip install deadpool-executor``. The *import*
   name is ``deadpool``, so in your Python code you must type
   ``import deadpool`` to use it.

``Deadpool`` is an implementation of the ``Executor`` interface
in the ``concurrent.futures`` standard library. ``Deadpool`` is
a process pool executor, quite similar to the stdlib's
``ProcessPoolExecutor``.

The discussion below assumes that you are familiar with the stdlib
``ProcessPoolExecutor``. If you are not, it is important
to understand that ``Deadpool`` makes very specific tradeoffs that
can result in quite different behaviour to the stdlib
implementation.

Why would I want to use this?
-----------------------------

I created ``Deadpool`` because I became frustrated with the
stdlib ``ProcessPoolExecutor``, and various other community
implementations of process pools. In particular, I had a use-case
that required a high server uptime, but also had variable and
unpredictable memory requirements such that certain tasks could
trigger the OOM killer, often resulting in a "broken" process
pool. I also needed task-specific timeouts *that will kill a hung
task*, which the stdlib executor doesn't provide.

I also tried using the `Pebble <https://github.com/noxdafox/pebble>`_
community process pool, but during testing I found several
occurrences of a mysterious ``RuntimeError`` that caused the Pebble
pool to become broken and no longer accept new tasks.

My goal with ``Deadpool`` is to make it impossible to break. The
tradeoffs are that I care less about:
- being cross-platform
- optimizing per-task latency

What differs from ``ProcessPoolExecutor``?
------------------------------------------

``Deadpool`` is generally very similar to ``ProcessPoolExecutor``
since it executes tasks in subprocesses, and implements the
standard ``Executor``
abstract interface. However, it differs in the following
ways:
- ``Deadpool`` makes a new subprocess for every task submitted to
the pool. It is like having ``max_tasks_per_child == 1``.
- ``Deadpool`` does not keep a pool of processes around indefinitely.
There will only be as many concurrent processes running as there
is work to be done, up to the limit set by the ``max_workers``
parameter; but if there are fewer tasks to be executed, there will
be fewer active subprocesses. When there are no pending or active
tasks, there will be *no subprocesses present*. They are created
on demand as necessary and disappear when not required.
- ``Deadpool`` tasks have timeouts. When a task hits the timeout,
the underlying subprocess in the pool is killed with ``SIGKILL``.
- If a ``Deadpool`` subprocess in the pool is killed by some
external actor, for example, the OS runs out of memory and the
OOM killer kills a pool subprocess that is using a lot of memory,
``Deadpool`` does not care and further operation is unaffected.
``Deadpool`` *cannot raise ``BrokenProcessPool`` or
``BrokenExecutor``*.
- ``Deadpool`` also allows a ``finalizer``, with corresponding
``finalargs``, that will be called after a task is executed on
a subprocess, but before the subprocess terminates. It is
analogous to the ``initializer`` and ``initargs`` parameters.
Just like the ``initializer`` callable, the ``finalizer``
callable is executed inside the subprocess.
- ``Deadpool`` currently only works on Linux.
- ``Deadpool`` defaults to the ``forkserver`` multiprocessing
context, unlike the stdlib pool which defaults to ``fork`` on
Linux.

Show me some code
-----------------

Simple case
^^^^^^^^^^^

The simple case works exactly the same:

.. code-block:: python

    from deadpool import Deadpool

    def f():
        return 123

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f)
        result = fut.result()

    assert result == 123

Timeouts
^^^^^^^^

.. code-block:: python

    import time
    import deadpool

    def f():
        time.sleep(10.0)

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f, timeout=1.0)

        with pytest.raises(deadpool.TimeoutError)
            fut.result()

Handling OOM killed situations
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    import time
    import deadpool

    def f():
        x = list(range(10**100))

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f, timeout=1.0)

        with pytest.raises(deadpool.ProcessError)
            fut.result()

As long as the OOM killer removes the subprocess (and not the main process),
this will not hurt the pool, and it will be able to receive more tasks.