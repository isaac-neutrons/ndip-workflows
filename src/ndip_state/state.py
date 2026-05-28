"""Workflow state for the ISAAC reflectivity pipeline.

A single JSON document threads through every stage. It splits cleanly into:

  - ``schema_version``        — currently ``"2"``.
  - ``workflow``              — identity (``run``, ``instrument``, ``ipts``).
  - ``inputs.operator``       — operator-supplied: sequence_total, prompt,
                                template_file, context_file, output_directory,
                                export_path, llm.{provider,model,base_url}.
  - ``inputs.derived``        — discovered: nexus_file, data_directory,
                                ipts_shared_root.
  - ``stages.{reduction,
              analysis,
              assembly}``     — per-stage records. Each carries ``status``,
                                ``params`` (resolved inputs the stage used —
                                provenance), ``artifacts`` (paths produced),
                                and ``info`` (scalar diagnostics).
  - ``errors``                — append-only ``[{stage, message, exit_code}, ...]``.

Stage records share the same four-field shape (status / params / artifacts /
info) as the neutral ``ndip-tool-result/1`` manifest the tools emit — so the
merge-in adapter is little more than a routed copy.
"""

import argparse
import json
import os


SCHEMA_VERSION = "2"

_STAGES = ("reduction", "analysis", "assembly")

# manifest status -> stage status
_STATUS_FROM_MANIFEST = {
    "ok": "ok",
    "dry-run": "ok",
    "skipped": "skipped",
    "failed": "failed",
    "needs-reprocessing": "failed",
}


def _empty_stage():
    return {"status": "pending", "params": {}, "artifacts": {}, "info": {}}


def empty_state():
    """Return a fresh state skeleton."""
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow": {},
        "inputs": {"operator": {}, "derived": {}},
        "stages": {s: _empty_stage() for s in _STAGES},
        "errors": [],
    }


def build_state(flat):
    """Construct a state document from a flat operator-shaped dict.

    The flat vocabulary is what ``seed-config`` and ``yaml-parser`` use::

        workflow identity : run, instrument, ipts
        operator inputs   : sequence_total, prompt,
                            template_file, context_file,
                            output_directory, export_path
        LLM endpoint      : llm_provider, llm_model, llm_base_url
        derived (parsed)  : event_file (-> inputs.derived.nexus_file),
                            data_directory, ipts_shared_root

    Unknown keys are dropped — the structure is rigid; provenance lives inside
    each stage's ``params`` block, populated by the merge-in adapters.
    """
    state = empty_state()

    for k in ("run", "instrument", "ipts"):
        if flat.get(k) is not None:
            state["workflow"][k] = flat[k]

    op = state["inputs"]["operator"]
    for k in ("sequence_total", "prompt",
              "template_file", "context_file",
              "output_directory", "export_path"):
        if flat.get(k) is not None:
            op[k] = flat[k]

    llm = {}
    for nested_key, flat_key in (("provider", "llm_provider"),
                                 ("model", "llm_model"),
                                 ("base_url", "llm_base_url")):
        if flat.get(flat_key) is not None:
            llm[nested_key] = flat[flat_key]
    if llm:
        op["llm"] = llm

    der = state["inputs"]["derived"]
    if flat.get("event_file"):
        der["nexus_file"] = flat["event_file"]
    if flat.get("data_directory"):
        der["data_directory"] = flat["data_directory"]
    if flat.get("ipts_shared_root"):
        der["ipts_shared_root"] = flat["ipts_shared_root"]

    return state


def load_state(path):
    """Load a state document. Empty/missing path returns a fresh skeleton.

    Documents without ``schema_version == "2"`` raise ``ValueError`` — there is
    no migration path and never has been (the pipeline ships only v2).
    """
    if not path or not os.path.isfile(path):
        return empty_state()
    with open(path) as f:
        d = json.load(f)
    if not isinstance(d, dict):
        return empty_state()
    if str(d.get("schema_version")) != SCHEMA_VERSION:
        raise ValueError(
            "state document has schema_version=%r; expected %r"
            % (d.get("schema_version"), SCHEMA_VERSION)
        )
    return d


def save_state(state, path):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# --------------------------------------------------------------------------
# merging tool-result manifests into stage records
# --------------------------------------------------------------------------

def record_error(state, stage, message, exit_code=None):
    """Append an error record for *stage*."""
    state.setdefault("errors", []).append({
        "stage": stage,
        "message": message,
        "exit_code": exit_code,
    })
    return state


def _manifest_to_stage_status(manifest, exit_code):
    """Resolve the stage status from a manifest's ``status`` + the exit code.

    A non-zero exit code forces ``failed`` regardless of the reported status;
    an unknown/missing status is treated as ``failed`` (never silently ok).
    """
    if exit_code not in (0, None):
        return "failed"
    return _STATUS_FROM_MANIFEST.get((manifest or {}).get("status"), "failed")


def _first_error_message(manifest):
    for m in (manifest or {}).get("messages") or []:
        if isinstance(m, dict) and m.get("level") == "error" and m.get("text"):
            return m["text"]
    return None


def merge_stage(state, stage, manifest, exit_code=0):
    """Fold a neutral tool-result *manifest* into ``state['stages'][stage]``.

    Routes the manifest's ``params`` / ``artifacts`` / ``info`` into the stage
    record (shallow-merged) and maps its ``status`` onto the stage status. On a
    failed status an ``errors[]`` entry is appended, preserving the invariant
    that ``status == "failed"`` always has a matching error record.
    """
    manifest = manifest or {}
    block = state.setdefault("stages", {}).setdefault(stage, _empty_stage())
    block["status"] = _manifest_to_stage_status(manifest, exit_code)
    for key in ("params", "artifacts", "info"):
        incoming = manifest.get(key)
        if incoming:
            block.setdefault(key, {}).update(incoming)
    if block["status"] == "failed":
        msg = _first_error_message(manifest) or "%s failed" % stage
        record_error(state, stage, msg, manifest.get("exit_code", exit_code))
    return state


def overall_status(state):
    """Roll up the per-stage statuses (computed, never stored)."""
    statuses = [
        (state.get("stages") or {}).get(s, {}).get("status") for s in _STAGES
    ]
    if "failed" in statuses:
        return "failed"
    if "pending" in statuses:
        return "pending"
    return "ok"


# --------------------------------------------------------------------------
# CLI: ``project-out`` / ``merge-in``. Lazy imports avoid a circular dependency
# (adapters imports merge_stage from this module).
# --------------------------------------------------------------------------

def _cmd_project_out(args):
    from .projection import project_out_shell
    import sys
    sys.stdout.write(project_out_shell(args.stage, load_state(args.state)))


def _cmd_merge_in(args):
    from .adapters import merge_in
    state = load_state(args.state_in)
    manifest = {}
    if args.result and os.path.isfile(args.result):
        with open(args.result) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            manifest = loaded
    merge_in(args.stage, state, manifest, args.exit_code, output_prefix=args.output_prefix)
    save_state(state, args.state_out)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ndip-state")
    sub = parser.add_subparsers(dest="cmd", required=True)
    stages = ["reduction", "plan", "analyze", "ingest", "convert"]

    p = sub.add_parser("project-out", help="Print shell-quoted tool args from state")
    p.add_argument("stage", choices=stages)
    p.add_argument("state")
    p.set_defaults(func=_cmd_project_out)

    p = sub.add_parser("merge-in", help="Merge a tool-result manifest into state")
    p.add_argument("stage", choices=stages)
    p.add_argument("state_in")
    p.add_argument("result")
    p.add_argument("exit_code", type=int)
    p.add_argument("state_out")
    p.add_argument("--output-prefix", default=None)
    p.set_defaults(func=_cmd_merge_in)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
