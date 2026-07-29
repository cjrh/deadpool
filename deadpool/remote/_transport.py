"""Unix and TCP socket lifecycle shared by client and server."""

from __future__ import annotations

import os
import socket
import stat
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import PeerInfo, TcpAddress, TcpListener, UnixAddress, UnixListener


@dataclass(slots=True)
class BoundListener:
    config: UnixListener | TcpListener
    socket: socket.socket
    address: object
    unix_identity: tuple[int, int] | None = None

    def close(self) -> None:
        try:
            self.socket.close()
        finally:
            if isinstance(self.config, UnixListener) and self.unix_identity is not None:
                path = self.config.path
                try:
                    current = path.lstat()
                except FileNotFoundError:
                    current = None
                if current is not None:
                    _unlink_if_identity(path, current, self.unix_identity)


def bind_listener(config: UnixListener | TcpListener) -> BoundListener:
    if isinstance(config, UnixListener):
        return _bind_unix(config)
    return _bind_tcp(config)


def connect_address(address: UnixAddress | TcpAddress) -> socket.socket:
    if isinstance(address, UnixAddress):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(address.connect_timeout)
        try:
            sock.connect(str(address.path))
        except BaseException:
            sock.close()
            raise
        return sock
    sock = socket.create_connection(
        (address.host, address.port), timeout=address.connect_timeout
    )
    if address.ssl_context is not None:
        hostname = address.server_hostname or address.host
        try:
            sock = address.ssl_context.wrap_socket(sock, server_hostname=hostname)
        except BaseException:
            sock.close()
            raise
    return sock


def peer_info(sock: socket.socket, transport: str) -> PeerInfo:
    uid = gid = pid = None
    if transport == "unix" and hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        pid, uid, gid = struct.unpack("3i", raw)
    certificate = None
    if hasattr(sock, "getpeercert"):
        try:
            certificate = sock.getpeercert() or None
        except (ValueError, OSError):
            pass
    try:
        address = sock.getpeername()
    except OSError:
        address = None
    return PeerInfo(transport, address, uid, gid, pid, certificate)


def _bind_unix(config: UnixListener) -> BoundListener:
    path = config.path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _validate_unix_directory(path.parent)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
            raise ValueError(f"refusing unexpected Unix socket path type: {path}")
        if config.stale_policy != "force_unlink":
            raise FileExistsError(path)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
            try:
                current = path.lstat()
            except FileNotFoundError:
                pass
            else:
                _unlink_if_identity(path, current, (info.st_dev, info.st_ino))
        else:
            raise OSError(f"Unix socket is already live: {path}")
        finally:
            probe.close()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity: tuple[int, int] | None = None
    try:
        with _umask(0o077):
            sock.bind(str(path))
        bound = path.lstat()
        identity = (bound.st_dev, bound.st_ino)
        if config.owner_uid is not None or config.owner_gid is not None:
            os.chown(
                path,
                config.owner_uid if config.owner_uid is not None else -1,
                config.owner_gid if config.owner_gid is not None else -1,
            )
        os.chmod(path, config.mode)
        sock.listen(config.backlog)
        return BoundListener(config, sock, path, identity)
    except BaseException:
        sock.close()
        if identity is not None:
            try:
                current = path.lstat()
            except FileNotFoundError:
                pass
            else:
                _unlink_if_identity(path, current, identity)
        raise


def _unlink_if_identity(
    path: Path, current: os.stat_result, identity: tuple[int, int]
) -> None:
    """Compare identity immediately before unlinking a Unix listener path.

    This protects replacements visible to the final ``lstat``. A portable
    ``lstat``/``unlink`` sequence still has an unavoidable TOCTOU window.
    """
    if (current.st_dev, current.st_ino) == identity:
        path.unlink()


def _validate_unix_directory(path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"Unix socket directory must be a real directory: {path}")
    if info.st_mode & 0o022:
        raise PermissionError(
            f"Unix socket directory must not be group/world writable: {path}"
        )
    if info.st_uid not in {0, os.getuid()}:
        raise PermissionError(f"Unix socket directory has an untrusted owner: {path}")


def _bind_tcp(config: TcpListener) -> BoundListener:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if config.keepalive:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.bind((config.host, config.port))
        sock.listen(config.backlog)
        return BoundListener(config, sock, sock.getsockname())
    except BaseException:
        sock.close()
        raise


def accept_socket(bound: BoundListener) -> socket.socket:
    """Accept only the raw socket; potentially slow TLS runs in a client thread."""
    sock, _ = bound.socket.accept()
    return sock


def prepare_accepted_socket(
    bound: BoundListener,
    sock: socket.socket,
    *,
    handshake_timeout: float,
) -> socket.socket:
    """Complete transport authentication under a finite deadline."""
    config = bound.config
    sock.settimeout(handshake_timeout)
    if isinstance(config, TcpListener) and config.ssl_context is not None:
        try:
            sock = config.ssl_context.wrap_socket(sock, server_side=True)
        except BaseException:
            sock.close()
            raise
    return sock


@contextmanager
def _umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)
