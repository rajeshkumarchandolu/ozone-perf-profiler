"""Runs every step in order over one ssh session and one run folder (.output/<run name>/)."""

from __future__ import annotations

from . import discover, freon, freon_output, profiling, report, rerun, telemetry
from .options import CM_PWD_ENV, SSH_PWD_ENV, Args
from .prompts import Abort, Prompter


def run(args: Args) -> bool:
    p = Prompter(interactive=not args.yes)
    d = discover.run(p, args)
    with d.remote:
        source = telemetry.ask(p, args, d)  # part of discovery: every endpoint is checked before anything runs
        available = profiling.detect(p, d)
        p.ok("Discovery successful")
        plan = freon.ask(p, args, d)
        profile = profiling.ask(p, args, d, available)
        profile_answers = profile.answers if profile else {"profiler": profiling.NONE}
        command = rerun.write(d.run_dir, d.answers | plan.answers | profile_answers | source.answers, args.out_dir)
        if not p.confirm("\nEverything is answered. Start Freon?"):
            raise Abort("Freon not started")
        if profile:
            profiling.install(p, d, profile, available)
        if plan.warmup_keys and not freon_output.report(p, d.run_dir, freon.execute(p, d, plan, 0)).ok:
            raise Abort("the warm-up run failed, so the measured runs would too; see the freon.log lines above")
        recording = profiling.start(p, d, profile) if profile else []  # from run 1 on, after the warm-up
        results = []
        try:
            for index in range(1, plan.runs + 1):
                if index > 1:
                    freon.wait_grace(p, plan, index)
                results.append(freon_output.report(p, d.run_dir, freon.execute(p, d, plan, index)))
        except BaseException as e:
            if recording:
                _interrupted_profiling(p, d, profile, recording, interrupted=isinstance(e, KeyboardInterrupt))
            raise
        recordings = profiling.finish(p, d, profile, recording) if recording else []
        freon.cleanup(p, d, plan)
    freon_output.series(p, d.run_dir, results)
    values = telemetry.collect(p, d, source, results)
    report.write(p, d, plan, results, source, values, profile, recordings)
    p.say(f"\nRun folder: {d.run_dir.path}")
    p.say(f"Repeat without questions ({CM_PWD_ENV}/{SSH_PWD_ENV} from the environment):\n  {command}")
    return all(r.ok for r in results)


def _interrupted_profiling(p: Prompter, d: discover.Discovered, profile: profiling.ProfilePlan,
                           recording: list[profiling.Target], interrupted: bool) -> None:
    """After Ctrl-C (or an error) during the runs: stop the recordings and copy them now, or leave them running."""
    question = f"Stop the {len(recording)} {profile.profiler} recordings now and copy them here?"
    if interrupted and p.interactive and p.confirm(question, default=True):
        profiling.finish(p, d, profile, recording)  # a second Ctrl-C here quits right away
    else:
        p.warn(profiling.interrupted_hint(profile))
