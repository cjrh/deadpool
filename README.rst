.. image:: https://github.com/cjrh/deadpool/workflows/Python%20application/badge.svg
    :target: https://github.com/cjrh/deadpool/actions

.. image:: https://coveralls.io/repos/github/cjrh/deadpool/badge.svg?branch=main
    :target: https://coveralls.io/github/cjrh/deadpool?branch=main

.. image:: https://img.shields.io/pypi/pyversions/deadpool-executor.svg
    :target: https://pypi.python.org/pypi/deadpool-executor

.. image:: https://img.shields.io/github/tag/cjrh/deadpool.svg
    :target: https://img.shields.io/github/tag/cjrh/deadpool.svg

.. image:: https://img.shields.io/badge/install-pip%20install%20deadpool--executor-ff69b4.svg
    :target: https://img.shields.io/badge/install-pip%20install%20deadpool--executor-ff69b4.svg

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

.. image:: https://api.securityscorecards.dev/projects/github.com/cjrh/deadpool/badge
    :alt: OpenSSF Scorecard
    :target: https://api.securityscorecards.dev/projects/github.com/cjrh/deadpool


Deadpool
--------

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

.. sectnum::

.. contents::
   :local:
   :depth: 2
   :backlinks: entry

Installation
------------

The python package name is *deadpool-executor*, so to install
you must type ``$ pip install deadpool-executor``. The import
name is *deadpool*, so in your Python code you must type
``import deadpool`` to use it.

Why would I want to use this?
-----------------------------

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
-----------------------------------------

``Deadpool`` is generally similar to `ProcessPoolExecutor`_ since it executes
tasks in subprocesses, and implements the standard ``Executor`` abstract
interface. We can draw a few comparisons to the stdlib pool to guide
your decision process about whether this makes sense for your use-case:

- ``Deadpool`` precreates all subprocesses up to the pool size.
- ``Deadpool`` also supports the
  ``max_tasks_per_child`` parameter (a new feature in
  Python 3.11, although it was available in `multiprocessing.Pool`_
  since Python 3.2).
- ``Deadpool`` tasks can have priorities. When the executor chooses
  the next pending task to schedule to a subprocess, it chooses the
  pending task with the highest priority. This gives you a way of
  prioritizing certain kinds of tasks. For example, you might give
  UI-sensitive tasks a higher priority to deliver a more snappy
  user experience to your users.
- ``Deadpool`` defaults to the `forkserver <https://docs.python.org/3.11/library/multiprocessing.html#contexts-and-start-methods>`_ multiprocessing
  context, unlike the stdlib pool which defaults to ``fork`` on
  Linux. It's just a setting though, you can change it in the same way as
  with the stdlib pool. Like the stdlib, I strongly advise you to avoid
  using ``fork`` because propagation threads and locks via fork is
  going to ruin your day eventually.
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
- ``Deadpool`` tasks can have timeouts. When a task hits the timeout,
  the underlying subprocess in the pool is killed with ``SIGKILL``.
  The entire process tree of that subprocess is killed. Your application
  logic needs to handle this. The ``finalizer`` will not run.
- ``Deadpool`` tasks can have priorities. The priority is set in the
  ``submit()`` call. See the examples later in this document for further
  discussion on priorities.
- The shutdown parameters ``wait`` and ``cancel_futures`` can behave
  differently to how they work in the _ProcessPoolExecutor. This is
  discussed in more detail later in this document.
- If a ``Deadpool`` subprocess in the pool is killed by some
  external actor, for example, the OS runs out of memory and the
  `OOM killer`_ kills a pool subprocess that is using too much memory,
  ``Deadpool`` does not care and further operation is unaffected.
  ``Deadpool`` will not, and indeed cannot raise
  `BrokenProcessPool <https://docs.python.org/3/library/concurrent.futures.html?highlight=broken%20process%20pool#concurrent.futures.process.BrokenProcessPool>`_ or
  `BrokenExecutor <https://docs.python.org/3/library/concurrent.futures.html?highlight=broken%20process%20pool#concurrent.futures.BrokenExecutor>`_.
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
- ``Deadpool`` currently only works on Linux. There isn't any specific
  reason it can't work on other platforms. The malloc trim feature also
  requires a glibc system, so probably won't work on Alpine.

Show me some code
-----------------

Simple case
^^^^^^^^^^^

The simple case works exactly the same as with `ProcessPoolExecutor`_:

.. code-block:: python

    from deadpool import Deadpool

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
^^^^^^^^

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

Handling OOM killed situations
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    import time
    import deadpool

    def f():
        x = list(range(10**100))

    with deadpool.Deadpool() as exe:
        fut = exe.submit(f, timeout=1.0)

        try:
            result = fut.result()
        except deadpool.ProcessError:
            print("Oh no someone killed my task!")


As long as the OOM killer terminates the subprocess (and not the main process),
which is likely because it'll be your subprocess that is using too much
memory, this will not hurt the pool, and it will be able to receive and
process more tasks. Note that this event will show up as a ``ProcessError``
exception when accessing the future, so you have a way of at least tracking
these events.

Design Details
--------------

Typical Example - with timeouts
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
            futs = (exe.submit(work, timeout=2.0) for _ in range(50))
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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
^^^^^^^^^^^^^^^^^^^

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
------------------

nox
^^^
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
^^^^^

To run the tests:

.. code-block:: shell

   $ nox -s tests

style
^^^^^

To apply style fixes, and check for any remaining lints,

.. code-block:: shell

   $ nox -t style

docs
^^^^

The only docs currently are this README, which uses RST. Github 
uses `docutils <https://docutils.sourceforge.io/docs/ref/rst/directives.html>`_
to render RST.

release
^^^^^^^

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
