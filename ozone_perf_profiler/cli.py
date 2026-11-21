"""opp: benchmark an Ozone cluster managed by Cloudera Manager. Runs the phases in order, asking for what's missing."""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from . import pipeline
from .options import Args
from .prompts import Abort, Prompter


def _parser() -> argparse.ArgumentParser:
    """Flags are generated from Args, so an option is declared once, with its help, in options.py."""
    parser = argparse.ArgumentParser(prog="opp", description=__doc__, epilog=Args.__doc__)
    for f in dataclasses.fields(Args):
        flag, help_text = "--" + f.name.replace("_", "-"), f.metadata["help"]
        if f.default is False:
            parser.add_argument(flag, action="store_true", help=help_text)
        else:
            parser.add_argument(flag, type=Path if isinstance(f.default, Path) else str, default=f.default,
                                help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = Args(**vars(_parser().parse_args(argv)))
    try:
        results = pipeline.run(args)
        return 0 if results and all(r.ok for r in results) else 2
    except Abort as e:
        Prompter().fail(f"stopped: {e}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ninterrupted", file=sys.stderr)
        return 130
