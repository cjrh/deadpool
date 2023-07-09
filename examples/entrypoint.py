import random
import time
import logging

import deadpool

logging.basicConfig(level="DEBUG")
logging.getLogger("deadpool").setLevel("DEBUG")


def init(*args, **kwargs):
    logging.basicConfig(level="DEBUG")
    logging.getLogger("deadpool").setLevel("DEBUG")


def work():
    time.sleep(random.random() * 4.0)
    print(".", end="", flush=True)
    return 1


def main():
    with deadpool.Deadpool(initializer=init) as exe:
        futs = (exe.submit(work, deadpool_timeout=2.0) for _ in range(50))
        for fut in deadpool.as_completed(futs):
            try:
                assert fut.result() == 1
            except deadpool.TimeoutError:
                print("x", end="", flush=True)


if __name__ == "__main__":
    main()
    print()
