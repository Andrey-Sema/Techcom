"""Supervise a fresh loopback receiver and the real installed CLI.

The prebound socket establishes ownership of port 8081. A fresh journal
prevents prior demo receipts from changing the output. No Docker, real
provider credentials or external endpoints are needed.
"""

import os
import shutil
import socket
import subprocess  # nosec B404 # Fixed argv; no shell.
import sys
import tempfile
import time
from http import HTTPStatus
from pathlib import Path

import httpx


def _ready(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                response = client.get("http://127.0.0.1:8081/v1/tasks")
            except httpx.TransportError:
                pass
            else:
                if response.status_code == HTTPStatus.OK:
                    return
            time.sleep(0.1)
    message = "Demo receiver did not become ready; see var/demo/mock.log"
    raise RuntimeError(message)


def main() -> None:
    """Release this invocation's process and socket on every exit."""
    output = Path("var/demo")
    output.mkdir(parents=True, exist_ok=True)
    with (
        socket.socket() as listener,
        tempfile.TemporaryDirectory(prefix="techcom-demo-") as directory,
        (output / "mock.log").open("wb") as logfile,
    ):
        (output / "mock.log").chmod(0o600)
        listener.bind(("127.0.0.1", 8081))
        journal = Path(directory) / "deliveries.jsonl"
        environment = os.environ | {"FOLLOWUP_MOCK_JOURNAL": str(journal)}
        arguments = [
            sys.executable,
            "-m",
            "uvicorn",
            "techcom_mocks.app:create_app",
            "--factory",
            "--workers",
            "1",
            "--fd",
            str(listener.fileno()),
            "--no-access-log",
        ]
        # Fixed arguments; no shell interprets configuration or paths.
        with subprocess.Popen(  # noqa: S603  # nosec B603
            arguments,
            env=environment,
            pass_fds=(listener.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=logfile,
        ) as process:
            try:
                _ready(process)
                command = [
                    str(Path(sys.executable).with_name("followup")),
                    "demo",
                    "--output",
                    str(output),
                ]
                # Code-owned executable and arguments, without a shell.
                subprocess.run(  # noqa: S603  # nosec B603
                    command, check=True
                )
                if journal.exists():
                    shutil.copyfile(journal, output / "delivery_log.jsonl")
                    (output / "delivery_log.jsonl").chmod(0o600)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
