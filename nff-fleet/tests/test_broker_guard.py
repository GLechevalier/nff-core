"""Unit tests for the zombie-instance startup guard in nff_fleet.broker.broker.

A stale fleet still holding the MQTT TLS port silently shadows a fresh instance. The guard
(`_listener_already_up`) must detect a live listener so `start_broker()` can abort loudly
instead of starting a half-dead broker.
"""
import socket

from nff_fleet.broker.broker import _listener_already_up


def test_free_port_reports_not_up():
    # Reserve a port, then release it so nothing is listening on it.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _listener_already_up(port) is False


def test_live_listener_reports_up():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert _listener_already_up(port) is True
    finally:
        listener.close()
