"""
Deadpool
========


"""
import concurrent.futures
import ctypes
import logging
import multiprocessing as mp
import os
import pickle
import signal
import sys
import threading
import traceback
import typing
import weakref
from concurrent.futures import CancelledError, Executor, InvalidStateError, as_completed
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from queue import Empty, PriorityQueue, Queue, SimpleQueue
from typing import Callable, Optional, Tuple

import psutil

__version__ = "2023.7.4"
__all__ = [
    "Deadpool",
    "Future",
    "CancelledError",
    "TimeoutError",
    "ProcessError",
    "PoolClosed",
    "as_completed",
]
logger = logging.getLogger("deadpool")


# Does not work. Hangs the process on exit.
# There currently isn't an official way to clean up the
# resource tracker process. It is an open issue on the
# Python issue tracker.
# @atexit.register
# def stop_resource_tracker():
#     from multiprocessing import resource_tracker
#     tracker = resource_tracker._resource_tracker
#     try:
#         import time
#         time.sleep(5)
#         tracker._stop()
#     except Exception:
#         logger.info("Error stopping the multiprocessing resource tracker")


@dataclass(order=True)
class PrioritizedItem:
    priority: int
    item: typing.Any = field(compare=False)


@dataclass(init=False)
class WorkerProcess:
    process: mp.Process
    connection_receive_msgs_from_process: Connection
    connection_send_msgs_to_process: Connection
    # Stats
    tasks_ran_counter: int
    # Controls
    # If the subprocess RSS memory is above this threshold,
    # ask the system allocator to release unused memory back
    # to the OS.
    malloc_trim_rss_memory_threshold_bytes: Optional[int] = None
    ok: bool = True

    def __init__(
        self,
        initializer=None,
        initargs=(),
        finalizer=None,
        finargs=(),
        daemon=True,
        mp_context="forkserver",
        malloc_trim_rss_memory_threshold_bytes=None,
    ):
        if isinstance(mp_context, str):
            mp_context = mp.get_context(mp_context)

        # For the process to send info OUT OF the process
        conn_receiver, conn_sender = mp.Pipe(duplex=False)
        # For sending work INTO the process
        conn_receiver2, conn_sender2 = mp.Pipe(duplex=False)
        p = mp_context.Process(
            daemon=daemon,
            target=raw_runner2,
            args=(
                conn_sender,
                conn_receiver2,
                os.getpid(),
                initializer,
                initargs,
                finalizer,
                finargs,
                malloc_trim_rss_memory_threshold_bytes,
            ),
        )

        p.start()
        self.process = p
        self.connection_receive_msgs_from_process = conn_receiver
        self.connection_send_msgs_to_process = conn_sender2
        self.tasks_ran_counter = 0
        self.ok = True

    def __hash__(self):
        return hash(self.process.pid)

    @property
    def pid(self):
        return self.process.pid

    def get_rss_bytes(self) -> int:
        return psutil.Process(pid=self.pid).memory_info().rss

    def submit_job(self, job):
        self.tasks_ran_counter += 1
        self.connection_send_msgs_to_process.send(job)

    def shutdown(self, wait=True):
        if not self.process.is_alive():
            return

        self.connection_receive_msgs_from_process.close()

        if self.connection_send_msgs_to_process.writable:
            try:
                self.connection_send_msgs_to_process.send(None)
            except BrokenPipeError:
                pass
            else:
                self.connection_send_msgs_to_process.close()

        if wait:
            self.process.join()

    def is_alive(self):
        return self.process.is_alive()

    def results_are_available(self, block_for: float = 0.2):
        return self.connection_receive_msgs_from_process.poll(timeout=block_for)

    def get_results(self):
        return self.connection_receive_msgs_from_process.recv()


class Future(concurrent.futures.Future):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pid: Optional[int] = None
        self.pid_callback = None

    @property
    def pid(self):
        return self._pid

    @pid.setter
    def pid(self, value):
        self._pid = value
        if self.pid_callback:
            try:
                self.pid_callback(self)
            except Exception:  # pragma: no cover
                logger.exception("Error calling pid_callback")

    def add_pid_callback(self, fn):
        self.pid_callback = fn

    def cancel_and_kill_if_running(self, sig=signal.SIGKILL):
        self.cancel()
        if self.pid:
            try:
                kill_proc_tree(self.pid, sig=sig)
            except Exception as e:  # pragma: no cover
                logger.warning(f"Got error killing pid {self.pid}: {e}")


class TimeoutError(concurrent.futures.TimeoutError):
    ...


class ProcessError(mp.ProcessError):
    ...


class PoolClosed(Exception):
    ...


class Deadpool(Executor):
    def __init__(
        self,
        max_workers: Optional[int] = None,
        min_workers: Optional[int] = None,
        max_tasks_per_child: Optional[int] = None,
        max_worker_memory_bytes: Optional[int] = None,
        mp_context=None,
        initializer=None,
        initargs=(),
        finalizer=None,
        finalargs=(),
        max_backlog=5,
        shutdown_wait: Optional[bool] = None,
        shutdown_cancel_futures: Optional[bool] = None,
        daemon=True,
        malloc_trim_rss_memory_threshold_bytes: Optional[int] = None,
    ) -> None:
        super().__init__()

        if not mp_context:
            mp_context = "forkserver"

        if isinstance(mp_context, str):
            mp_context = mp.get_context(mp_context)

        self.ctx = mp_context
        self.initializer = initializer
        self.initargs = initargs
        self.finitializer = finalizer
        self.finitargs = finalargs
        self.pool_size = max_workers or len(os.sched_getaffinity(0))
        self.min_workers = min_workers or self.pool_size
        self.max_tasks_per_child = max_tasks_per_child
        self.max_worker_memory_bytes = max_worker_memory_bytes
        self.submitted_jobs: PriorityQueue[PrioritizedItem] = PriorityQueue(
            maxsize=max_backlog
        )
        self.running_jobs = Queue(maxsize=self.pool_size)
        self.running_futs = weakref.WeakSet()
        self.closed = False
        self.shutdown_wait = shutdown_wait
        self.shutdown_cancel_futures = shutdown_cancel_futures
        self.daemon = daemon
        self.malloc_trim_rss_memory_threshold_bytes = (
            malloc_trim_rss_memory_threshold_bytes
        )

        # TODO: overcommit
        self.workers = SimpleQueue()
        for _ in range(self.pool_size):
            self.add_worker_to_pool()
        # When a worker is running a job, it will be removed from
        # the workers queue, and added to the busy_workers set.
        # When a worker successfully completes a job, it will be
        # added back to the workers queue, and removed from the
        # busy_workers set.
        self.busy_workers = set()  #  weakref.WeakSet()

        # THE ONLY ACTIVE, PERSISTENT STATE IN DEADPOOL IS THIS THREAD
        # BELOW. PROTECT IT AT ALL COSTS.
        self.runner_thread = threading.Thread(
            target=self.runner, name="deadpool.runner", daemon=True
        )
        self.runner_thread.start()

    def add_worker_to_pool(self):
        worker = WorkerProcess(
            initializer=self.initializer,
            initargs=self.initargs,
            finalizer=self.finitializer,
            finargs=self.finitargs,
            mp_context=self.ctx,
            daemon=self.daemon,
            malloc_trim_rss_memory_threshold_bytes=self.malloc_trim_rss_memory_threshold_bytes,
        )
        self.workers.put(worker)

    def runner(self):
        while True:
            # This will block if the queue of running jobs is full.
            self.running_jobs.put(None)

            priority_job = self.submitted_jobs.get()
            job = priority_job.item
            if job is None:
                # This is for the `None` that terminates the while loop.
                self.submitted_jobs.task_done()
                self.running_jobs.get()
                # TODO: this probably isn't necessary, since cleanup is happening
                # in the shutdown method anyway.
                cancel_all_futures_on_queue(self.submitted_jobs)
                logger.debug("Got shutdown event, leaving runner.")
                return

            *_, fut = job
            if fut.done():
                # This shouldn't really be possible, but if the associated future
                # for this job has somehow already been marked as done (e.g. if
                # the caller decided to cancel it themselves) then just skip the
                # whole job.
                self.submitted_jobs.task_done()
                self.running_jobs.get()
                continue

            t = threading.Thread(target=self.run_task, args=job, daemon=True)
            t.start()

    def get_process(self) -> WorkerProcess:
        bw = len(self.busy_workers)
        mw = self.pool_size
        qs = self.workers.qsize()

        total_workers = bw + qs
        if total_workers < mw and qs == 0:
            self.add_worker_to_pool()

        wp = self.workers.get()
        self.busy_workers.add(wp)
        return wp

    def done_with_process(self, wp: WorkerProcess):
        self.busy_workers.remove(wp)

        bw = len(self.busy_workers)
        mw = self.min_workers
        qs = self.workers.qsize()

        total_workers = bw + qs
        if total_workers > mw and qs > 0:
            wp.shutdown(wait=False)
            return

        if not wp.is_alive():
            self.add_worker_to_pool()
            return

        if not wp.ok:
            self.add_worker_to_pool()
            return

        if self.max_tasks_per_child is not None:
            if wp.tasks_ran_counter >= self.max_tasks_per_child:
                logger.debug(f"Worker {wp.pid} hit max tasks per child.")
                wp.shutdown(wait=False)
                self.add_worker_to_pool()
                return

        if self.max_worker_memory_bytes is not None:
            if wp.get_rss_bytes() >= self.max_worker_memory_bytes:
                wp.shutdown(wait=False)
                self.add_worker_to_pool()
                return

        self.workers.put(wp)

    def run_task(self, fn, args, kwargs, timeout, fut: Future):
        try:
            worker: WorkerProcess = self.get_process()
            worker.submit_job((fn, args, kwargs, timeout))
            fut.pid = worker.pid
            self.running_futs.add(fut)

            while True:
                if worker.results_are_available():
                    try:
                        results = worker.get_results()
                    except EOFError:
                        fut.set_exception(
                            ProcessError("Worker process died unexpectedly")
                        )
                    except BaseException as e:
                        logger.debug(f"Unexpected exception from worker: {e}")
                        fut.set_exception(e)
                    else:
                        if isinstance(results, BaseException):
                            fut.set_exception(results)
                        else:
                            fut.set_result(results)

                        if isinstance(results, TimeoutError):
                            logger.debug(
                                f"TimeoutError on {worker.pid}, setting ok=False"
                            )
                            worker.ok = False
                    finally:
                        break
                elif not worker.is_alive():
                    logger.debug(f"p is no longer alive: {worker.process}")
                    try:
                        signame = signal.strsignal(-worker.process.exitcode)
                    except (ValueError, TypeError):  # pragma: no cover
                        signame = "Unknown"

                    if not fut.done():
                        # It is possible that fut has already had a result set on
                        # it. If that's the case we'll do nothing. Otherwise, put
                        # an exception reporting the unexpected situation.
                        msg = (
                            f"Subprocess {worker.pid} completed unexpectedly with "
                            f"exitcode {worker.process.exitcode} ({signame})"
                        )
                        try:
                            fut.set_exception(ProcessError(msg))
                        except InvalidStateError:  # pragma: no cover
                            # We still have to catch this even though there is a
                            # check for `fut.done()`, simply due to an possible
                            # race between the done check and the set_exception call.
                            pass

                    break
                else:
                    pass  # pragma: no cover

            self.done_with_process(worker)
        finally:
            self.submitted_jobs.task_done()

            if not fut.done():  # pragma: no cover
                fut.set_exception(ProcessError("Somehow no result got set on fut."))

            try:
                self.running_jobs.get_nowait()
            except Empty:  # pragma: no cover
                logger.warning("Weird error, did not expect running jobs to be empty")

    def submit(
        self,
        __fn: Callable,
        *args,
        deadpool_timeout=None,
        deadpool_priority=0,
        **kwargs,
    ) -> Future:
        if deadpool_priority < 0:  # pragma: no cover
            raise ValueError(
                f"Parameter deadpool_priority must be >= 0, but was {deadpool_priority}"
            )

        if self.closed:
            raise PoolClosed("The pool is closed. No more tasks can be submitted.")

        fut = Future()
        self.submitted_jobs.put(
            PrioritizedItem(
                priority=deadpool_priority,
                item=(__fn, args, kwargs, deadpool_timeout, fut),
            )
        )
        return fut

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        logger.debug(f"shutdown: {wait=} {cancel_futures=}")

        # No more new tasks can be submitted
        self.closed = True

        if cancel_futures:
            cancel_all_futures_on_queue(self.submitted_jobs)

        if wait:
            # The None sentinel will pop last
            shutdown_priority = sys.maxsize
        else:
            # The None sentinel will pop first
            shutdown_priority = -1

        try:
            self.submitted_jobs.put(
                PrioritizedItem(priority=shutdown_priority, item=None),
                timeout=2.0,
            )
        except TimeoutError:  # pragma: no cover
            logger.warning(
                "Timed out putting None on the submit queue. This "
                "should not be possible "
                "and might be a bug in deadpool."
            )

        # Up till this point, all the pending work that has been
        # submitted, but not yet started, has been cancelled. The
        # runner loop has also been stopped (with the None sentinel).
        # The only thing left to do is decide whether or not to
        # actively kill processes that are still running. We presume
        # that if the user is asking for cancellation and doesn't
        # want to wait, that she probably wants us to also stop
        # running processes.
        if (not wait) and cancel_futures:
            running_futs = list(self.running_futs)
            for fut in running_futs:
                fut.cancel_and_kill_if_running()

        logger.debug("waiting for submitted_jobs to join...")
        self.submitted_jobs.join()

        super().shutdown(wait, cancel_futures=cancel_futures)

        # We can now remove all other processes hanging around
        # in the background.
        while not self.workers.empty():
            try:
                worker = self.workers.get_nowait()
                worker.shutdown()
            except Empty:
                break

        # There may be a few processes left in the
        # `busy_workers` queue. Shut them down too.
        while self.busy_workers:
            worker = self.busy_workers.pop()
            worker.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.closed:
            kwargs = {}
            if self.shutdown_wait is not None:
                kwargs["wait"] = self.shutdown_wait

            if self.shutdown_cancel_futures is not None:
                kwargs["cancel_futures"] = self.shutdown_cancel_futures

            self.shutdown(**kwargs)

        self.runner_thread.join()
        return False


def cancel_all_futures_on_queue(q: Queue):
    while True:
        try:
            priority_item = q.get_nowait()
            q.task_done()
            job = priority_item.item
            *_, fut = job
            fut.cancel()
        except Empty:
            break


def raw_runner2(
    conn: Connection,
    conn_receiver: Connection,
    parent_pid,
    initializer,
    initargs,
    finitializer: Optional[Callable] = None,
    finitargs: Optional[Tuple] = None,
    mem_clear_threshold_bytes: Optional[int] = None,
):
    # This event is used to signal that the "parent"
    # monitor thread should be deactivated.
    evt = threading.Event()

    def self_destruct_if_parent_disappers():
        """Poll every 5 seconds to see whether the parent is still
        alive.
        """
        while True:
            if evt.wait(2.0):
                return

            if not psutil.pid_exists(parent_pid):
                logger.warning(f"Parent {parent_pid} is gone, self-destructing.")
                evt.set()
                kill_proc_tree(pid, sig=signal.SIGKILL, allow_kill_self=True)
                return

    tparent = threading.Thread(target=self_destruct_if_parent_disappers, daemon=True)
    tparent.start()

    def deactivate_parentless_self_destruct():
        evt.set()

    proc = psutil.Process()
    pid = proc.pid

    def conn_send_safe(obj):
        try:
            conn.send(obj)
        except BrokenPipeError:  # pragma: no cover
            logger.debug("Pipe not usable")
        except BaseException:
            logger.exception("Unexpected pipe error")

    def timed_out():
        """Action to fire when the timeout given to ``threading.Timer``
        is reached. It kills this process with SIGKILL."""
        # First things first. Set a self-destruct timer for ourselves.
        # If we don't finish up in time, boom.
        deactivate_parentless_self_destruct()
        conn_send_safe(TimeoutError(f"Process {pid} timed out, self-destructing."))
        # kill_proc_tree_in_process_daemon(pid, signal.SIGKILL)
        kill_proc_tree(pid, sig=signal.SIGKILL, allow_kill_self=True)

    if initializer:
        initargs = initargs or ()
        try:
            initializer(*initargs)
        except Exception:
            logger.exception("Initializer failed")

    while True:
        # Wait for some work.
        try:
            job = conn_receiver.recv()
        except EOFError:
            logger.debug("Received EOF, exiting.")
            break
        except KeyboardInterrupt:
            logger.debug("Received KeyboardInterrupt, exiting.")
            break
        except BaseException:
            logger.exception("Received unexpected exception, exiting.")
            break

        if job is None:
            logger.debug("Received None, exiting.")
            break

        # Real work, unpack.
        fn, args, kwargs, timeout = job

        if timeout:
            t = threading.Timer(timeout, timed_out)
            t.start()
            deactivate_timer = lambda: t.cancel()  # noqa: E731
        else:
            deactivate_timer = lambda: None  # noqa: E731

        try:
            results = fn(*args, **kwargs)
        except BaseException as e:
            # Check whether the exception can be pickled. If not we're going
            # to wrap it. Why do this? It turns out that mp.Connection.send
            # will try to pickle the exception, and if it can't, it will
            # lose its mind. I've gotten segfaults in Python with this.
            try:  # pragma: no cover
                pickle.dumps(e)
            except Exception as pickle_error:
                msg = (
                    f"An exception occurred but pickling it failed. "
                    f"The original exception is presented here as a string with "
                    f"traceback.\n{e}\n{traceback.format_exception(e)}\n\n"
                    f"The reason for the pickking failure is the following:\n"
                    f"{traceback.format_exception(pickle_error)}"
                )
                e = ProcessError(msg)
            conn_send_safe(e)
        else:
            conn_send_safe(results)
        finally:
            deactivate_timer()

            if mem_clear_threshold_bytes is not None:
                mem = proc.memory_info().rss
                if mem > mem_clear_threshold_bytes:
                    trim_memory()

    if finitializer:
        finitargs = finitargs or ()
        try:
            finitializer(*finitargs)
        except BaseException:
            logger.exception("finitializer failed")

    # We've reached the end of this function which means this
    # process must exit. However, we started a couple threads
    # in here and they don't magically exit. Additional
    # synchronization controls are needed to tell the threads
    # to exit, which we don't have. However, we do have a kill
    # switch. Since this process worker will process no more
    # work, and since we've already fun the finalizer, we may
    # as well just nuke it. That will remove its memory space
    # and all its threads too.
    deactivate_parentless_self_destruct()
    logger.debug(f"Deleting worker {pid=}")
    kill_proc_tree(pid, sig=signal.SIGKILL, allow_kill_self=True)


def kill_proc_tree_in_process_daemon(pid, sig):  # pragma: no cover
    mp.Process(target=kill_proc_tree, args=(pid, sig), daemon=True).start()


# Taken from
# https://psutil.readthedocs.io/en/latest/index.html?highlight=children#kill-process-tree
def kill_proc_tree(
    pid,
    sig=signal.SIGTERM,
    include_parent=True,
    timeout=None,
    on_terminate=None,
    allow_kill_self=False,
):  # pragma: no cover
    """Kill a process tree (including grandchildren) with signal
    "sig" and return a (gone, still_alive) tuple.
    "on_terminate", if specified, is a callback function which is
    called as soon as a child terminates.
    """
    if not allow_kill_self and pid == os.getpid():
        raise ValueError("Won't kill myself")

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    children = parent.children(recursive=True)
    if include_parent:
        children.append(parent)

    for p in children:
        try:
            p.send_signal(sig)
        except psutil.NoSuchProcess:
            pass

    gone, alive = psutil.wait_procs(children, timeout=timeout, callback=on_terminate)
    return (gone, alive)


def trim_memory() -> None:
    """Tell malloc to give all the unused memory back to the OS."""
    if sys.platform == "linux":
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
