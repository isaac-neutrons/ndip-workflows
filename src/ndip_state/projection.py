"""Project a stage's CLI arguments out of the workflow state.

This is the "project-out" half of the orchestrator: given a state and a stage
name, build the exact argument list the foreign tool needs. It is the single,
reviewable home for "what does this tool need from the state".

Tools stay schema-agnostic: they receive explicit flags here and report a
neutral manifest back (see ``adapters.py`` and docs/tool-result-schema.md).

Each entry in ``STAGE_PROJECTIONS`` is ``(flag, getter, required)``. ``flag``
is the CLI flag (e.g. ``--event-file``) or the sentinel ``POSITIONAL`` for a
bare positional argument. ``getter(state)`` returns the string value (or empty).
A ``required`` getter that yields nothing raises ``ProjectionError``.
"""

import os
import shlex


POSITIONAL = "__positional__"


class ProjectionError(ValueError):
    """A required value was missing from the state for a stage projection."""


def _get(state, *keys):
    """Deep-get ``state[keys[0]][keys[1]]...``; '' if any level is missing."""
    node = state
    for k in keys:
        if not isinstance(node, dict):
            return ""
        node = node.get(k)
    return node if node is not None else ""


def _outdir(state):
    return _get(state, "inputs", "operator", "output_directory")


def _rundir(state):
    """Operator output dir namespaced by run number, e.g. <out>/226644.

    Automated batch processing reuses one ``output_directory`` across many
    runs. Per-run analysis/assembly artifacts (plan, results, reports,
    assembled) hang off this run-specific subdirectory so a later run can't
    overwrite an earlier one. The reduction stage writes to ``output_directory``
    directly (its filenames already carry the run number), so it does not use
    this. Falls back to ``output_directory`` when no run number is known.
    """
    out = _outdir(state)
    if not out:
        return ""
    run = _get(state, "workflow", "run")
    if run in ("", None):
        return out
    return os.path.join(out, str(run))


def _sub(state, name):
    """A per-run subdirectory of the output dir, e.g. <out>/226644/plan."""
    run_dir = _rundir(state)
    return os.path.join(run_dir, name) if run_dir else ""


def _job_yaml(state):
    # plan-data reports job_yaml as an artifact; a migrated doc may carry it
    # under params. Accept either.
    return (
        _get(state, "stages", "analysis", "artifacts", "job_yaml")
        or _get(state, "stages", "analysis", "params", "job_yaml")
    )


def _model_if_analysis_ok(state):
    if _get(state, "stages", "analysis", "status") == "ok":
        return _get(state, "stages", "analysis", "artifacts", "problem_json")
    return ""


def _ingest_dir(state):
    return (
        _get(state, "stages", "assembly", "artifacts", "ingest_dir")
        or _get(state, "stages", "assembly", "artifacts", "assembled_directory")
    )


def _seq_total(state):
    v = _get(state, "inputs", "operator", "sequence_total")
    return "" if v == "" else str(v)


STAGE_PROJECTIONS = {
    "reduction": [
        ("--event-file", lambda s: _get(s, "inputs", "derived", "nexus_file"), True),
        ("--template", lambda s: _get(s, "inputs", "operator", "template_file"), True),
        ("--output-dir", _outdir, True),
    ],
    "plan": [
        (POSITIONAL, lambda s: _get(s, "stages", "reduction", "artifacts", "partial_file"), True),
        (POSITIONAL, lambda s: _get(s, "inputs", "operator", "context_file"), True),
        ("--output-dir", lambda s: _sub(s, "plan"), True),
        ("--sequence-total", _seq_total, False),
        ("--llm-provider", lambda s: _get(s, "inputs", "operator", "llm", "provider"), False),
        ("--llm-model", lambda s: _get(s, "inputs", "operator", "llm", "model"), False),
        ("--llm-base-url", lambda s: _get(s, "inputs", "operator", "llm", "base_url"), False),
    ],
    # analyze-sample takes the job YAML as a positional CONFIG and derives its
    # own models dir, so we pass job_yaml positionally and only override the
    # results/reports roots.
    "analyze": [
        (POSITIONAL, _job_yaml, True),
        ("--results-dir", lambda s: _sub(s, "results"), True),
        ("--reports-dir", lambda s: _sub(s, "reports"), True),
    ],
    "ingest": [
        ("-o", lambda s: _sub(s, "assembled"), True),
        ("--reduced", lambda s: _get(s, "stages", "reduction", "artifacts", "partial_file"), False),
        ("--nexus-file", lambda s: _get(s, "inputs", "derived", "nexus_file"), False),
        ("--model", _model_if_analysis_ok, False),
    ],
    # nr-isaac-format convert-ingest uses --raw (not --nexus-file).
    "convert": [
        (POSITIONAL, _ingest_dir, True),
        ("--raw", lambda s: _get(s, "inputs", "derived", "nexus_file"), False),
        ("--reduced", lambda s: _get(s, "stages", "reduction", "artifacts", "partial_file"), False),
    ],
}


def project_out(stage, state):
    """Return the CLI argument tokens for *stage* built from *state*.

    Raises ``ProjectionError`` if the stage is unknown or a required value is
    missing. Optional flags whose value is empty are omitted.
    """
    spec = STAGE_PROJECTIONS.get(stage)
    if spec is None:
        raise ProjectionError("unknown stage: %s" % stage)

    args = []
    for flag, getter, required in spec:
        value = getter(state) or ""
        value = str(value)
        if not value:
            if required:
                raise ProjectionError(
                    "stage %r: missing required value for %s"
                    % (stage, "positional arg" if flag == POSITIONAL else flag)
                )
            continue
        if flag == POSITIONAL:
            args.append(value)
        else:
            args.extend([flag, value])
    return args


def project_out_shell(stage, state):
    """Like :func:`project_out` but as a single shell-quoted string for XML bash."""
    return " ".join(shlex.quote(a) for a in project_out(stage, state))
