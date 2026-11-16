"""Terminal prompts that validate every answer on the spot: ✔ and move on, or ✘ and ask again.

A value given as a flag (preset) goes through the same validator. Non-interactive mode (--yes) never reads stdin:
it uses the preset, else the default, else stops naming the flag that is missing.
"""

from __future__ import annotations

import getpass
import os
import sys
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Invalid(Exception):
    """A validator rejects the answer; the message says why."""


class Abort(Exception):
    """The session cannot go on."""


@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T
    detail: str = ""  # shown after ✔; empty prints nothing


@dataclass(frozen=True)
class Option(Generic[T]):
    key: str      # matched against the flag value
    label: str    # the row shown in the list
    value: T


Validator = Callable[[str], Ok[T]]


def accept(raw: str) -> Ok[str]:
    if not raw:
        raise Invalid("a value is required")
    return Ok(raw)


class _Style:
    def __init__(self, stream):
        tty = stream.isatty()
        utf8 = (stream.encoding or "").lower().replace("-", "") == "utf8"
        color = tty and "NO_COLOR" not in os.environ
        self._ok = "✔" if tty and utf8 else "[OK]"
        self._fail = "✘" if tty and utf8 else "[FAIL]"
        self._warn = "!" if tty and utf8 else "[WARN]"
        self._paint = (lambda code, s: f"\033[{code}m{s}\033[0m") if color else (lambda code, s: s)

    def ok(self, msg: str) -> str:
        return f"  {self._paint('32', self._ok)} {msg}"

    def fail(self, msg: str) -> str:
        return f"  {self._paint('31', self._fail)} {msg}"

    def warn(self, msg: str) -> str:
        return f"  {self._paint('33', self._warn)} {msg}"

    def dim(self, msg: str) -> str:
        return self._paint("2", msg)


class Prompter:
    def __init__(self, interactive: bool = True, out=sys.stdout):
        self.interactive = interactive
        self._out = out
        self._style = _Style(out)

    # --- output ---

    def say(self, msg: str = "") -> None:
        print(msg, file=self._out, flush=True)

    def ok(self, msg: str) -> None:
        self.say(self._style.ok(msg))

    def fail(self, msg: str) -> None:
        self.say(self._style.fail(msg))

    def warn(self, msg: str) -> None:
        self.say(self._style.warn(msg))

    def dim(self, msg: str) -> str:
        return self._style.dim(msg)

    def section(self, title: str) -> None:
        self.say(f"\n== {title} " + "=" * max(0, 72 - len(title)))

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[object]], indent: int = 0) -> None:
        cells = [[str(c) for c in row] for row in rows]
        widths = [max([len(h), *(len(r[i]) for r in cells)]) for i, h in enumerate(headers)]
        line = " " * indent + "  ".join(f"{{:<{w}}}" for w in widths)
        self.say(line.format(*headers))
        self.say(line.format(*("-" * w for w in widths)))
        for r in cells:
            self.say(line.format(*r))

    # --- questions ---

    def ask(self, label: str, validate: Validator[T] = accept, *, default: str | None = None,
            preset: str | None = None, flag: str = "", secret: bool = False) -> T:
        if preset is not None:
            result = self._check(validate, preset, secret)
            if result is not None:
                return result.value
            if not self.interactive:
                raise Abort(f"{flag or label}: invalid value")
            # Interactive: a bad flag value falls through to asking.
        elif not self.interactive:
            if default is None:
                raise Abort(f"{flag or label} is required with --yes")
            result = self._check(validate, default, secret)
            if result is None:
                raise Abort(f"{label}: default {default!r} is not valid here")
            return result.value
        hint = f" [{self._style.dim(default)}]" if default and not secret else ""
        while True:
            raw = self._read_secret(f"{label}: ", flag) if secret else self._read(f"{label}{hint}: ")
            result = self._check(validate, raw if raw else (default or ""), secret)
            if result is not None:
                return result.value

    def choose(self, label: str, options: Sequence[Option[T]], *, preset: str | None = None, flag: str = "",
               validate: Callable[[T], Ok[Any]] | None = None) -> Any:
        """Numbered pick list. Returns the option's value, or what validate wraps it into.
        A single option is taken without asking (still validated)."""
        if not options:
            raise Abort(f"nothing to choose for {label}")
        if len(options) == 1 and preset is None:
            only = options[0]
            result = self._check(lambda _: validate(only.value) if validate
                                 else Ok(only.value, f"{label}: {only.label}  (only one)"), "")
            if result is None:
                raise Abort(f"{label}: the only choice, {only.key}, is not usable")
            return result.value
        self.say(f"{label}:")
        for i, opt in enumerate(options, 1):
            self.say(f"  {i}) {opt.label}")
        by_key = {o.key: o for o in options}

        def pick(raw: str) -> Ok[Any]:
            raw = raw.strip()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                opt = options[int(raw) - 1]
            elif raw in by_key:
                opt = by_key[raw]
            else:
                raise Invalid(f"enter a number from 1 to {len(options)}")
            return validate(opt.value) if validate else Ok(opt.value, opt.key)

        return self.ask(f"Select 1-{len(options)}", pick, default="1", preset=preset, flag=flag)

    def confirm(self, question: str, default: bool = True) -> bool:
        if not self.interactive:
            return default
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            raw = self._read(f"{question} {hint} ").strip().lower()
            if not raw:
                return default
            if raw in ("y", "yes", "n", "no"):
                return raw.startswith("y")

    # --- internals ---

    def _check(self, validate: Validator[T], raw: str, secret: bool = False) -> Ok[T] | None:
        try:
            result = validate(raw if secret else raw.strip())
        except Invalid as e:
            self.fail(str(e))
            return None
        if result.detail:
            self.ok(result.detail)
        return result

    def _read(self, prompt: str) -> str:
        return input(prompt)

    def _read_secret(self, prompt: str, source: str) -> str:
        """Hidden input. Fails closed: getpass falls back to echoing when it can't control the terminal
        (IDE run consoles, piped stdin), and a password must never show on screen."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return getpass.getpass(prompt)
            except getpass.GetPassWarning:
                raise Abort(f"this console cannot hide typed input; run opp in a real terminal, "
                            f"or set {source or 'the secret'} in the environment") from None
