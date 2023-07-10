import deadpool
from multiprocessing import Queue, Manager
import time
import random

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
    )
    with exe:
        for i in range(1000):
            time.sleep(random.randrange(0, 10) / 10)
            w = random.randrange(0, 20) / 10
            exe.submit(worker, results, 1)
            print(f"{exe.workers.qsize()=} {len(exe.busy_workers)=}")

    outcome = []
    while not results.empty():
        outcome.append(results.get_nowait())

    print(outcome)


if __name__ == "__main__":
    main()

