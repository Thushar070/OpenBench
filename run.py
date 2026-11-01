"""Start the local OpenBench API and dashboard as one supervised process."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parent
API_HOST = "127.0.0.1"
API_PORT = 8000
WEB_HOST = "127.0.0.1"
WEB_PORT = 3000
EXPECTED_GENERATION = "recovery-20261101T044109.101917Z"
STARTUP_TIMEOUT_SECONDS = 60
SHUTDOWN_TIMEOUT_SECONDS = 10
DEFAULT_API_URL = f"http://{API_HOST}:{API_PORT}"


class StartupError(RuntimeError):
    """A required environment, artifact, or local service check failed."""


def _python_executable(root: Path) -> str:
    virtualenv_python = root / ".venv" / "Scripts" / "python.exe"
    return str(virtualenv_python) if virtualenv_python.is_file() else sys.executable


def _project_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(root / "src")
    existing_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (source_path, existing_path) if path
    )
    return environment


def _check_python_environment(root: Path) -> str:
    python = _python_executable(root)
    probe = "\n".join(
        (
            "import importlib.util",
            "import json",
            "import sys",
            "modules = (",
            "    'fastapi', 'pydantic', 'uvicorn', 'torch', 'numpy',",
            "    'pandas', 'sklearn',",
            ")",
            "missing = [",
            "    name for name in modules",
            "    if importlib.util.find_spec(name) is None",
            "]",
            "print(json.dumps({",
            "    'version': list(sys.version_info[:2]),",
            "    'missing': missing,",
            "}))",
        )
    )
    try:
        result = subprocess.run(
            [python, "-c", probe],
            cwd=root,
            env=_project_environment(root),
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise StartupError(
            f"Could not inspect the project Python environment: {error}"
        ) from error
    if result.returncode != 0:
        message = (
            result.stderr.strip() or result.stdout.strip() or "Python probe failed."
        )
        raise StartupError(message)
    try:
        environment = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise StartupError(
            "Python environment check returned invalid output."
        ) from error

    version = environment.get("version")
    if version != [3, 15]:
        raise StartupError(
            "OpenBench requires Python 3.15; the selected interpreter reports "
            f"{version!r}. Activate the existing project virtual environment."
        )
    missing = environment.get("missing", [])
    if missing:
        raise StartupError(
            "The project Python environment is missing required packages: "
            + ", ".join(missing)
        )
    return python


def _verify_active_generation(root: Path, python: str) -> None:
    check = (
        "import sys; from pathlib import Path; "
        "from deeptext.experiments.phase_16_quality import discover_active_generation; "
        "active = discover_active_generation(Path.cwd()); "
        "print(active['generation_id'])"
    )
    try:
        result = subprocess.run(
            [python, "-c", check],
            cwd=root,
            env=_project_environment(root),
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise StartupError(
            f"Could not verify the active model generation: {error}"
        ) from error
    if result.returncode != 0:
        message = (
            result.stderr.strip() or result.stdout.strip() or "Artifact check failed."
        )
        raise StartupError(f"Active model artifacts are not ready: {message}")
    generation = result.stdout.strip()
    if generation != EXPECTED_GENERATION:
        raise StartupError(
            f"Expected frozen generation {EXPECTED_GENERATION}, found {generation!r}."
        )


def _check_frontend(root: Path) -> tuple[str, Path]:
    node = shutil.which("node")
    if node is None:
        raise StartupError("Node.js was not found on PATH.")
    frontend = root / "frontend"
    next_cli = frontend / "node_modules" / "next" / "dist" / "bin" / "next"
    if not next_cli.is_file():
        raise StartupError(
            f"The frontend dependencies are unavailable: {next_cli} does not exist."
        )

    configured_api = os.environ.get("NEXT_PUBLIC_OPENBENCH_API_URL", DEFAULT_API_URL)
    if configured_api.rstrip("/") not in {
        DEFAULT_API_URL,
        f"http://localhost:{API_PORT}",
    }:
        raise StartupError(
            "OpenBench is local-only. NEXT_PUBLIC_OPENBENCH_API_URL must point "
            f"to {DEFAULT_API_URL} or http://localhost:{API_PORT}."
        )
    return node, next_cli


def _assert_port_available(host: str, port: int, service: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((host, port))
        except OSError as error:
            raise StartupError(
                f"Cannot start {service}: {host}:{port} is already in use. "
                "Close the conflicting local service and try again."
            ) from error


def _start_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    popen_factory: Any = subprocess.Popen,
) -> Any:
    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True
    return popen_factory(
        command,
        cwd=cwd,
        env=environment,
        **process_options,
    )


def _wait_for_url(
    service: str,
    url: str,
    processes: dict[str, Any],
    *,
    timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        for name, process in processes.items():
            exit_code = process.poll()
            if exit_code is not None:
                raise StartupError(
                    f"{name} exited during startup with code {exit_code}."
                )
        try:
            with urlopen(url, timeout=2) as response:
                if 200 <= response.status < 400:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, URLError, TimeoutError) as error:
            last_error = str(error)
        time.sleep(0.25)
    raise StartupError(
        f"{service} did not become ready at {url} within {timeout:.0f} seconds "
        f"({last_error})."
    )


def _stop_process(process: Any, name: str) -> None:
    if process.poll() is not None:
        return
    try:
        stop_signal = (
            getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
            if os.name == "nt"
            else signal.SIGINT
        )
        process.send_signal(stop_signal)
    except OSError, ValueError:
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"{name} did not stop cleanly; terminating it.", flush=True)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _stop_services(processes: dict[str, Any]) -> None:
    for name, process in reversed(tuple(processes.items())):
        _stop_process(process, name)


def main() -> int:
    processes: dict[str, Any] = {}
    try:
        python = _check_python_environment(PROJECT_ROOT)
        node, next_cli = _check_frontend(PROJECT_ROOT)
        _verify_active_generation(PROJECT_ROOT, python)
        _assert_port_available(API_HOST, API_PORT, "FastAPI")
        _assert_port_available(WEB_HOST, WEB_PORT, "Next.js")

        backend_environment = _project_environment(PROJECT_ROOT)
        backend_command = [
            python,
            "-m",
            "uvicorn",
            "deeptext.dashboard.api:app",
            "--app-dir",
            str(PROJECT_ROOT / "src"),
            "--host",
            API_HOST,
            "--port",
            str(API_PORT),
        ]
        processes["FastAPI"] = _start_process(
            backend_command,
            cwd=PROJECT_ROOT,
            environment=backend_environment,
        )
        _wait_for_url(
            "FastAPI readiness check",
            f"{DEFAULT_API_URL}/api/ready",
            processes,
        )

        frontend_environment = os.environ.copy()
        frontend_environment["NEXT_PUBLIC_OPENBENCH_API_URL"] = os.environ.get(
            "NEXT_PUBLIC_OPENBENCH_API_URL", DEFAULT_API_URL
        ).rstrip("/")
        frontend_environment["NEXT_TELEMETRY_DISABLED"] = "1"
        frontend_command = [
            node,
            str(next_cli),
            "dev",
            "--hostname",
            WEB_HOST,
            "--port",
            str(WEB_PORT),
        ]
        processes["Next.js"] = _start_process(
            frontend_command,
            cwd=PROJECT_ROOT / "frontend",
            environment=frontend_environment,
        )
        dashboard_url = f"http://{WEB_HOST}:{WEB_PORT}"
        _wait_for_url("Next.js dashboard", dashboard_url, processes)
        print(f"OpenBench is ready at {dashboard_url}", flush=True)
        print("Press Ctrl+C to stop both local services.", flush=True)

        while True:
            for name, process in processes.items():
                exit_code = process.poll()
                if exit_code is not None:
                    raise StartupError(
                        f"{name} exited unexpectedly with code {exit_code}."
                    )
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping OpenBench services...", flush=True)
        return_code = 0
    except StartupError as error:
        print(f"OpenBench startup failed: {error}", file=sys.stderr, flush=True)
        return_code = 1
    finally:
        _stop_services(processes)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
