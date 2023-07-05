import random
import time

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
