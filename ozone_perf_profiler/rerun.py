"""rerun.sh: the flags that reproduce a run's answers, so it can be repeated without questions."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from .manifest import RunDir
from .options import CM_PWD_ENV, DEFAULT_OUT_DIR, SSH_PWD_ENV


def command(answers: dict[str, object], out_dir: Path) -> str:
    if shutil.which("opp"):
        prog = "opp"
    else:  # not pip-installed: run the package from this checkout
        root = Path(__file__).resolve().parent.parent
        prog = f"PYTHONPATH={shlex.quote(str(root))} {shlex.quote(sys.executable)} -m ozone_perf_profiler"
    flags: list[str] = []
    for key, value in answers.items():
        if value is None or value is False:
            continue
        flag = "--" + key.replace("_", "-")
        flags += [flag] if value is True else [f"{flag}={value}"]
    if out_dir != DEFAULT_OUT_DIR:
        flags.append(f"--out-dir={out_dir}")
    return " ".join([prog, *map(shlex.quote, flags), "--yes"])


def write(run_dir: RunDir, answers: dict[str, object], out_dir: Path) -> str:
    cmd = command(answers, out_dir)
    run_dir.write_text("rerun.sh", "#!/usr/bin/env bash\n"
                                   f"# Export {CM_PWD_ENV} (and {SSH_PWD_ENV} when ssh uses a password) first.\n"
                                   f"set -euo pipefail\nexec {cmd} \"$@\"\n", mode=0o755)
    return cmd
