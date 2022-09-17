"""
Deadpool
========


"""
import os
import signal
import multiprocessing as mp
from multiprocessing.connection import Connection
import concurrent.futures
from concurrent.futures import Executor, Future as CFFuture, TimeoutError as CFTimeoutError
import threading
import typing
from queue import Queue, Empty
from typing import Callable, Optional

import psutil


__version__ = "2022.9.1"


class Future(concurrent.futures.Future):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pid: Optional[int] = None


class TimeoutError(concurrent.futures.TimeoutError):
    ...


class ProcessError(mp.ProcessError):
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
            mp_context = 'forkserver'

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

    def runner(self):
        while job := self.submitted_jobs.get():
            # Need permission from the pool size to proceed
            self.running_jobs.put(None)
            t = threading.Thread(
                target=self.run_process,
                args=job,
            )
            t.start()

        # This is for the `None` that terminates the while loop.
        self.submitted_jobs.task_done()

    def run_process(self, fn, args, kwargs, timeout, fut: Future):
        try:
            conn_sender, conn_receiver = mp.Pipe()
            p = self.ctx.Process(
                target=raw_runner,
                args=(
                    conn_sender,
                    fn,
                    args,
                    kwargs,
                    self.initializer,
                    self.initargs,
                    self.finitializer,
                    self.finitargs,
                ),
            )
            p.start()
            fut.pid = p.pid

            def timed_out():
                print('timed out, killing process')
                kill_proc_tree(p.pid, sig=signal.SIGKILL)
                conn_sender.send(TimeoutError())

            t = threading.Timer(timeout or 1800, timed_out)
            t.start()

            while True:
                if conn_receiver.poll(0.2):
                    results = conn_receiver.recv()
                    t.cancel()
                    conn_receiver.close()
                    break
                elif not p.is_alive():
                    try:
                        signame = signal.strsignal(-p.exitcode)
                    except ValueError:  # pragma: no cover
                        signame = "Unknown"

                    msg = (
                        f"Subprocess {p.pid} completed unexpectedly with exitcode {p.exitcode} "
                        f"({signame})"
                    )
                    # Loop will read this data into conn_received on
                    # next pass.
                    print(msg)
                    conn_sender.send(ProcessError(msg))

            if isinstance(results, BaseException):
                fut.set_exception(results)
            else:
                fut.set_result(results)

            p.join()
        finally:
            self.submitted_jobs.task_done()
            try:
                self.running_jobs.get_nowait()
            except Empty:  # pragma: no cover
                print(f"Weird error, did not expect running jobs to be empty")

    def submit(self, __fn: Callable, *args, timeout=None, **kwargs) -> Future:
        fut = Future()
        self.submitted_jobs.put((__fn, args, kwargs, timeout, fut))
        return fut

    def shutdown(self, wait: bool = ..., *, cancel_futures: bool = ...) -> None:
        self.submitted_jobs.put_nowait(None)
        self.submitted_jobs.join()
        return super().shutdown(wait, cancel_futures=cancel_futures)

    def __enter__(self):
        self.runner_thread = threading.Thread(target=self.runner)
        self.runner_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown(wait=True)
        self.runner_thread.join()
        return False


def raw_runner(conn: Connection, fn, args, kwargs, initializer, initargs, finitializer, finitargs):
    if initializer:
        try:
            initializer(*initargs)
        except:
            print(f"Initializer failed")

    try:
        results = fn(*args, **kwargs)
    except BaseException as e:
        conn.send(e)
    else:
        conn.send(results)
    finally:
        conn.close()

        if finitializer:
            try:
                finitializer(*finitargs)
            except:
                print(f"Finitializer failed")


# Taken fromhttps
# https://psutil.readthedocs.io/en/latest/index.html?highlight=children#kill-process-tree
def kill_proc_tree(pid, sig=signal.SIGTERM, include_parent=True,
                   timeout=None, on_terminate=None):  # pragma: no cover
    """Kill a process tree (including grandchildren) with signal
    "sig" and return a (gone, still_alive) tuple.
    "on_terminate", if specified, is a callback function which is
    called as soon as a child terminates.
    """
    if pid == os.getpid():
        raise ValueError("won't kill myself")

    parent = psutil.Process(pid)
    children = parent.children(recursive=True)
    if include_parent:
        children.append(parent)

    for p in children:
        try:
            p.send_signal(sig)
        except psutil.NoSuchProcess:
            pass

    gone, alive = psutil.wait_procs(children, timeout=timeout,
                                    callback=on_terminate)
    return (gone, alive)
