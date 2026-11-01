from __future__ import annotations

import socket
import subprocess

import pytest
import run


def test_port_check_reports_an_existing_local_service() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((run.API_HOST, 0))
        port = listener.getsockname()[1]

        with pytest.raises(run.StartupError, match="already in use"):
            run._assert_port_available(run.API_HOST, port, "test service")


def test_readiness_wait_reports_a_service_that_exits_early() -> None:
    class ExitedProcess:
        @staticmethod
        def poll() -> int:
            return 2

    with pytest.raises(run.StartupError, match="FastAPI exited during startup"):
        run._wait_for_url(
            "API readiness check",
            "http://127.0.0.1:8000/api/ready",
            {"FastAPI": ExitedProcess()},
            timeout=0.1,
        )


def test_wait_for_url_accepts_a_healthy_local_response(monkeypatch) -> None:
    class HealthyResponse:
        status = 200

        def __enter__(self) -> HealthyResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(run, "urlopen", lambda *_args, **_kwargs: HealthyResponse())
    run._wait_for_url("dashboard", "http://127.0.0.1:3000", {})


def test_stop_process_terminates_after_graceful_shutdown_timeout() -> None:
    class SlowProcess:
        def __init__(self) -> None:
            self.running = True
            self.signal_sent = False
            self.terminated = False
            self.wait_count = 0

        def poll(self) -> int | None:
            return None if self.running else 0

        def send_signal(self, _signal: int) -> None:
            self.signal_sent = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_count += 1
            if self.wait_count == 1:
                raise subprocess.TimeoutExpired("test", timeout)
            return 0

        def terminate(self) -> None:
            self.terminated = True
            self.running = False

        def kill(self) -> None:
            self.running = False

    process = SlowProcess()
    run._stop_process(process, "test service")

    assert process.signal_sent is True
    assert process.terminated is True


def test_frontend_rejects_a_nonlocal_api_origin(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run.shutil, "which", lambda _name: "node")
    monkeypatch.setenv("NEXT_PUBLIC_OPENBENCH_API_URL", "https://example.com")
    next_cli = tmp_path / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next"
    next_cli.parent.mkdir(parents=True)
    next_cli.write_text("", encoding="utf-8")

    with pytest.raises(run.StartupError, match="local-only"):
        run._check_frontend(tmp_path)
