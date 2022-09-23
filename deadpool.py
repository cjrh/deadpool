"""
Deadpool
========


"""
import os
import sys
import signal
import multiprocessing as mp
from multiprocessing.connection import Connection
import concurrent.futures
from concurrent.futures import Executor, CancelledError, as_completed, InvalidStateError
import threading
from queue import Queue, Empty, PriorityQueue
from typing import Callable, Optional
import logging
import typing
import weakref
from dataclasses import dataclass, field

import psutil


__version__ = "2022.9.5"
__all__ = [
    "Deadpool",
    "Future",
    "CancelledError",
    "TimeoutError",
    "ProcessError",
    "PoolClosed",
    "as_completed",
]
logger = logging.getLogger(__name__)


@dataclass(order=True)
class PrioritizedItem:
    priority: int
    item: typing.Any = field(compare=False)


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
                logger.exception(f"Error calling pid_callback")

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
        mp_context=None,
        initializer=None,
        initargs=(),
        finalizer=None,
        finalargs=(),
        max_backlog=5,
        shutdown_wait: Optional[bool] = None,
        shutdown_cancel_futures: Optional[bool] = None,
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
        self.submitted_jobs: PriorityQueue[PrioritizedItem] = PriorityQueue(
            maxsize=max_backlog
        )
        self.running_jobs = Queue(maxsize=self.pool_size)
        self.running_futs = weakref.WeakSet()
        self.closed = False
        self.shutdown_wait = shutdown_wait
        self.shutdown_cancel_futures = shutdown_cancel_futures

        # THE ONLY ACTIVE, PERSISTENT STATE IN DEADPOOL IS THIS THREAD
        # BELOW. PROTECT IT AT ALL COSTS.
        self.runner_thread = threading.Thread(
            target=self.runner, name="deadpool.runner", daemon=True
        )
        self.runner_thread.start()

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
                logger.debug(f"Got shutdown event, leaving runner.")
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

            t = threading.Thread(target=self.run_process, args=job)
            t.start()

    def run_process(self, fn, args, kwargs, timeout, fut: Future):
        try:
            conn_receiver, conn_sender = mp.Pipe(duplex=False)
            p = self.ctx.Process(
                target=raw_runner,
                args=(
                    conn_sender,
                    fn,
                    args,
                    kwargs,
                    timeout,
                    os.getpid(),
                    self.initializer,
                    self.initargs,
                    self.finitializer,
                    self.finitargs,
                ),
            )
            p.start()
            fut.pid = p.pid
            self.running_futs.add(fut)

            while True:
                if conn_receiver.poll(0.2):
                    try:
                        results = conn_receiver.recv()
                    except BaseException as e:
                        fut.set_exception(e)
                    else:
                        if isinstance(results, BaseException):
                            fut.set_exception(results)
                        else:
                            fut.set_result(results)
                    finally:
                        try:
                            conn_receiver.close()
                        finally:
                            break
                elif not p.is_alive():
                    logger.debug(f"p is no longer alive: {p}")
                    try:
                        signame = signal.strsignal(-p.exitcode)
                    except ValueError:  # pragma: no cover
                        signame = "Unknown"

                    if not fut.done():
                        # It is possible that fut has already had a result set on
                        # it. If that's the case we'll do nothing. Otherwise, put
                        # an exception reporting the unexpected situation.
                        msg = (
                            f"Subprocess {p.pid} completed unexpectedly with exitcode {p.exitcode} "
                            f"({signame})"
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

            p.join()
        finally:
            self.submitted_jobs.task_done()

            if not fut.done():  # pragma: no cover
                fut.set_exception(ProcessError("Somehow no result got set on fut."))

            try:
                self.running_jobs.get_nowait()
            except Empty:  # pragma: no cover
                logger.warning(f"Weird error, did not expect running jobs to be empty")

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
                "Timed out putting None on the submit queue. This should not be possible "
                " and might be a bug in deadpool."
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
                try:
                    if fut.pid:
                        kill_proc_tree(fut.pid)
                except Exception as e:
                    logger.warning(f"Got error cancelling {fut.pid=}: {e}")
                finally:
                    fut.cancel()

        logger.debug("waiting for submitted_jobs to join...")
        self.submitted_jobs.join()

        return super().shutdown(wait, cancel_futures=cancel_futures)

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


def raw_runner(
    conn: Connection,
    fn,
    args,
    kwargs,
    timeout,
    parent_pid,
    initializer,
    initargs,
    finitializer,
    finitargs,
):
    pid = os.getpid()
    lock = threading.Lock()

    def conn_send_safe(obj):
        try:
            with lock:
                conn.send(obj)
        except BrokenPipeError:  # pragma: no cover
            logger.debug("Pipe not usable")
        except:
            logger.exception("Unexpected pipe error")

    def timed_out():
        # First things first. Set a self-destruct timer for ourselves.
        # If we don't finish up in time, boom.
        conn_send_safe(TimeoutError(f"Process {pid} timed out, self-destructing."))
        # kill_proc_tree_in_process_daemon(pid, signal.SIGKILL)
        kill_proc_tree(pid, sig=signal.SIGKILL, allow_kill_self=True)

    if timeout:
        t = threading.Timer(timeout, timed_out)
        t.start()
        deactivate_timer = lambda: t.cancel()
    else:
        deactivate_timer = lambda: None

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
                # kill_proc_tree_in_process_daemon(pid, signal.SIGKILL)
                kill_proc_tree(pid, sig=signal.SIGKILL, allow_kill_self=True)
                return

    tparent = threading.Thread(target=self_destruct_if_parent_disappers, daemon=True)
    tparent.start()
    deactivate_parentless_self_destruct = lambda: evt.set()

    if initializer:
        try:
            initializer(*initargs)
        except:
            logger.exception(f"Initializer failed")

    try:
        results = fn(*args, **kwargs)
    except BaseException as e:
        conn_send_safe(e)
    else:
        conn_send_safe(results)
    finally:
        deactivate_timer()
        deactivate_parentless_self_destruct()

        try:
            conn.close()
        except BrokenPipeError:  # pragma: no cover
            logger.error("Pipe not usable")

        if finitializer:
            try:
                finitializer(*finitargs)
            except:
                logger.exception(f"Finitializer failed")


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
