import psutil

import deadpool
from deadpool import Deadpool


def worker(mb):
    list = [1] * mb * 1024 * 1024

    # Use psutil to check our own memory usage. If greater than
    # 500 MB, sigkill ourselves
    if psutil.Process().memory_info().rss > 500 * 1024 * 1024:
        import os

        print("bad")
        os.kill(os.getpid(), 9)

    print("ok")
    return True


def test_oom():
    """Verify that jobs continue to run even if some of them OOM

    The terminal output will look something like this:

        ok
        ok
        bad
           WARNING      19773 Thread-96 (run_task) BrokenPipeError on 1114111, retrying. [deadpool.py:449]
        ok
        ok
        ok
        ok
        ok
        bad
           WARNING      22602 Thread-102 (run_task) BrokenPipeError on 1114145, retrying. [deadpool.py:449]
        ok
        ok
        bad
           WARNING      25418 Thread-105 (run_task) BrokenPipeError on 1114155, retrying. [deadpool.py:449]
        ok
        bad
        ok
        ok

    """
    import random

    tasks = [random.randint(1, 10) for _ in range(100)]

    too_big_tasks = [500 for _ in range(10)]

    all_tasks = tasks + too_big_tasks
    random.shuffle(all_tasks)

    # Check that we can get through all the work
    results = []
    with Deadpool(max_workers=1) as exe:
        for task in all_tasks:
            futs = exe.submit(worker, task)
            results.append(futs)

        count = 0
        failed = 0
        for fut in results:
            try:
                if fut.result() == True:
                    count += 1
            except deadpool.ProcessError:
                failed += 1

    print(f"count: {count}, failed: {failed}")

    assert count == len(tasks)
    assert failed == len(too_big_tasks)
