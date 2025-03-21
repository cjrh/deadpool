.. |ci| image:: https://github.com/cjrh/deadpool/workflows/Python%20application/badge.svg
    :target: https://github.com/cjrh/deadpool/actions

.. |coverage| image:: https://coveralls.io/repos/github/cjrh/deadpool/badge.svg?branch=main
    :target: https://coveralls.io/github/cjrh/deadpool?branch=main

.. |pyversions| image:: https://img.shields.io/pypi/pyversions/deadpool-executor.svg
    :target: https://pypi.python.org/pypi/deadpool-executor

.. |tag| image:: https://img.shields.io/github/tag/cjrh/deadpool.svg
    :target: https://img.shields.io/github/tag/cjrh/deadpool.svg

.. |install| image:: https://img.shields.io/badge/install-pip%20install%20deadpool--executor-ff69b4.svg
    :target: https://img.shields.io/badge/install-pip%20install%20deadpool--executor-ff69b4.svg

.. |pypi| image:: https://img.shields.io/pypi/v/deadpool-executor.svg
    :target: https://pypi.org/project/deadpool-executor/

.. |calver| image:: https://img.shields.io/badge/calver-YYYY.MM.MINOR-22bfda.svg
    :alt: This project uses calendar-based versioning scheme
    :target: http://calver.org/

.. |pepy| image:: https://pepy.tech/badge/deadpool-executor
    :alt: Downloads
    :target: https://pepy.tech/project/deadpool-executor

.. |black| image:: https://img.shields.io/badge/code%20style-black-000000.svg
    :alt: This project uses the "black" style formatter for Python code
    :target: https://github.com/python/black

.. |openssf| image:: https://api.securityscorecards.dev/projects/github.com/cjrh/deadpool/badge
    :alt: OpenSSF Scorecard
    :target: https://api.securityscorecards.dev/projects/github.com/cjrh/deadpool

|ci| |coverage| |pyversions| |tag| |install| |pypi| |calver| |pepy| |black| |openssf|

.. sectnum::

.. contents::
   :local:
   :depth: 2
   :backlinks: entry

Deadpool
========

``Deadpool`` is a process pool that is really hard to kill.

``Deadpool`` is an implementation of the ``Executor`` interface
in the ``concurrent.futures`` standard library. ``Deadpool`` is
a process pool executor, quite similar to the stdlib's
`ProcessPoolExecutor`_.

This document assumes that you are familiar with the stdlib
`ProcessPoolExecutor`_. If you are not, it is important
to understand that ``Deadpool`` makes very specific tradeoffs that
can result in quite different behaviour to the stdlib
implementation.

Licence
=======

This project can be licenced either under the terms of the `Apache 2.0`_
licence, or the `Affero GPL 3.0`_ licence. The choice is yours.

Installation
============

The python package name is *deadpool-executor*, so to install
you must type ``$ pip install deadpool-executor``. The import
name is *deadpool*, so in your Python code you must type
``import deadpool`` to use it.

I try quite hard to keep dependencies to a minimum. Currently
``Deadpool`` has no dependencies other than ``psutil`` which
is simply too useful to avoid for this library.

Why would I want to use this?
=============================

I created ``Deadpool`` because I became frustrated with the
stdlib `ProcessPoolExecutor`_, and various other community
implementations of process pools. In particular, I had a use-case
that required a high server uptime, but also had variable and
unpredictable memory requirements such that certain tasks could
trigger the `OOM killer`_, often resulting in a "broken" process
pool. I also needed task-specific timeouts that could kill a "hung"
task, which the stdlib executor doesn't provide.

You might wonder, isn't it bad to just kill a task like that?
In my use-case, we had extensive logging and monitoring to alert
us if any tasks failed; but it was paramount that our services
continue to operate even when tasks got killed in OOM scenarios,
or specific tasks took too long. This is the primary trade-off
that ``Deadpool`` offers: the pool will not break, but tasks
can receive SIGKILL under certain conditions. This trade-off
is likely fine if you've seen many OOMs break your pools.

I also tried using the `Pebble <https://github.com/noxdafox/pebble>`_
community process pool. This is a cool project, featuring several
of the properties I've been looking for such as timeouts, and
more resilient operation. However, during testing I found several
occurrences of a mysterious `RuntimeError`_ that caused the Pebble
pool to become broken and no longer accept new tasks.

My goal with ``Deadpool`` is that **the pool must never enter
a broken state**. Any means by which that can happen will be
considered a bug.

What differs from `ProcessPoolExecutor`_?
=========================================

``Deadpool`` is generally similar to `ProcessPoolExecutor`_ since it executes
tasks in subprocesses, and implements the standard ``Executor`` abstract
interface. We can draw a few comparisons to the stdlib pool to guide
your decision process about whether this makes sense for your use-case:

Similarities
------------

- ``Deadpool`` also supports the
  ``max_tasks_per_child`` parameter (a new feature in
  Python 3.11, although it was available in `multiprocessing.Pool`_
  since Python 3.2).
- The "initializer" callback in ``Deadpool`` works the same.
- ``Deadpool`` defaults to the `forkserver <https://docs.python.org/3.11/library/multiprocessing.html#contexts-and-start-methods>`_ multiprocessing
  context, unlike the stdlib pool which defaults to ``fork`` on
  Linux. It's just a setting though, you can change it in the same way as
  with the stdlib pool. Like the stdlib, I strongly advise you to avoid
  using ``fork`` because propagation threads and locks via fork is
  going to ruin your day eventually. While this is a difference to the
  default behaviour of the stdlib pool, it's not a difference in
  behaviour to the stdlib pool when you use the ``forkserver`` context
  which is the recommended context for multiprocessing.

Differences in existing behaviour
---------------------------------

``Deadpool`` differs from the stdlib pool in the following ways:

- If a ``Deadpool`` subprocess in the pool is killed by some
  external actor, for example, the OS runs out of memory and the
  `OOM killer`_ kills a pool subprocess that is using too much memory,
  ``Deadpool`` does not care and further operation is unaffected.
  ``Deadpool`` will not, and indeed cannot raise
  `BrokenProcessPool <https://docs.python.org/3/library/concurrent.futures.html?highlight=broken%20process%20pool#concurrent.futures.process.BrokenProcessPool>`_ or
  `BrokenExecutor <https://docs.python.org/3/library/concurrent.futures.html?highlight=broken%20process%20pool#concurrent.futures.BrokenExecutor>`_.
- ``Deadpool`` precreates all subprocesses up to the pool size on
  creation.
- ``Deadpool`` tasks can have priorities. When the executor chooses
  the next pending task to schedule to a subprocess, it chooses the
  pending task with the highest priority. This gives you a way of
  prioritizing certain kinds of tasks. For example, you might give
  UI-sensitive tasks a higher priority to deliver a more snappy
  user experience to your users. The priority can be specified in
  the ``submit`` call.
- The shutdown parameters ``wait`` and ``cancel_futures`` can behave
  differently to how they work in the `ProcessPoolExecutor`_. This is
  discussed in more detail later in this document.
- ``Deadpool`` currently only works on Linux. There isn't any specific
  reason it can't work on other platforms. The malloc trim feature also
  requires a glibc system, so probably won't work on Alpine.

New features in Deadpool
------------------------

``Deadpool`` has the following features that are not present in the
stdlib pool:

- With ``Deadpool`` you can provider a "finalizer" callback that will
  fire before a subprocess is shut down or killed. The finalizer callback
  might be executed in a different thread than the main thread of the
  subprocess, so don't rely on the callback running in the main
  subprocess thread. There are certain circumstances where the finalizer
  will not run at all, such as when the subprocess is killed by the OS
  due to an out-of-memory (OOM) condition. So don't design your application
  such that the finalizer is required to run for correct operation.
- Even though ``Deadpool`` typically uses a hard kill to remove
  subprocesses, it does still run any handlers registered with
  ``atexit``.
- ``Deadpool`` tasks can have timeouts. When a task hits the timeout,
  the underlying subprocess in the pool is killed with ``SIGKILL``.
  The entire process tree of that subprocess is killed. Your application
  logic needs to handle this. The ``finalizer`` will not run.
- ``Deadpool`` also allows a ``finalizer``, with corresponding
  ``finalargs``, that will be called after a task is executed on
  a subprocess, but before the subprocess terminates. It is
  analogous to the ``initializer`` and ``initargs`` parameters.
  Just like the ``initializer`` callable, the ``finalizer``
  callable is executed inside the subprocess. It is not guaranteed that
  the finalizer will always run. If a process is killed, e.g. due to a
  timeout or any other reason, the finalizer will not run. The finalizer
  could be used for things like flushing pending monitoring messages,
  such as traces and so on.
- ``Deadpool`` can ask the system allocator (Linux only) to return
  unused memory back to the OS based on exceeding a max threshold RSS.
  For long-running pools and modern
  kernels, the system memory allocator can hold onto unused memory
  for a surprisingly long time, and coupled with bloat due to
  memory fragmentation, this can result in carrying very large
  RSS values in your pool. The ``max_tasks_per_child`` helps with
  this because a subprocess is entirely erased when the max is
  reached, but it does mean that periodically there will be a small
  latency penalty from constructing the replacement subprocess. In
  my opinion, ``max_tasks_per_child`` is appropriate for when you
  know or suspect there's a real memory leak somewhere in your code
  (or a 3rd-party package!), and the easiest way to deal with that
  right now is just to periodically remove a process.
- ``Deadpool`` can propagate ``os.environ`` to the subprocesses.
  Normally, env vars present at the time of the "main" process will
  propagate to subprocesses, but dynamically modified env vars
  via ``os.environ`` will not. Actually, it depends on the start
  method, with ``fork`` doing the propagation, and ``forkserver``
  and ``spawn`` not doing it. The parameter ``propagate_environ``,
  e.g., ``propagate_environ=os.environ``, re-enables this for
  ``forkserver`` and ``spawn``. The supplied mapping will be
  applied to the subprocesses as they are created. This also means
  that if you want to modify some settings, you can modify the
  mapping object at any time, and new subprocesses created after
  that modification will get the new vars. One example use-case
  is dynamically changing the logging level within subprocesses.

Minimum and Maximum Workers
~~~~~~~~~~~~~~~~~~~~~~~~~~~

``Deadpool`` has a ``min_workers`` and ``max_workers`` parameter.
While ``max_workers`` is the same as the stdlib pool, ``min_workers``
is a new feature.

The ``min_workers`` parameter allows deadpool to "scale down" the
pool when it is idle. This is another strategy alongside other
features like ``max_tasks_per_child`` and ``max_worker_memory_bytes``
to help deal with memory bloat in long-running pools.

Statistics
~~~~~~~~~~

Here is a very simple example of how to get statistics from the
executor:

.. code-block:: python

    with deadpool.Deadpool() as exe:
        fut = exe.submit(...)
        stats = exe.get_statistics()

The call must be made while the executor is still alive. It
will succeed after the executor is shut down or closed, but
some of the statistics will be zeroed out.

The call to ``get_statistics`` will return a dictionary with the
following keys:

- ``tasks_received``: The total number of tasks submitted to the
  executor. Does not mean that they started running, only that they
  were successfully submitted.
- ``tasks_launched``: The total number of tasks that were launched
  on a subprocess. This records the count of all tasks that were
  successfully scheduled to run. These tasks were picked up from
  the submit backlog and given to a worker process to execute.
- ``tasks_failed``: The total number of tasks that failed. This
  includes tasks that raised an exception, and tasks that were
  killed due to a timeout, and really any other reason that a task
  failed.
- ``worker_processes_created``: The total number of subprocesses that
  were ever created by the executor. This can be, and often will be
  greater than the `max_workers` setting because there are many options
  that can cause workers to be discarded and replaced. Examples of these
  might be the ``max_tasks_per_child`` setting, or the ``min_workers``
  setting, or the memory thresholds and so on.
- ``max_workers_busy_concurrently``: The maximum number of workers that
  were ever busy at the same time. This is a useful statistic to
  decide whether you might consider increasing or decreasing the size
  of the pool. For example, if your ``max_workers`` is set to 100, but
  after running for, say, a week, you see that ``max_workers_busy_concurrently``
  is only 50, then you might consider reducing the pool size to 50.
  The system memory manager on linux likes to hold onto heap memory.
  If your have more workers than you need, you'll see that the system
  memory usage over time is going to be higher than it needs to be
  because even when the pool is fully idle, you will still observe
  the persistent worker processes having a large memory allocation
  even though no jobs are running. This is a symptom of malloc
  retention behaviour.
- ``worker_processes_still_alive``: The number of worker processes that
  are still alive. This includes both idle and busy worker processes.
  This is mainly a debugging statistic that I can use to check whether
  worker processes are "leaking" somehow and not being cleaned up
  correctly. This number should not be greater than the ``max_workers``.
  (It could be, temporarily, depending on the exact timing and strategy
  in the inner workings of the executor, but on average it should not)
- ``worker_processes_idle``: The number of worker processes that are idle.
- ``worker_processes_busy``: The number of worker processes that are busy.


Here is an example from the tests to explain what each of the
statistics mean:

.. code-block:: python

    with deadpool.Deadpool(min_workers=5, max_workers=10) as exe:
        futs = []
        for _ in range(50):
            futs.append(exe.submit(t, 0.05))
            futs.append(exe.submit(f_err, Exception))

        results = []
        for fut in deadpool.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:
                pass

        time.sleep(0.5)
        stats = exe.get_statistics()

    assert results == [0.05] * 50
    print(f"{stats=}")
    assert stats == {
        "tasks_received": 100,
        "tasks_launched": 100,
        "tasks_failed": 50,
        "worker_processes_created": 10,
        "max_workers_busy_concurrently": 10,
        "worker_processes_still_alive": 5,
        "worker_processes_idle": 5,
        "worker_processes_busy": 0,
    }

In this example, we submit 100 tasks, 50 of which will raise an
exception. The executor will create 10 worker processes, and
will have a maximum of 10 workers busy at the same time. After
all the tasks are completed, we wait for a short time to allow
the executor to clean up any worker processes that are no longer
needed. The statistics should show that 5 worker processes are
still alive, and all of them are idle.

Show me some code
=================

Simple case
-----------

The simple case works exactly the same as with `ProcessPoolExecutor`_:

.. code-block:: python

    import deadpool

    def f():
        return 123

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f)
        result = fut.result()

    assert result == 123

It is intended that all the basic behaviour should "just work" in the
same way, and ``Deadpool`` should be a drop-in replacement for
`ProcessPoolExecutor`_; but there are some subtle differences so you
should read all of this document to see if any of those will affect you.

Timeouts
--------

If a timeout is reached on a task, the subprocess running that task will be
killed, as in ``SIGKILL``. ``Deadpool`` doesn't mind, but your own
application should: if you use timeouts it is likely important that your tasks
be `idempotent <https://en.wikipedia.org/wiki/Idempotence>`_, especially if
your application will restart tasks, or restart them after application deployment,
and other similar scenarios.

.. code-block:: python

    import time
    import deadpool

    def f():
        time.sleep(10.0)

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f, deadpool_timeout=1.0)

        with pytest.raises(deadpool.TimeoutError)
            fut.result()

The parameter ``deadpool_timeout`` is special and consumed by ``Deadpool``
in the call. You can't use a parameter with this name in your function 
kwargs.

Handling OOM killed situations
------------------------------

.. code-block:: python

    import time
    import deadpool

    def f():
        x = list(range(10**100))

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f, deadpool_timeout=1.0)

        try:
            result = fut.result()
        except deadpool.ProcessError:
            print("Oh no someone killed my task!")


As long as the OOM killer terminates merely a subprocess (and not the main
process), which is likely because it'll be your subprocess that is using too
much memory, this will not hurt the pool, and it will be able to receive and
process more tasks. Note that this event will show up as a ``ProcessError``
exception when accessing the future, so you have a way of at least tracking
these events.

Design Details
==============

Typical Example - with timeouts
-------------------------------

Here's a typical example of how code using Deadpool might look. The
output of the code further below should be similar to the following:

.. code-block:: bash

    $ python examples/entrypoint.py
    ...................xxxxxxxxxxx.xxxxxxx.x.xxxxxxx.x
    $

Each ``.`` is a successfully completed task, and each ``x`` is a task
that timed out. Below is the code for this example.

.. code-block:: python

    import random, time
    import deadpool


    def work():
        time.sleep(random.random() * 4.0)
        print(".", end="", flush=True)
        return 1


    def main():
        with deadpool.Deadpool() as exe:
            futs = (exe.submit(work, deadpool_timeout=2.0) for _ in range(50))
            for fut in deadpool.as_completed(futs):
                try:
                    assert fut.result() == 1
                except deadpool.TimeoutError:
                    print("x", end="", flush=True)


    if __name__ == "__main__":
        main()
        print()

- The work function will be busy for a random time period between 0 and
  4 seconds.
- There is a ``deadpool_timeout`` kwarg given to the ``submit`` method.
  This kwarg is special and will be consumed by Deadpool. You cannot
  use this kwarg name for your own task functions.
- When a task completes, it prints out ``.`` internally. But when a task
  raises a ``deadpool.TimeoutError``, a ``x`` will be printed out instead.
- When a task times out, keep in mind that the underlying process that
  is executing that task is killed, literally with the ``SIGKILL`` signal.

Deadpool tasks have priority
----------------------------

The example below is similar to the previous one for timeouts. In fact
this example retains the timeouts to show how the different features
compose together. In this example we create tasks with different
priorities, and we change the printed character of each task to show
that higher priority items are executed first.

The code example will print something similar to the following:

.. code-block:: bash

    $ python examples/priorities.py
    !!!!!xxxxxxxxxxx!x..!...x.xxxxxxxx.xxxx.x...xxxxxx

You can see how the ``!`` characters, used for indicating higher priority
tasks, appear towards the front indicating that they were executed sooner.
Below is the code.

.. code-block:: python

    import random, time
    import deadpool


    def work(symbol):
        time.sleep(random.random() * 4.0)
        print(symbol, end="", flush=True)
        return 1


    def main():
        with deadpool.Deadpool(max_backlog=100) as exe:
            futs = []
            for _ in range(25):
                fut = exe.submit(work, ".",deadpool_timeout=2.0, deadpool_priority=10)
                futs.append(fut)
                fut = exe.submit(work, "!",deadpool_timeout=2.0, deadpool_priority=0)
                futs.append(fut)

            for fut in deadpool.as_completed(futs):
                try:
                    assert fut.result() == 1
                except deadpool.TimeoutError:
                    print("x", end="", flush=True)


    if __name__ == "__main__":
        main()
        print()

- When the tasks are submitted, they are given a priority. The default
  value for the ``deadpool_priority`` parameter is 0, but here we'll
  write them out explicity.  Half of the tasks will have priority 10 and
  half will have priority 0.
- A lower value for the ``deadpool_priority`` parameters means a **higher**
  priority. The highest priority allowed is indicated by 0. Negative
  priority values are not allowed.
- I also specified the ``max_backlog`` parameter when creating the
  Deadpool instance. This is discussed in more detail next, but quickly:
  task priority can only be enforced on what is in the submitted backlog
  of tasks, and the ``max_backlog`` parameter controls the depth of that
  queue. If ``max_backlog`` is too low, then the window of prioritization
  will not include tasks submitted later which might have higher priorities
  than earlier-submitted tasks. The ``submit`` call will in fact block
  once the ``max_backlog`` depth has been reached.

Controlling the backlog of submitted tasks
------------------------------------------

By default, the ``max_backlog`` parameter is set to 5. This parameter is
used to create the "submit queue" size. The submit queue is the place
where submitted tasks are held before they are executed in background
processes.

If the submit queue is large (``max_backlog``), it will mean
that a large number of tasks can be added to the system with the
``submit`` method, even before any tasks have finished exiting. Conversely,
a low ``max_backlog`` parameter means that the submit queue will fill up
faster. If the submit queue is full, it means that the next call to
``submit`` will block.

This kind of blocking is fine, and typically desired. It means that
backpressure from blocking is controlling the amount of work in flight.
By using a smaller ``max_backlog``, it means that you'll also be
limiting the amount of memory in use during the execution of all the tasks.

However, if you nevertheless still accumulate received futures as my
example code above is doing, that accumulation, i.e., the list of futures,
will contribute to memory growth. If you have a large amount of work, it
will be better to set a *callback* function on each of the futures rather
than processing them by iterating over ``as_completed``.

The example below illustrates this technique for keeping memory
consumption down:

.. code-block:: python

    import random, time
    import deadpool


    def work():
        time.sleep(random.random() * 4.0)
        print(".", end="", flush=True)
        return 1


    def cb(fut):
        try:
            assert fut.result() == 1
        except deadpool.TimeoutError:
            print("x", end="", flush=True)


    def main():
        with deadpool.Deadpool() as exe:
            for _ in range(50):
                exe.submit(work, deadpool_timeout=2.0).add_done_callback(cb)


    if __name__ == "__main__":
        main()
        print()


With this callback-based design, we no longer have an accumulation of futures
in a list. We get the same kind of output as in the "typical example" from
earlier:

.. code-block:: bash

    $ python examples/callbacks.py
    .....xxx.xxxxxxxxx.........x..xxxxx.x....x.xxxxxxx


Speaking of callbacks, the customized ``Future`` class used by Deadpool
lets you set a callback for when the task begins executing on a real
system process. That can be configured like so:

.. code-block:: python

    with deadpool.Deadpool() as exe:
        f = exe.submit(work)

        def cb(fut: deadpool.Future):
            print(f"My task is running on process {fut.pid}")

        f.add_pid_callback(cb)

Obviously, both kinds of callbacks can be added:

.. code-block:: python

    with deadpool.Deadpool() as exe:
        f = exe.submit(work)
        f.add_pid_callback(lambda fut: f"Started on {fut.pid=}")
        f.add_done_callback(lambda fut: f"Completed {fut.pid=}")

More about shutdown
-------------------

In the documentation for ProcessPoolExecutor_, the following function
signature is given for the shutdown_ method of the executor interface:

.. code-block:: python

    shutdown(wait=True, *, cancel_futures=False)

I want to honor this, but it presents some difficulties because the
semantics of the ``wait`` and ``cancel_futures`` parameters need to be
somewhat different for Deadpool.

In Deadpool, this is what the combinations of those flags mean:

.. csv-table:: Shutdown flags
   :header: ``wait``, ``cancel_futures``, ``effect``
   :widths: 10, 10, 80
   :align: left

   ``True``, ``True``, "Wait for already-running tasks to complete; the
   ``shutdown()`` call will unblock (return) when they're done. Cancel
   all pending tasks that are in the submit queue, but have not yet started
   running. The ``fut.cancelled()`` method will return ``True`` for such
   cancelled tasks."
   ``True``, ``False``, "Wait for already-running tasks to complete.
   Pending tasks in the
   submit queue that have not yet started running will *not* be cancelled, and
   will all continue to execute. The ``shutdown()`` call will return only
   after all submitted tasks have completed. "
   ``False``, ``True``, "Already-running tasks **will be cancelled** and this
   means the underlying subprocesses executing these tasks will receive
   SIGKILL. Pending tasks on the submit queue that have not yet started
   running will also be cancelled."
   ``False``, ``False``, "This is a strange one. What to do if the caller
   doesn't want to wait, but also doesn't want to cancel things? In this
   case, already-running tasks will be allowed to complete, but pending
   tasks on the submit queue will be cancelled. This is the same outcome as
   as ``wait==True`` and ``cancel_futures==True``. An alternative design
   might have been to allow all tasks, both running and pending, to just
   keep going in the background even after the ``shutdown()`` call
   returns. Does anyone have a use-case for this?"

If you're using ``Deadpool`` as a context manager, you might be wondering
how exactly to set these parameters in the ``shutdown`` call, since that
call is made for you automatically when the context manager exits.

For this, Deadpool provides additional parameters that can be provided
when creating the instance:

.. code-block:: python

   # This is pseudocode
   import deadpool

   with deadpool.DeadPool(
           shutdown_wait=True,
           shutdown_cancel_futures=True
   ):
       fut = exe.submit(...)

Developer Workflow
==================

nox
---

This project uses ``nox``. Follow the instructions for installing
nox at their page, and then come back here.

While nox can be configured so that all the tools for each of
the tasks can be installed automatically when run, this takes
too much time and so I've decided that you should just have
the following tools in your environment, ready to go. They
do not need to be installed in the same venv or anything like
that. I've found a convenient way to do this is with ``pipx``.
For example, to install ``black`` using ``pipx`` you can do
the following:

.. code-block:: shell

   $ pipx install black

You must do the same for ``isort`` and ``ruff``. See the following
sections for actually using ``nox`` to perform dev actions.

tests
-----

To run the tests:

.. code-block:: shell

   $ nox -s test

To run tests for a particular version, and say with coverage:

.. code-block:: shell

   $ nox -s testcov-3.11

To pass additional arguments to pytest, use the ``--`` separator:

.. code-block:: shell

   $ nox -s testcov-3.11 -- -k test_deadpool -s <etc>

This is nonstandard above, but I customized the ``noxfile.py`` to
allow this.

style
-----

To apply style fixes, and check for any remaining lints,

.. code-block:: shell

   $ nox -t style

docs
----

The only docs currently are this README, which uses RST. Github
uses `docutils <https://docutils.sourceforge.io/docs/ref/rst/directives.html>`_
to render RST.

release
-------

This project uses flit to release the package to pypi. The whole
process isn't as automated as I would like, but this is what
I currently do:

1. Ensure that ``main`` branch is fully up to date with all to
   be released, and all the tests succeed.
2. Change the ``__version__`` field in ``deadpool.py``. Flit
   uses this to stamp the version.
3. Verify that ``flit build`` succeeds. This will produce a
   wheel in the ``dist/`` directory. You can inspect this
   wheel to ensure it contains only what is necessary. This
   wheel will be what is uploaded to PyPI.
4. **Commit the changed ``__version__``**. Easy to forget this
   step, resulting in multiple awkward releases to try to
   get the state all correct again.
5. Now create the git tag and push to github:

   .. code-block:: shell

        $ git tag YYYY.MM.patch
        $ git push --tags origin main

6. Now deploy to PyPI:

   .. code-block:: shell

        $ flit publish


.. _shutdown: https://docs.python.org/3/library/concurrent.futures.html?highlight=brokenprocesspool#concurrent.futures.Executor.shutdown
.. _ProcessPoolExecutor: https://docs.python.org/3/library/concurrent.futures.html?highlight=broken%20process%20pool#processpoolexecutor
.. _RuntimeError: https://github.com/noxdafox/pebble/issues/42#issuecomment-551245730
.. _OOM killer: https://en.wikipedia.org/wiki/Out_of_memory#Out_of_memory_management
.. _multiprocessing.Pool: https://docs.python.org/3.11/library/multiprocessing.html#multiprocessing.pool.Pool
.. _Apache 2.0: https://www.apache.org/licenses/LICENSE-2.0
.. _Affero GPL 3.0: https://www.gnu.org/licenses/agpl-3.0.html
