import deadpool
from multiprocessing import Queue, Manager
import time
import random
import logging

logging.basicConfig(level="DEBUG")
Executor = deadpool.Deadpool
manager = Manager()


def worker(q: Queue, val: int):
    time.sleep(val)
    print(".", end="")
    q.put(val)


def main():
    results = manager.Queue()
    exe = Executor(
        max_workers=10,
        min_workers=2,
        daemon=False,
        max_tasks_per_child=2,
    )
    with exe:
        for i in range(500):
            time.sleep(random.randrange(0, 10) / 10)
            w = random.randrange(0, 50) / 10
            exe.submit(worker, results, w, deadpool_timeout=4)
            print(f"{exe.workers.qsize()=} {len(exe.busy_workers)=}")

    outcome = []
    while not results.empty():
        outcome.append(results.get_nowait())

    print(outcome)
    print(f"{len(outcome)=}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
