"""Importable registered tasks used by forkserver worker tests."""

import os
import time
from pathlib import Path


def multiply(left: int, right: int) -> int:
    return left * right


def delayed(value: object, delay: float) -> object:
    time.sleep(delay)
    return value


def make_bytes(size: int) -> bytes:
    return b"x" * size


def echo_bytes(value: bytes) -> bytes:
    return value


def exit_abruptly(code: int = 17) -> None:
    """Terminate the current worker without Python-level cleanup."""
    os._exit(code)


def mark(path: str | Path, value: str = "ran") -> str:
    Path(path).write_text(value)
    return value


def wait_then_mark(
    release_path: str | Path, marker_path: str | Path, value: str
) -> str:
    release = Path(release_path)
    while not release.exists():
        time.sleep(0.005)
    Path(marker_path).write_text(value)
    return value


def append_line(path: str | Path, value: str) -> str:
    with Path(path).open("a") as stream:
        stream.write(f"{value}\n")
    return value
