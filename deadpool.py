"""
Deadpool
========


"""
import os
import time
import signal
import multiprocessing as mp
from multiprocessing.connection import Connection
import concurrent.futures
from concurrent.futures import Executor
import threading
from queue import Queue, Empty
from typing import Callable, Optional
import logging

import psutil


__version__ = "2022.9.3"
logger = logging.getLogger(__name__)


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
                self.pid_callback(self._pid)
            except Exception:  # pragma: no cover
                logger.exception(f"Error calling pid_callback")

    def add_pid_callback(self, fn):
        self.pid_callback = fn


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
        self.submitted_jobs = Queue(maxsize=100)
        self.running_jobs = Queue(maxsize=self.pool_size)
        self.closed = False

        # THE ONLY ACTIVE, PERSISTENT STATE IN DEADPOOL IS THIS THREAD
        # BELOW. PROTECT IT AT ALL COSTS.
        self.runner_thread = threading.Thread(target=self.runner, daemon=True)
        self.runner_thread.start()

    def runner(self):
        while job := self.submitted_jobs.get():
            # This will block if the queue of running jobs is max size.
            self.running_jobs.put(None)
            t = threading.Thread(target=self.run_process, args=job)
            t.start()

        # This is for the `None` that terminates the while loop.
        self.submitted_jobs.task_done()

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

                    msg = (
                        f"Subprocess {p.pid} completed unexpectedly with exitcode {p.exitcode} "
                        f"({signame})"
                    )
                    fut.set_exception(ProcessError(msg))
                    break
                else:
                    pass

            p.join()
        finally:
            if not fut.done():  # pragma: no cover
                fut.set_exception(ProcessError("Somehow no result got set on fut."))

            self.submitted_jobs.task_done()
            try:
                self.running_jobs.get_nowait()
            except Empty:  # pragma: no cover
                logger.warning(f"Weird error, did not expect running jobs to be empty")

    def submit(self, __fn: Callable, *args, timeout=None, **kwargs) -> Future:
        if self.closed:
            raise PoolClosed("The pool is closed. No more tasks can be submitted.")

        fut = Future()
        self.submitted_jobs.put((__fn, args, kwargs, timeout, fut))
        return fut

    def shutdown(self, wait: bool = ..., *, cancel_futures: bool = ...) -> None:
        logger.debug("in shutdown")
        self.closed = True
        self.submitted_jobs.put_nowait(None)
        if wait:
            logger.debug("waiting for submitted_jobs to join...")
            self.submitted_jobs.join()
            logger.debug("submitted_jobs joined.")
        return super().shutdown(wait, cancel_futures=cancel_futures)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown(wait=True)
        self.runner_thread.join()
        return False


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
            logger.error("Pipe not usable")
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
            logger.warning("sig: %s %s", p, sig)
            p.send_signal(sig)
        except psutil.NoSuchProcess:
            pass

    gone, alive = psutil.wait_procs(children, timeout=timeout, callback=on_terminate)
    return (gone, alive)
