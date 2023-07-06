import random
import time

import deadpool


def work(symbol):
    time.sleep(random.random() * 4.0)
    print(symbol, end="", flush=True)
    return 1


def main():
    with deadpool.Deadpool(max_backlog=100) as exe:
        futs = []
        for _ in range(25):
            fut = exe.submit(work, ".", deadpool_timeout=2.0, deadpool_priority=10)
            futs.append(fut)
            fut = exe.submit(work, "!", deadpool_timeout=2.0, deadpool_priority=0)
            futs.append(fut)

        for fut in deadpool.as_completed(futs):
            try:
                assert fut.result() == 1
            except deadpool.TimeoutError:
                print("x", end="", flush=True)


if __name__ == "__main__":
    main()
    print()
