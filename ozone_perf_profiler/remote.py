"""ssh to cluster hosts through the system OpenSSH, one ControlMaster connection per host.

A password, if any, reaches ssh through SSH_ASKPASS from the environment; it is never put on a command line,
printed or written anywhere except the private askpass helper's environment.
"""

from __future__ import annotations

import concurrent.futures
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

MAX_PARALLEL = 16


class RemoteError(Exception):
    pass


def short(host: str) -> str:
    return host.split(".")[0]


def parallel(fn: Callable[[T], R], items: Iterable[T]) -> dict[T, R | Exception]:
    """Runs fn on every item concurrently; each value is the result or the exception it raised."""
    items = list(items)
    if not items:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(items))) as pool:
        futures = {item: pool.submit(fn, item) for item in items}
    out: dict[T, R | Exception] = {}
    for item, future in futures.items():
        try:
            out[item] = future.result()
        except Exception as e:  # reported per item by the caller
            out[item] = e
    return out


class Remote:
    def __init__(self, user: str, password: str = ""):
        self.user = user
        # Short path: the ControlPath socket name must fit in sun_path (104 bytes on macOS).
        self._tmp = tempfile.mkdtemp(prefix="opp-", dir="/tmp")
        self._hosts: set[str] = set()
        self._env = dict(os.environ)
        self._opts = [
            "-o", "ControlMaster=auto", "-o", f"ControlPath={self._tmp}/%C", "-o", "ControlPersist=30m",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "LogLevel=ERROR",
        ]
        if password:
            askpass = os.path.join(self._tmp, "askpass")
            with open(askpass, "w") as f:
                f.write('#!/bin/sh\nprintf \'%s\\n\' "$OPP_SSH_PASSWORD"\n')
            os.chmod(askpass, 0o700)
            self._env.update(SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force", OPP_SSH_PASSWORD=password)
            self._env.setdefault("DISPLAY", ":0")
            self._opts += ["-o", "NumberOfPasswordPrompts=1"]
        else:
            self._opts += ["-o", "BatchMode=yes"]

    def run(self, host: str, script: str, *args: object, timeout: float = 120) -> str:
        """Runs `bash -c script` on host with args as $1.. and returns stdout; non-zero exit raises."""
        self._hosts.add(host)
        command = " ".join(["bash", "-c", shlex.quote(script), "opp", *(shlex.quote(str(a)) for a in args)])
        try:
            p = subprocess.run(["ssh", *self._opts, f"{self.user}@{host}", command], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, errors="replace", timeout=timeout, env=self._env)
        except subprocess.TimeoutExpired:
            raise RemoteError(f"{short(host)}: timed out after {timeout:.0f}s") from None
        if p.returncode != 0:
            detail = (p.stderr.strip() or p.stdout.strip())[-400:] or f"exit {p.returncode}"
            raise RemoteError(f"{short(host)}: {detail}")
        return p.stdout

    def fetch(self, host: str, remote_path: str, local_path: Path, timeout: float = 1800) -> None:
        """Copies one file from host with scp over the same connection."""
        self._hosts.add(host)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            p = subprocess.run(["scp", "-q", "-p", *self._opts, f"{self.user}@{host}:{remote_path}", str(local_path)],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                               env=self._env)
        except subprocess.TimeoutExpired:
            raise RemoteError(f"{short(host)}: copying {remote_path} timed out after {timeout:.0f}s") from None
        if p.returncode != 0:
            raise RemoteError(f"{short(host)}: copying {remote_path} failed: {p.stderr.strip()[-300:]}")

    def check(self, host: str) -> None:
        if self.run(host, "echo ok", timeout=60).strip() != "ok":
            raise RemoteError(f"{short(host)}: unexpected answer to 'echo ok'")

    def close(self) -> None:
        for host in self._hosts:
            try:
                subprocess.run(["ssh", *self._opts, "-O", "exit", f"{self.user}@{host}"],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=15, env=self._env)
            except (subprocess.TimeoutExpired, OSError):
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> Remote:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
