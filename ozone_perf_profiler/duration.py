"""Durations for time options: '90s', '2m', '15m', '1h30m', '1.5h', '1d', or a plain number of seconds ('120')."""

from __future__ import annotations

import re
from collections.abc import Callable

from .prompts import Invalid, Ok

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PART = re.compile(r"(\d+(?:\.\d+)?)([smhd])")


def parse(raw: str) -> int:
    """Seconds. A plain number is seconds, so older flags like --grace=120 keep working."""
    text = raw.strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+", text):
        return int(text)
    parts = _PART.findall(text)
    if not text or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"not a duration: {raw!r} (use e.g. 90s, 2m, 1h30m, or seconds)")
    return round(sum(float(n) * _UNITS[u] for n, u in parts))


def check(what: str, minimum: int = 0) -> Callable[[str], Ok[int]]:
    """Validator for a prompt or flag: the value in seconds, shown back as e.g. '2m'."""
    def validate(raw: str) -> Ok[int]:
        try:
            seconds = parse(raw)
        except ValueError as e:
            raise Invalid(str(e)) from None
        if seconds < minimum:
            raise Invalid(f"{what} must be at least {format(minimum)}")
        return Ok(seconds, f"{what}: {format(seconds)}")
    return validate


def format(seconds: int) -> str:
    """120 -> '2m', 5400 -> '1h30m', 95 -> '1m35s', 0 -> '0s'."""
    out, left = "", int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if left >= size:
            out += f"{left // size}{unit}"
            left %= size
    return out or "0s"
