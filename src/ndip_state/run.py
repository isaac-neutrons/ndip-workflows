"""``ndip-run`` — drive one pipeline stage from the workflow state.

This is the agent-driven (no-Galaxy) orchestrator. For a given stage it:

  1. projects the tool's CLI args out of the current state,
  2. runs the tool with those args + ``--result-out <manifest>``,
  3. merges the neutral manifest back into the state (canonicalizing paths),
  4. writes the updated state back.

The agent never needs to know any tool's argument surface — only the stage
name. Each stage has a default ``--tool-cmd`` (see ``DEFAULT_TOOL_CMDS``), so
the tool invocation is optional; pass ``--tool-cmd`` only to override it.
Example::

    S=/tmp/state.json
    seed-config /SNS/.../REF_L_226644.nxs.h5 seed.yaml -o $S   # produces the seed
    ndip-run reduction --state $S
    ndip-run plan      --state $S
    ndip-run analyze   --state $S
    ndip-run ingest    --state $S
    ndip-run convert   --state $S

For a Mantid-free local run, seed past reduction and drive the whole downstream
chain in one shot::

    seed-config seed.yaml --from-reduced REFL_..._partial.txt -o $S
    ndip-run all --state $S       # plan -> analyze -> ingest -> convert

``ndip-run all`` skips reduction by default; pass ``--include-reduction`` to
prepend it on a host that has the full (Mantid) tool image and an event file.

Runs on a single host where the tool binaries are on ``$PATH``; cross-container
orchestration stays Galaxy's job.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile

from .adapters import merge_in
from .projection import ProjectionError, project_out
from .state import load_state, save_state


# Default tool invocation per stage. ``--tool-cmd`` overrides these; the binaries
# come from the ``.[workflow]`` extra (nr-analyzer, data-assembler,
# nr-isaac-format) and must be on ``$PATH``.
DEFAULT_TOOL_CMDS = {
    "reduction": "simple-reduction",
    "plan": "plan-data",
    "analyze": "analyze-sample --no-reduction-gate",
    "ingest": "data-assembler ingest",
    "convert": "nr-isaac-format convert-ingest",
}

# The stages ``ndip-run all`` drives by default. Reduction is excluded: it needs
# Mantid + an event file, and the local-first path skips it via
# ``seed-config --from-reduced``. ``--include-reduction`` prepends it.
CHAIN_STAGES = ("plan", "analyze", "ingest", "convert")


def run_stage(stage, state_path, tool_cmd, output_prefix=None, result_out=None):
    """Project args, invoke the tool, merge its manifest. Returns the exit code."""
    state = load_state(state_path)

    try:
        args = project_out(stage, state)
    except ProjectionError as exc:
        raise SystemExit("project-out failed for stage %r: %s" % (stage, exc))

    cleanup = False
    if result_out is None:
        fd, result_out = tempfile.mkstemp(prefix="ndip_result_", suffix=".json")
        os.close(fd)
        cleanup = True

    cmd = shlex.split(tool_cmd) + args + ["--result-out", result_out]
    sys.stderr.write("ndip-run %s: %s\n" % (stage, " ".join(shlex.quote(c) for c in cmd)))
    proc = subprocess.run(cmd)
    exit_code = proc.returncode

    manifest = _load_manifest(result_out, exit_code)
    merge_in(stage, state, manifest, exit_code, output_prefix=output_prefix)
    save_state(state, state_path)

    if cleanup:
        try:
            os.unlink(result_out)
        except OSError:
            pass
    return exit_code


def run_chain(stages, state_path, tool_cmds=None, output_prefix=None):
    """Run *stages* in order via :func:`run_stage`; stop at the first non-zero rc.

    Each stage uses its entry in *tool_cmds* (defaults to ``DEFAULT_TOOL_CMDS``).
    Returns the exit code of the last stage attempted.
    """
    tool_cmds = tool_cmds or DEFAULT_TOOL_CMDS
    rc = 0
    for st in stages:
        rc = run_stage(st, state_path, tool_cmds[st], output_prefix)
        if rc != 0:
            sys.stderr.write("ndip-run all: stage %r failed (exit %d); stopping.\n" % (st, rc))
            break
    return rc


def _load_manifest(path, exit_code):
    """Load the tool's manifest; synthesize a failed one if it's missing/bad."""
    if path and os.path.isfile(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "status": "failed",
        "exit_code": exit_code,
        "messages": [{"level": "error", "text": "tool produced no usable result manifest"}],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ndip-run",
        description="Project args from state, run a stage's tool, merge its result back.",
    )
    parser.add_argument(
        "stage",
        choices=["reduction", "plan", "analyze", "ingest", "convert", "all"],
        help="Pipeline stage, or 'all' to chain plan->analyze->ingest->convert.",
    )
    parser.add_argument("--state", required=True, help="Path to the workflow-state JSON (updated in place).")
    parser.add_argument(
        "--tool-cmd",
        default=None,
        help="The tool invocation, e.g. 'analyze-sample --no-reduction-gate'. "
             "Defaults per stage (see DEFAULT_TOOL_CMDS); not allowed with 'all'.",
    )
    parser.add_argument(
        "--include-reduction",
        action="store_true",
        help="With 'all', prepend the reduction stage (needs the full Mantid "
             "image and an event file).",
    )
    parser.add_argument("--output-prefix", default=None, help="Operator output dir for path canonicalization.")
    parser.add_argument("--result-out", default=None, help="Where the tool writes its manifest (default: temp file).")
    args = parser.parse_args(argv)

    # Default the canonicalization prefix to the operator output directory.
    output_prefix = args.output_prefix
    if output_prefix is None:
        state = load_state(args.state)
        output_prefix = (
            (state.get("inputs") or {}).get("operator", {}).get("output_directory") or None
        )

    if args.stage == "all":
        if args.tool_cmd is not None:
            parser.error("--tool-cmd cannot be combined with the 'all' stage; each stage uses its default tool.")
        if args.result_out is not None:
            parser.error("--result-out cannot be combined with the 'all' stage.")
        stages = (("reduction",) + CHAIN_STAGES) if args.include_reduction else CHAIN_STAGES
        rc = run_chain(stages, args.state, output_prefix=output_prefix)
        sys.exit(rc)

    tool_cmd = args.tool_cmd or DEFAULT_TOOL_CMDS[args.stage]
    rc = run_stage(args.stage, args.state, tool_cmd, output_prefix, args.result_out)
    sys.exit(rc)


if __name__ == "__main__":
    main()
