"""
v1 workflow state for the ISAAC reflectivity pipeline.

A single JSON document flows through every Galaxy tool
(reduction -> simple_analyzer -> data_assembler). It captures:

  - schema_version: the on-disk schema version ("1").
  - run, instrument, ipts, sequence_total, prompt: top-level scalars.
  - paths: input/derived filesystem paths shared across stages.
  - llm:   LLM endpoint configuration.
  - reduction / analysis / assembly: per-stage outcome blocks. Each block
    has at minimum ``success`` (bool|None) and ``metadata`` (dict). Helpers
    in this module merge stage-specific keys (e.g. partial_file, problem_json).
  - errors: append-only list of stage error records.

This module is intentionally stdlib-only so the same source can be inlined
verbatim into Galaxy tool configfiles, which run inside foreign containers
that do not have this project installed.
"""

import argparse
import json
import os
import shlex


SCHEMA_VERSION = "1"

_PATH_KEYS = (
    "data_directory",
    "output_directory",
    "template_file",
    "context_file",
    "event_file",
    "input_file",
    "raw_data",
    "export_path",
)

_LLM_KEY_MAP = (
    # (v1 nested key, flat-input key used by yaml-parser / seed-config)
    ("provider", "llm_provider"),
    ("model", "llm_model"),
    ("base_url", "llm_base_url"),
)


def empty_state():
    """Return a fresh v1 state skeleton."""
    return {
        "schema_version": SCHEMA_VERSION,
        "paths": {},
        "llm": {},
        "reduction": {"success": None, "metadata": {}},
        "analysis": {"success": None, "metadata": {}},
        "assembly": {"success": None, "metadata": {}},
        "errors": [],
    }


def _path(state, key, default=""):
    """Read a path-style value from the state's ``paths.*`` block."""
    return (state.get("paths") or {}).get(key) or default


def build_state(flat):
    """Construct a v1 state from a flat-shaped input dict.

    Used by ``yaml-parser`` and ``seed-config`` to translate operator-authored
    YAML / JSON (where everything is one flat namespace) into the nested v1
    shape. Known keys land in their target blocks; anything else is preserved
    verbatim at the top level for forward compatibility.
    """
    state = empty_state()
    leftover = dict(flat)
    leftover.pop("schema_version", None)

    for k in _PATH_KEYS:
        if k in leftover:
            state["paths"][k] = leftover.pop(k)

    for v1_key, flat_key in _LLM_KEY_MAP:
        if flat_key in leftover:
            state["llm"][v1_key] = leftover.pop(flat_key)

    state.update(leftover)
    return state


def _ensure_blocks(d):
    """Fill missing top-level blocks on a v1 state read from disk."""
    base = empty_state()
    for k, v in base.items():
        if k not in d:
            d[k] = v
        elif isinstance(v, dict) and isinstance(d.get(k), dict):
            for kk, vv in v.items():
                d[k].setdefault(kk, vv)
    return d


def load_state(path):
    """Load a v1 state file.

    An empty / missing path returns an empty v1 state. The pipeline has only
    ever produced v1 documents; no v0 migration is attempted.
    """
    if not path or not os.path.isfile(path):
        return empty_state()
    with open(path) as f:
        d = json.load(f)
    if not isinstance(d, dict):
        return empty_state()
    return _ensure_blocks(d)


def save_state(state, path):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def update_stage(state, stage, **fields):
    """Set fields under ``state[stage]``; ``metadata`` is shallow-merged."""
    block = state.setdefault(stage, {"success": None, "metadata": {}})
    meta = fields.pop("metadata", None)
    if meta:
        block.setdefault("metadata", {}).update(meta)
    block.update(fields)
    return state


def record_error(state, stage, message, exit_code=None):
    """Append an error record for a stage."""
    state.setdefault("errors", []).append({
        "stage": stage,
        "message": message,
        "exit_code": exit_code,
    })
    return state


# Mapping from env-var name to a getter producing the string value.
# The set is intentionally generous: every tool sources the same _env.sh
# and ignores variables it doesn't use.
_ENV_FROM_STATE = (
    ("EVENT_FILE", lambda s: _path(s, "event_file")),
    ("INPUT_FILE", lambda s: _path(s, "input_file") or _path(s, "event_file")),
    ("TEMPLATE", lambda s: _path(s, "template_file")),
    ("DATA_DIR", lambda s: _path(s, "data_directory")),
    ("OUTPUT_DIR", lambda s: _path(s, "output_directory")),
    ("CONTEXT_FILE", lambda s: _path(s, "context_file")),
    ("RAW_DATA", lambda s: _path(s, "raw_data") or _path(s, "event_file")),
    ("EXPORT_PATH", lambda s: _path(s, "export_path")),
    ("PROMPT", lambda s: s.get("prompt", "")),
    ("SEQUENCE_TOTAL", lambda s: "" if s.get("sequence_total") is None else str(s.get("sequence_total"))),
    ("LLM_PROVIDER", lambda s: (s.get("llm") or {}).get("provider", "")),
    ("LLM_MODEL", lambda s: (s.get("llm") or {}).get("model", "")),
    ("LLM_BASE_URL", lambda s: (s.get("llm") or {}).get("base_url", "")),
    ("REFLECTIVITY_FILE", lambda s: (s.get("reduction") or {}).get("partial_file", "")),
    ("PARTIAL_FILE", lambda s: (s.get("reduction") or {}).get("partial_file", "")),
    ("COMBINED_FILE", lambda s: (s.get("reduction") or {}).get("combined_file", "")),
    ("MODEL_NAME", lambda s: (s.get("analysis") or {}).get("model_name", "") or ""),
    ("FINAL_MODEL", lambda s: (s.get("analysis") or {}).get("problem_json", "") or ""),
    ("MODEL_AVAILABLE", lambda s: "1" if (s.get("analysis") or {}).get("success") else "0"),
    ("ISAAC_RECORD", lambda s: (s.get("assembly") or {}).get("isaac_record", "") or ""),
)


def emit_env(state, env_path):
    """Write ``export KEY=value`` lines that a tool's bash can ``. _env.sh``."""
    with open(env_path, "w") as f:
        for name, getter in _ENV_FROM_STATE:
            value = getter(state) or ""
            f.write("export %s=%s\n" % (name, shlex.quote(str(value))))


# --------------------------------------------------------------------------
# CLI subcommands invoked from Galaxy tool XMLs.
# --------------------------------------------------------------------------

def _load_or_empty(path):
    return load_state(path) if path else empty_state()


def _cmd_parse_config(args):
    state = _load_or_empty(args.config)
    emit_env(state, args.env_out)


def _cmd_merge_reduction(args):
    state = _load_or_empty(args.config)
    with open(args.summary) as f:
        summary = json.load(f)
    partial = summary.get("partial_file") or ""
    combined = summary.get("combined_file") or ""
    update_stage(
        state,
        "reduction",
        success=True,
        partial_file=partial,
        combined_file=combined,
    )
    ev = _path(state, "event_file")
    if ev:
        state.setdefault("paths", {})["raw_data"] = ev
    save_state(state, args.out)


def _cmd_merge_analyzer(args):
    state = _load_or_empty(args.config)
    success = args.exit_code == 0
    update_stage(
        state,
        "analysis",
        success=success,
        model_name=args.model_name or None,
        problem_json=args.problem_json or None,
    )
    if not success:
        record_error(
            state,
            "analysis",
            "analyze-sample failed or no problem.json produced",
            args.exit_code,
        )
    save_state(state, args.out)


def _cmd_merge_assembler(args):
    state = _load_or_empty(args.config)
    success = args.exit_code == 0
    update_stage(
        state,
        "assembly",
        success=success,
        isaac_record=args.isaac_record or None,
    )
    if not success:
        record_error(state, "assembly", "data-assembler failed", args.exit_code)
    save_state(state, args.out)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ndip-state")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse-config", help="Emit _env.sh from a state file")
    p.add_argument("config")
    p.add_argument("env_out")
    p.set_defaults(func=_cmd_parse_config)

    p = sub.add_parser("merge-reduction", help="Fold a reduction summary into state")
    p.add_argument("config")
    p.add_argument("summary")
    p.add_argument("out")
    p.set_defaults(func=_cmd_merge_reduction)

    p = sub.add_parser("merge-analyzer", help="Fold analyzer outcome into state")
    p.add_argument("config")
    p.add_argument("exit_code", type=int)
    p.add_argument("model_name")
    p.add_argument("problem_json")
    p.add_argument("out")
    p.set_defaults(func=_cmd_merge_analyzer)

    p = sub.add_parser("merge-assembler", help="Fold assembler outcome into state")
    p.add_argument("config")
    p.add_argument("exit_code", type=int)
    p.add_argument("isaac_record")
    p.add_argument("out")
    p.set_defaults(func=_cmd_merge_assembler)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
