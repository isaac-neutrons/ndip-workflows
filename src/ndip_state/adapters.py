"""Merge a neutral tool-result manifest back into the workflow state.

This is the "merge-in" half of the orchestrator. One entry point,
:func:`merge_in`, dispatches per call-stage:

  - ``reduction``           -> ``stages.reduction``
  - ``plan`` / ``analyze``  -> ``stages.analysis``
  - ``ingest`` / ``convert``-> ``stages.assembly``

The heavy lifting (status mapping, field routing, error recording) lives in
``state.merge_stage`` because a manifest and a stage record share the same
four-field shape. On top of that, this layer:

  - blends in **orchestrator-derived provenance** the tool itself can't know
    (the input files we resolved for it, the reduction template's content hash),
  - canonicalizes realpath'd paths back to the operator prefix on request.
"""

import hashlib
import os

from .canonicalize import canonicalize_paths
from .projection import _get
from .state import merge_stage

# call-stage -> stage record it folds into
_TARGET = {
    "reduction": "reduction",
    "plan": "analysis",
    "analyze": "analysis",
    "ingest": "assembly",
    "convert": "assembly",
}


def _sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _derived_params(stage, state):
    """Provenance the orchestrator knows from the state (not from the tool).

    These are the resolved inputs we handed the tool, recorded so each stage
    record is self-contained and replays the run. The tool's own manifest
    ``params`` take precedence when keys overlap.
    """
    out = {}
    if stage == "reduction":
        template = _get(state, "inputs", "operator", "template_file")
        if template:
            out["template_file"] = template
            sha = _sha256(template)
            if sha:
                out["template_sha256"] = sha
    elif stage in ("ingest", "convert"):
        nexus = _get(state, "inputs", "derived", "nexus_file")
        reduced = _get(state, "stages", "reduction", "artifacts", "partial_file")
        if nexus:
            out["nexus_input"] = nexus
        if reduced:
            out["reduced_input"] = reduced
        if _get(state, "stages", "analysis", "status") == "ok":
            model = _get(state, "stages", "analysis", "artifacts", "problem_json")
            if model:
                out["model_input"] = model
    return out


def merge_in(stage, state, manifest, exit_code=0, output_prefix=None):
    """Fold *manifest* into *state* for *stage*; return the updated state.

    *output_prefix* (e.g. the operator ``$OUTPUT_DIR``), when given, canonicalizes
    realpath'd paths in the whole state back to that prefix after the merge.
    """
    if stage not in _TARGET:
        raise ValueError("unknown stage: %s" % stage)

    manifest = dict(manifest or {})
    derived = _derived_params(stage, state)
    if derived:
        # Tool-reported params win over orchestrator-derived ones.
        blended = dict(derived)
        blended.update(manifest.get("params") or {})
        manifest["params"] = blended

    merge_stage(state, _TARGET[stage], manifest, exit_code)

    # Record the tool's self-reported version (ndip-tool-result carries a
    # top-level tool_version). Key by the call-stage so the two calls that share
    # a target record (plan+analyze into analysis, ingest+convert into assembly)
    # don't clobber each other. Set after merge_stage so it survives the second
    # call's shallow info-update.
    tool_version = manifest.get("tool_version")
    if tool_version:
        info = state["stages"][_TARGET[stage]].setdefault("info", {})
        info.setdefault("tool_versions", {})[stage] = {
            "tool": manifest.get("tool"),
            "version": tool_version,
        }

    if output_prefix:
        # canonicalize_paths returns a new structure; keep merge_in's in-place
        # contract (consistent with merge_stage) by writing the result back.
        canonical = canonicalize_paths(state, output_prefix)
        if canonical is not state:
            state.clear()
            state.update(canonical)
    return state
