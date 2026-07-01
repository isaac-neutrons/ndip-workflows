"""``ndip-package`` — gather a reproducible provenance package from the state.

Reads a final workflow-state JSON and copies the scattered analysis artifacts
into one organized, git-storable directory: the reproduction *core* (inputs,
plan, model, compact fit results), the human-facing reports, the AI-ready
record, plus a ``MANIFEST.json`` (roles + sha256 + tool versions) and a
``REPRODUCE.md`` runbook.

The pipeline has non-deterministic steps (``plan-data``/``create-model`` are
LLM-driven, ``run-fit``/``aure`` are MCMC), so the package is
**frozen-artifact-authoritative**: it freezes the small artifacts that *are* the
answer and records the inputs / LLM endpoint / tool versions so a re-run can be
*compared*, not bit-verified.

Every artifact is resolved from the paths recorded in the state (anchored on
``dirname(stages.analysis.artifacts.problem_json)``), never reconstructed from
directory conventions — so it works for both the flat tool layout and
``ndip-run``'s per-run subdirs, and for both analyzer backends:

  - **simple** (``analyze-sample``): ``models/<model>.py`` + ``results/<model>/``
    (``problem.json`` + ``.par``/``.err``/``.out``) + ``reports/``.
  - **aure**: ``problem.json`` at the results-dir top, an agentic ``checkpoints/``
    trail, and no model script / reports dir (skipped, not an error).

Large binaries (raw NeXus, parquet) and bulky regenerable byproducts (MCMC
chains, plots, per-model ``.dat``, large AuRE checkpoint ``.json``) are recorded
by *reference* (path + sha256) rather than copied, keeping the package small.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version

from .adapters import _sha256
from .projection import _get, _rundir
from .state import load_state, overall_status


# Compact fit results kept from the analysis output dir (allow-list; everything
# else there is regenerable and referenced/omitted).
_RESULT_ALLOW = ("problem.json", "problem.par", "problem.err", "problem.out", "run_info.json")

# Scientific data products kept wherever they live under the analysis dir
# (top-level for simple; nested in refl1d_output/ for AuRE), matched by suffix:
# reflectivity curves, SLD profiles, parameter uncertainties, experiment
# metadata. The heavier/regenerable siblings (-slabs.dat, -steps.dat, *.png,
# *.mc.gz) are deliberately not matched.
_DATA_SUFFIXES = ("-refl.dat", "-profile.dat", "-expt.json", "-err.json")

# Bulky files in the analysis dir referenced (not copied) unless --include-bulky.
_RESULT_REFERENCE = ("final_state.json",)

# Packages whose versions we stamp at package time (installed by .[workflow]).
_PKGS = ("nr-analyzer", "aure", "refl1d", "bumps", "data-assembler", "nr-isaac-format")

MANIFEST_VERSION = "1"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _copy_into(files, notes, root, src, rel, role, stage, tool=None):
    """Copy *src* into ``root/rel``, hash it, and record a manifest entry."""
    if not src or not os.path.isfile(src):
        notes.append("missing %s artifact (%s): %r" % (role, stage, src))
        return False
    dest = os.path.join(root, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    files.append({
        "path": rel, "role": role, "packaged": True,
        "sha256": _sha256(dest), "bytes": _size(dest),
        "source_abspath": os.path.abspath(src),
        "produced_by_stage": stage, "tool": tool,
    })
    return True


def _reference(files, src, role, stage, note, tool=None, record_missing=False):
    """Record *src* by reference (path + sha256) without copying it.

    With *record_missing*, still record the intended path when the file is not
    reachable on the packaging host (e.g. the raw NeXus that lives at the
    facility) — the path is provenance even without a local hash.
    """
    if not src:
        return False
    exists = os.path.exists(src)
    if not exists and not record_missing:
        return False
    files.append({
        "path": None, "role": role, "packaged": False, "reference": True,
        "sha256": _sha256(src) if exists and os.path.isfile(src) else "",
        "bytes": _size(src),
        "source_abspath": os.path.abspath(src),
        "produced_by_stage": stage, "tool": tool,
        "note": note if exists else note + " (not reachable on packaging host)",
    })
    return True


def _copytree_into(files, root, src_dir, rel, role, stage):
    """Copy a whole tree (used by --include-bulky) and hash every file."""
    if not src_dir or not os.path.isdir(src_dir):
        return
    for base, _dirs, names in os.walk(src_dir):
        for name in names:
            s = os.path.join(base, name)
            r = os.path.join(rel, os.path.relpath(s, src_dir))
            _copy_into(files, [], root, s, r, role, stage)


def _partials_from_job(job_yaml):
    """Return the reduced-partial basenames listed under the plan's states[].data."""
    if not job_yaml or not os.path.isfile(job_yaml):
        return []
    try:
        import yaml  # base dependency of this package
        doc = yaml.safe_load(open(job_yaml).read())
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    out = []
    for st in doc.get("states") or []:
        if isinstance(st, dict):
            out.extend(d for d in (st.get("data") or []) if isinstance(d, str))
    return out


def _final_fit_dir(refl1d_output):
    """The final (accepted) fit iteration under an AuRE ``refl1d_output/`` tree.

    AuRE writes one ``fit_iter<N>_<method>/`` per refinement pass; the highest N
    is the accepted solution (its problem.json is the one AuRE copies to the top
    of the output dir). Earlier iterations are rejected solutions we drop. Falls
    back to the sole subdir if the names don't parse.
    """
    subdirs = [d for d in os.listdir(refl1d_output)
               if os.path.isdir(os.path.join(refl1d_output, d))]
    numbered = [(int(m.group(1)), d) for d in subdirs
                for m in [re.search(r"fit_iter(\d+)", d)] if m]
    if numbered:
        return os.path.join(refl1d_output, max(numbered)[1])
    if len(subdirs) == 1:
        return os.path.join(refl1d_output, subdirs[0])
    return None


def _detect_backend(analysis_dir, models_dir):
    if models_dir and os.path.isdir(models_dir) and any(
        f.endswith(".py") for f in os.listdir(models_dir)
    ):
        return "simple"
    if analysis_dir and os.path.isdir(os.path.join(analysis_dir, "checkpoints")):
        return "aure"
    return "unknown"


def _collect_versions(state, isaac_record):
    env = {}
    for pkg in _PKGS:
        try:
            env[pkg] = version(pkg)
        except PackageNotFoundError:
            env[pkg] = None
    state_versions = {
        "analysis": _get(state, "stages", "analysis", "info", "tool_versions") or {},
        "assembly": _get(state, "stages", "assembly", "info", "tool_versions") or {},
    }
    stamped = _versions_from_isaac(isaac_record)
    return {"env": env, "state": state_versions, "stamped": stamped,
            "python": sys.version.split()[0]}


def _versions_from_isaac(isaac_record):
    """Harvest generated_by {agent, version} stamps already frozen in the record."""
    if not isaac_record or not os.path.isfile(isaac_record):
        return {}
    try:
        doc = json.load(open(isaac_record))
    except (OSError, json.JSONDecodeError):
        return {}
    seen = {}
    for out in (((doc.get("descriptors") or {}).get("outputs")) or []):
        gb = out.get("generated_by") or {}
        if gb.get("agent"):
            seen[gb["agent"]] = gb.get("version")
    return seen


def _reproducibility(state):
    llm = _get(state, "inputs", "operator", "llm") or {}
    return {
        "llm": {"provider": llm.get("provider"), "model": llm.get("model"),
                "base_url": llm.get("base_url")},
        "prompt": _get(state, "inputs", "operator", "prompt") or None,
        "seed": None,  # run-fit --seed is not captured in state (see REPRODUCE.md)
        "non_deterministic": ["plan-data (LLM)", "create-model Mode B (LLM)",
                              "run-fit / aure (MCMC)"],
        "note": "Frozen-artifact-authoritative: LLM/MCMC steps do not reproduce "
                "bit-for-bit; compare a re-run against the frozen artifacts.",
    }


def run_package(state, package_dir, include_reports=True, include_ai_ready=True,
                copy_parquet=False, copy_nexus=False, include_bulky=False):
    """Build a provenance package under *package_dir*; return (files, notes, meta)."""
    os.makedirs(package_dir, exist_ok=True)
    files, notes = [], []

    # state.json — the ledger, self-describing root.
    state_path = os.path.join(package_dir, "state.json")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
    files.append({"path": "state.json", "role": "STATE", "packaged": True,
                  "sha256": _sha256(state_path), "bytes": _size(state_path),
                  "source_abspath": None, "produced_by_stage": "orchestrator",
                  "tool": "ndip-run"})

    # inputs/ — context file + the reduced partials the plan consumed.
    context = _get(state, "inputs", "operator", "context_file")
    if context:
        _copy_into(files, notes, package_dir, context,
                   "inputs/" + os.path.basename(context), "INPUT", "operator")

    job_yaml = _get(state, "stages", "analysis", "artifacts", "job_yaml")
    partial = _get(state, "stages", "reduction", "artifacts", "partial_file")
    reduced_dir = os.path.dirname(partial) if partial else \
        (_get(state, "inputs", "operator", "output_directory") or "")
    for base in _partials_from_job(job_yaml):
        src = os.path.join(reduced_dir, base) if not os.path.isabs(base) else base
        _copy_into(files, notes, package_dir, src, "inputs/" + os.path.basename(base),
                   "INPUT", "reduction", tool="simple-reduction")

    # plan/ — the LLM plan (both backends run plan-data first).
    if job_yaml:
        _copy_into(files, notes, package_dir, job_yaml,
                   "plan/" + os.path.basename(job_yaml), "PLAN", "analysis", tool="plan-data")

    # model/ — the refl1d script (simple backend only).
    models_dir = _get(state, "stages", "analysis", "artifacts", "models_dir")
    model_name = _get(state, "stages", "analysis", "params", "model_name")
    if models_dir and os.path.isdir(models_dir):
        script = os.path.join(models_dir, "%s.py" % model_name) if model_name else ""
        if not (script and os.path.isfile(script)):
            pys = [f for f in os.listdir(models_dir) if f.endswith(".py")]
            if len(pys) == 1:
                script = os.path.join(models_dir, pys[0])
                notes.append("model script %s.py not found; used sole %s" % (model_name, pys[0]))
        _copy_into(files, notes, package_dir, script,
                   "model/" + os.path.basename(script), "MODEL", "analysis", tool="create-model")
    else:
        notes.append("no model script (aure backend or models_dir absent)")

    # results/ — anchored on the analysis output dir (works for both backends).
    problem_json = _get(state, "stages", "analysis", "artifacts", "problem_json")
    analysis_dir = os.path.dirname(problem_json) if problem_json else ""
    backend = _detect_backend(analysis_dir, models_dir)
    if analysis_dir and os.path.isdir(analysis_dir):
        if include_bulky:
            _copytree_into(files, package_dir, analysis_dir, "results", "RESULT", "analysis")
        else:
            for name in _RESULT_ALLOW:
                src = os.path.join(analysis_dir, name)
                if os.path.isfile(src):
                    _copy_into(files, notes, package_dir, src, "results/" + name,
                               "RESULT", "analysis", tool="run-fit")
            # Scientific data products (reflectivity curves, SLD profiles,
            # uncertainties, experiment metadata) directly under the analysis dir
            # — the simple backend writes these here (one fit, all final).
            for name in sorted(os.listdir(analysis_dir)):
                src = os.path.join(analysis_dir, name)
                if os.path.isfile(src) and name.endswith(_DATA_SUFFIXES):
                    _copy_into(files, notes, package_dir, src, "results/" + name,
                               "RESULT", "analysis", tool="run-fit")
            # AuRE writes per-iteration fits under refl1d_output/; keep only the
            # FINAL (accepted) iteration's data products. Earlier iterations are
            # rejected solutions — their reasoning lives in the checkpoint .md trail.
            ro = os.path.join(analysis_dir, "refl1d_output")
            if os.path.isdir(ro):
                final = _final_fit_dir(ro)
                if final:
                    for base, _dirs, names in os.walk(final):
                        for name in names:
                            if name.endswith(_DATA_SUFFIXES):
                                src = os.path.join(base, name)
                                rel = os.path.relpath(src, analysis_dir)
                                _copy_into(files, notes, package_dir, src,
                                           os.path.join("results", rel), "RESULT", "analysis", tool="run-fit")
                    notes.append("refl1d_output/: kept final iteration %r data curves; "
                                 "earlier (rejected) iterations + chains/plots/-slabs/-steps omitted"
                                 % os.path.basename(final))
                else:
                    notes.append("refl1d_output/: could not identify the final iteration; "
                                 "curves omitted (use --include-bulky for all)")
            # AuRE agentic trail: copy the tiny .md summaries, reference the big .json.
            ck = os.path.join(analysis_dir, "checkpoints")
            if os.path.isdir(ck):
                for fn in sorted(os.listdir(ck)):
                    src = os.path.join(ck, fn)
                    if not os.path.isfile(src):
                        continue
                    if fn.endswith(".md"):
                        _copy_into(files, notes, package_dir, src, "results/checkpoints/" + fn,
                                   "RESULT", "analysis", tool="aure")
                    else:
                        _reference(files, src, "RESULT", "analysis",
                                   "AuRE checkpoint (bulky) — referenced", tool="aure")
            for name in _RESULT_REFERENCE:
                _reference(files, os.path.join(analysis_dir, name), "RESULT", "analysis",
                           "regenerable/bulky — referenced")
    else:
        notes.append("analysis output dir not found: %r" % analysis_dir)

    # reports/ — simple backend only.
    reports_dir = _get(state, "stages", "analysis", "artifacts", "reports_dir")
    if include_reports and reports_dir and os.path.isdir(reports_dir):
        for base, _dirs, names in os.walk(reports_dir):
            for name in names:
                s = os.path.join(base, name)
                rel_in = os.path.relpath(s, reports_dir)
                # drop the leading dot so .pipeline_state.json is visible in the pkg
                rel_in = rel_in.replace(".pipeline_state.json", "pipeline_state.json")
                _copy_into(files, notes, package_dir, s, os.path.join("reports", rel_in),
                           "REPORT", "analysis")
    elif include_reports:
        notes.append("no reports dir (aure backend or reports_dir absent)")

    # ai-ready/ — isaac_record (both backends); parquet referenced by default.
    isaac_record = _get(state, "stages", "assembly", "artifacts", "isaac_record")
    if include_ai_ready and _get(state, "stages", "assembly", "status") == "ok":
        _copy_into(files, notes, package_dir, isaac_record,
                   "ai-ready/" + os.path.basename(isaac_record) if isaac_record else "",
                   "AI-READY", "assembly", tool="nr-isaac-format")
        for kind, pq in (_get(state, "stages", "assembly", "artifacts", "parquet_files") or {}).items():
            if copy_parquet:
                _copy_into(files, notes, package_dir, pq,
                           "ai-ready/parquet/%s/%s" % (kind, os.path.basename(pq)),
                           "AI-READY", "assembly", tool="data-assembler")
            else:
                _reference(files, pq, "AI-READY", "assembly",
                           "parquet (binary) — referenced; --copy-parquet to embed",
                           tool="data-assembler", record_missing=True)

    # Reference (or copy) the raw NeXus.
    nexus = _get(state, "inputs", "derived", "nexus_file")
    if nexus:
        if copy_nexus:
            _copy_into(files, notes, package_dir, nexus, "inputs/" + os.path.basename(nexus),
                       "INPUT", "acquisition")
        else:
            _reference(files, nexus, "INPUT", "acquisition",
                       "raw NeXus (large binary) — referenced; --copy-nexus to embed",
                       record_missing=True)

    meta = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": _now(),
        "created_by": {"tool": "ndip-package", "version": _self_version()},
        "analysis_backend": backend,
        "state_schema_version": state.get("schema_version"),
        "options": {"include_reports": include_reports, "include_ai_ready": include_ai_ready,
                    "copy_parquet": copy_parquet, "copy_nexus": copy_nexus,
                    "include_bulky": include_bulky},
        "workflow": state.get("workflow") or {},
        "model_name": model_name,
        "overall_status": overall_status(state),
        "tool_versions": _collect_versions(state, isaac_record),
        "reproducibility": _reproducibility(state),
        "notes": notes,
    }
    _write_manifest(package_dir, meta, files)
    _write_reproduce(package_dir, meta, state, backend)
    return files, notes, meta


def _self_version():
    try:
        return version("yaml-parser")
    except PackageNotFoundError:
        return None


def _write_manifest(package_dir, meta, files):
    manifest = dict(meta)
    manifest["files"] = files
    with open(os.path.join(package_dir, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=2)


def _write_reproduce(package_dir, meta, state, backend):
    wf = meta["workflow"]
    llm = meta["reproducibility"]["llm"]
    lines = [
        "# Reproducing %s" % (meta.get("model_name") or "analysis"),
        "",
        "Run %s / %s / %s. Package built %s by ndip-package %s (backend: %s)."
        % (wf.get("run"), wf.get("instrument"), wf.get("ipts"),
           meta["created_at"], meta["created_by"]["version"], backend),
        "See MANIFEST.json for every file's role + sha256 + tool versions.",
        "",
        "## This package is FROZEN-ARTIFACT-AUTHORITATIVE",
        "Non-deterministic steps (LLM planning/modeling, MCMC fitting) will NOT",
        "reproduce bit-for-bit. The copied artifacts are the record of record;",
        "use the commands below to REGENERATE and COMPARE.",
        "  LLM endpoint: provider=%s model=%s base_url=%s"
        % (llm.get("provider"), llm.get("model"), llm.get("base_url")),
        "  fit seed: not recorded in state (run-fit --seed is not persisted).",
        "",
        "## Environment",
        "    pip install 'ndip-workflows[workflow]'",
        "    # versions that ran: see MANIFEST.tool_versions",
        "",
        "## Steps",
        "1. plan  (LLM — expect drift):",
        "     plan-data inputs/<partial> inputs/<context> --output-dir plan --sequence-total N \\",
        "       --llm-provider %s --llm-model %s --llm-base-url %s"
        % (llm.get("provider"), llm.get("model"), llm.get("base_url")),
    ]
    if backend == "aure":
        lines += [
            "2. analyze  (AuRE agentic; LLM + MCMC — expect drift):",
            "     aure analyze -c plan/<job>.yaml -o results/",
            "   Authoritative frozen output: results/problem.json (+ checkpoints/*.md trail)",
        ]
    else:
        lines += [
            "2. create-model (deterministic from an explicit stack, else LLM):",
            "     create-model plan/<job>.yaml -o model/",
            "3. fit  (MCMC — expect stochastic variation):",
            "     run-fit model/<model>.py --results-dir results/",
            "   Authoritative frozen results: results/problem.{json,par,err,out}",
        ]
    lines += [
        "",
        "## assemble  (deterministic)",
        "     data-assembler ingest -o assembled/ --reduced inputs/<partial> --model results/problem.json --nexus-file <NEXUS>",
        "     nr-isaac-format convert-ingest assembled/ --raw <NEXUS> --reduced inputs/<partial>",
        "   Authoritative frozen output: ai-ready/isaac_record_<run>.json",
        "   (<NEXUS> and parquet are REFERENCED in MANIFEST — fetch + verify sha256.)",
        "",
        "## Comparing a re-run",
        "Diff a new results/problem.par against the frozen one; parameters should",
        "agree within the uncertainties in results/problem.err. Larger divergence",
        "points to an environment change (see MANIFEST.tool_versions).",
    ]
    with open(os.path.join(package_dir, "REPRODUCE.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _default_package_dir(state):
    rd = _rundir(state)
    if rd:
        return os.path.join(rd, "provenance")
    model = _get(state, "stages", "analysis", "params", "model_name") or "provenance"
    return os.path.join("provenance", model)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ndip-package",
        description="Gather a reproducible provenance package from a workflow state.",
    )
    parser.add_argument("--state", required=True, help="Path to the final workflow-state JSON.")
    parser.add_argument("--out", "-o", default=None,
                        help="Package directory (default: <output_directory>/<run>/provenance).")
    parser.add_argument("--no-reports", action="store_true", help="Skip the reports/ tree.")
    parser.add_argument("--no-ai-ready", action="store_true", help="Skip the isaac_record.")
    parser.add_argument("--copy-parquet", action="store_true", help="Embed parquet (else referenced).")
    parser.add_argument("--copy-nexus", action="store_true", help="Embed the raw NeXus (else referenced).")
    parser.add_argument("--include-bulky", action="store_true",
                        help="Copy the whole analysis output tree (MCMC byproducts included).")
    parser.add_argument("--force", action="store_true", help="Overwrite a non-empty package dir.")
    args = parser.parse_args(argv)

    state = load_state(args.state)
    package_dir = args.out or _default_package_dir(state)

    if os.path.isdir(package_dir) and os.listdir(package_dir) and not args.force:
        raise SystemExit("package dir %r is not empty; use --force to overwrite" % package_dir)

    files, notes, meta = run_package(
        state, package_dir,
        include_reports=not args.no_reports,
        include_ai_ready=not args.no_ai_ready,
        copy_parquet=args.copy_parquet,
        copy_nexus=args.copy_nexus,
        include_bulky=args.include_bulky,
    )
    packaged = sum(1 for f in files if f.get("packaged"))
    referenced = sum(1 for f in files if not f.get("packaged"))
    sys.stderr.write("ndip-package: wrote %s (%d files, %d referenced, backend=%s)\n"
                     % (package_dir, packaged, referenced, meta["analysis_backend"]))
    for n in notes:
        sys.stderr.write("  note: %s\n" % n)


if __name__ == "__main__":
    main()
