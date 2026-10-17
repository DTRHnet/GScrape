"""Local network resource helpers."""

from __future__ import annotations

import socket
from contextlib import closing


def allocate_loopback_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an available loopback TCP port.

    The caller must bind the returned port immediately. This helper is for
    short-lived standalone OAuth listeners; the dashboard should prefer one
    shared callback listener and session-state validation.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])