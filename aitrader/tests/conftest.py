"""Keep paper tests off the network and away from live credentials."""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_outbound(monkeypatch):
    def refused(*_args, **_kwargs):
        raise RuntimeError("outbound network is blocked in paper tests")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket.socket, "connect", refused)
