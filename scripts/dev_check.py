"""Start isolated Redis, run all tests, then exercise actual worker processes."""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from benchmark import ROOT, stop_process
from redis.exceptions import RedisError

from api_sentinel.queue import connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-server", default=shutil.which("redis-server"))
    args = parser.parse_args()
    if not args.redis_server:
        parser.error("install redis-server or pass its absolute path")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    output = ROOT / "reports"
    output.mkdir(exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="sentinel-redis-") as directory,
        (output / "redis.log").open("w") as log,
    ):
        redis = subprocess.Popen(
            [
                args.redis_server,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                directory,
            ],
            stdout=log,
            stderr=log,
        )
        url = f"redis://127.0.0.1:{port}/0"
        client = connect(url)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if client.ping():
                        break
                except RedisError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
            env = {**os.environ, "TEST_REDIS_URL": url}
            commands = [
                [sys.executable, "-m", "ruff", "check", "."],
                [sys.executable, "-m", "ruff", "format", "--check", "."],
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--cov=api_sentinel",
                    "--cov-report=json:reports/coverage.json",
                    "--cov-report=term-missing",
                    "--junitxml=reports/unit-tests.xml",
                ],
                [sys.executable, "scripts/verify_local.py", "--redis-url", url],
            ]
            for command in commands:
                subprocess.run(command, env=env, cwd=ROOT, check=True, timeout=180)
        finally:
            client.close()
            stop_process(redis)


if __name__ == "__main__":
    main()
