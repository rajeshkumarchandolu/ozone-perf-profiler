"""Runs every step in order over one ssh session and one run folder (.output/<run name>/)."""

from __future__ import annotations

from . import discover, freon, freon_output, jfr, report, rerun, telemetry
from .options import CM_PWD_ENV, SSH_PWD_ENV, Args
from .prompts import Abort, Prompter


def run(args: Args) -> bool:
    p = Prompter(interactive=not args.yes)
    d = discover.run(p, args)
    with d.remote:
        source = telemetry.ask(p, args, d)  # part of discovery: every endpoint is checked before anything runs
        p.ok("Discovery successful")
        plan = freon.ask(p, args, d)
        jfr_plan = jfr.ask(p, args, d)
        jfr_answers = jfr_plan.answers if jfr_plan else {}
        command = rerun.write(d.run_dir, d.answers | plan.answers | jfr_answers | source.answers, args.out_dir)
        if not p.confirm("\nEverything is answered. Start Freon?"):
            raise Abort("Freon not started")
        if plan.warmup_keys and not freon_output.report(p, d.run_dir, freon.execute(p, d, plan, 0)).ok:
            raise Abort("the warm-up run failed, so the measured runs would too; see the freon.log lines above")
        recording = jfr.start(p, d, jfr_plan) if jfr_plan else []  # from run 1 on, after the warm-up
        results = []
        try:
            for index in range(1, plan.runs + 1):
                if index > 1:
                    freon.wait_grace(p, plan, index)
                results.append(freon_output.report(p, d.run_dir, freon.execute(p, d, plan, index)))
        except BaseException:
            if recording:
                p.warn(jfr.interrupted_hint(jfr_plan))
            raise
        recordings = jfr.finish(p, d, jfr_plan, recording) if recording else []
        freon.cleanup(p, d, plan)
    freon_output.series(p, d.run_dir, results)
    values = telemetry.collect(p, d, source, results)
    report.write(p, d, plan, results, source, values, recordings)
    p.say(f"\nRun folder: {d.run_dir.path}")
    p.say(f"Repeat without questions ({CM_PWD_ENV}/{SSH_PWD_ENV} from the environment):\n  {command}")
    return all(r.ok for r in results)
