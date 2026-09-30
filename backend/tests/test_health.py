import ipaddress
import socket

from fastapi.testclient import TestClient


def test_live_does_not_call_github(monkeypatch, settings_factory):
    from devflow.app import create_app

    github_calls = []
    original_connect = socket.socket.connect

    def record_outbound(sock, address):
        if not ipaddress.ip_address(address[0]).is_loopback:
            github_calls.append(address)
            raise AssertionError(f"Unexpected network request to {address}")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", record_outbound)
    try:
        with TestClient(create_app(settings_factory())) as client:
            response = client.get("/health/live")
    finally:
        monkeypatch.setattr(socket.socket, "connect", original_connect)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert github_calls == []


def test_request_id_is_unique_per_request(settings_factory):
    from devflow.app import create_app

    with TestClient(create_app(settings_factory())) as client:
        first = client.get("/health/live")
        second = client.get("/health/live")

    assert first.headers["x-request-id"]
    assert second.headers["x-request-id"]
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
