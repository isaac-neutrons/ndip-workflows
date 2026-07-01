"""Tests for ``ndip_state.package`` — building a provenance package from state.

Two analyzer backends produce different artifact shapes; the packager must fit
both. We build a fake artifact tree + a matching state in ``tmp_path`` for each
and assert what lands in the package.
"""

from __future__ import annotations

import json
import os

from ndip_state.package import run_package, main
from ndip_state.state import empty_state, save_state


def _w(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


_JOB_YAML = """\
describe: test sample
states:
- name: run_226642
  data:
  - REFL_226642_1_226642_partial.txt
  - REFL_226642_2_226643_partial.txt
  - REFL_226642_3_226644_partial.txt
model_name: Cu-D2O
"""


def _base_state(out, ctx):
    s = empty_state()
    s["workflow"] = {"run": 226644, "instrument": "REF_L", "ipts": "IPTS-36897"}
    s["inputs"]["operator"] = {
        "output_directory": out, "context_file": ctx, "sequence_total": 3,
        "llm": {"provider": "local", "model": "gpt-4", "base_url": "https://x/v1/"},
    }
    s["inputs"]["derived"] = {"nexus_file": "/nexus/REF_L_226644.nxs.h5"}
    return s


def _simple_tree(tmp_path):
    out = str(tmp_path / "out")
    ctx = _w(str(tmp_path / "ctx" / "context.md"), "context\n") and str(tmp_path / "ctx" / "context.md")
    # reduced partials at output_directory
    for n in ("REFL_226642_1_226642_partial.txt", "REFL_226642_2_226643_partial.txt",
              "REFL_226642_3_226644_partial.txt"):
        _w(os.path.join(out, n), "0.01 1.0 0.1\n")
    job = _w(os.path.join(out, "plan", "job_226644.yaml"), _JOB_YAML)
    _w(os.path.join(out, "models", "Cu-D2O.py"), "# refl1d model\nproblem = None\n")
    rdir = os.path.join(out, "results", "Cu-D2O")
    for n in ("problem.json", "problem.par", "problem.err", "problem.out", "run_info.json"):
        _w(os.path.join(rdir, n), "{}" if n.endswith(".json") else "x\n")
    # scientific data products that MUST be copied (refl/profile/expt/err)
    for n in ("problem-1-refl.dat", "problem-1-profile.dat", "problem-1-expt.json", "problem-err.json"):
        _w(os.path.join(rdir, n), "data\n")
    # bulky/uninteresting siblings that must NOT be copied by default
    for n in ("problem-chain.mc.gz", "problem-model0.png", "final_state.json",
              "problem-1-slabs.dat", "problem-1-steps.dat"):
        _w(os.path.join(rdir, n), "BULKY")
    _w(os.path.join(out, "reports", "report_Cu-D2O.md"), "# report\n")
    _w(os.path.join(out, "reports", "sample_Cu-D2O", ".pipeline_state.json"), "{}")
    isaac = _w(os.path.join(out, "assembled", "isaac_record_226644.json"), "{}")
    pq = _w(os.path.join(out, "assembled", "reflectivity", "r.parquet"), "PARQUET")

    s = _base_state(out, ctx)
    s["stages"]["reduction"] = {"status": "ok", "params": {},
        "artifacts": {"partial_file": os.path.join(out, "REFL_226642_3_226644_partial.txt")}, "info": {}}
    s["stages"]["analysis"] = {"status": "ok",
        "params": {"model_name": "Cu-D2O"},
        "artifacts": {"job_yaml": job, "problem_json": os.path.join(rdir, "problem.json"),
                      "results_dir": os.path.join(out, "results"),
                      "reports_dir": os.path.join(out, "reports"),
                      "models_dir": os.path.join(out, "models")},
        "info": {"tool_versions": {"plan": {"tool": "plan-data", "version": "0.7.2"}}}}
    s["stages"]["assembly"] = {"status": "ok", "params": {},
        "artifacts": {"isaac_record": isaac, "parquet_files": {"reflectivity": pq}}, "info": {}}
    return s, out


def _aure_tree(tmp_path):
    out = str(tmp_path / "out")
    ctx = str(_w(str(tmp_path / "ctx" / "context.md"), "context\n"))
    _w(os.path.join(out, "REFL_226642_3_226644_partial.txt"), "0.01 1.0 0.1\n")
    job = _w(os.path.join(out, "plan", "job_226644.yaml"), _JOB_YAML)
    # AuRE writes problem.json at the top of the -o dir; no models/, no reports/.
    adir = os.path.join(out, "results")
    _w(os.path.join(adir, "problem.json"), "{}")
    _w(os.path.join(adir, "run_info.json"), "{}")
    _w(os.path.join(adir, "final_state.json"), "BIG")
    _w(os.path.join(adir, "checkpoints", "001_intake.md"), "# intake\n")
    _w(os.path.join(adir, "checkpoints", "001_intake.json"), "BIGJSON")
    _w(os.path.join(adir, "checkpoints", "003_modeling.md"), "# modeling\n")
    # AuRE refinement loop: iter0 (rejected) then iter1 (final/accepted). Only
    # the final iteration's curves should be packaged.
    for it in ("fit_iter0_dream", "fit_iter1_dream"):
        for n in ("x-1-refl.dat", "x-1-profile.dat", "x-1-expt.json", "x-err.json"):
            _w(os.path.join(adir, "refl1d_output", it, n), "data\n")
        _w(os.path.join(adir, "refl1d_output", it, "x-chain.mc.gz"), "BULKY")
    isaac = _w(os.path.join(out, "assembled", "isaac_record_226644.json"), "{}")

    s = _base_state(out, ctx)
    s["stages"]["reduction"] = {"status": "ok", "params": {},
        "artifacts": {"partial_file": os.path.join(out, "REFL_226642_3_226644_partial.txt")}, "info": {}}
    s["stages"]["analysis"] = {"status": "ok", "params": {"model_name": "Cu-D2O"},
        "artifacts": {"job_yaml": job, "problem_json": os.path.join(adir, "problem.json"),
                      "results_dir": adir}, "info": {}}
    s["stages"]["assembly"] = {"status": "ok", "params": {},
        "artifacts": {"isaac_record": isaac}, "info": {}}
    return s, out


def _rel_paths(pkg):
    out = []
    for base, _d, names in os.walk(pkg):
        for n in names:
            out.append(os.path.relpath(os.path.join(base, n), pkg))
    return set(out)


# --- simple backend -----------------------------------------------------------

def test_simple_package_contents(tmp_path):
    s, _ = _simple_tree(tmp_path)
    pkg = str(tmp_path / "pkg")
    files, notes, meta = run_package(s, pkg)
    paths = _rel_paths(pkg)

    assert meta["analysis_backend"] == "simple"
    # core present
    assert "state.json" in paths
    assert "MANIFEST.json" in paths and "REPRODUCE.md" in paths
    assert "inputs/context.md" in paths
    assert "inputs/REFL_226642_1_226642_partial.txt" in paths
    assert "inputs/REFL_226642_3_226644_partial.txt" in paths
    assert "plan/job_226644.yaml" in paths
    assert "model/Cu-D2O.py" in paths
    assert {"results/problem.json", "results/problem.par", "results/problem.err",
            "results/problem.out", "results/run_info.json"} <= paths
    # scientific data products included (refl curves, SLD profiles, err, expt)
    assert {"results/problem-1-refl.dat", "results/problem-1-profile.dat",
            "results/problem-1-expt.json", "results/problem-err.json"} <= paths
    assert "reports/report_Cu-D2O.md" in paths
    # leading-dot report file is renamed so it's visible in the package
    assert "reports/sample_Cu-D2O/pipeline_state.json" in paths
    assert "ai-ready/isaac_record_226644.json" in paths
    # bulky / uninteresting siblings excluded by default
    assert "results/problem-chain.mc.gz" not in paths
    assert "results/problem-model0.png" not in paths
    assert "results/final_state.json" not in paths
    assert "results/problem-1-slabs.dat" not in paths
    assert "results/problem-1-steps.dat" not in paths


def test_simple_manifest_hashes_and_references(tmp_path):
    s, _ = _simple_tree(tmp_path)
    pkg = str(tmp_path / "pkg")
    run_package(s, pkg)
    man = json.load(open(os.path.join(pkg, "MANIFEST.json")))

    for f in man["files"]:
        if f.get("packaged"):
            assert f["sha256"] and len(f["sha256"]) == 64
            assert os.path.isfile(os.path.join(pkg, f["path"]))
    # parquet + raw nexus are referenced, not copied
    refs = [f for f in man["files"] if not f.get("packaged")]
    assert any(f["source_abspath"].endswith("r.parquet") for f in refs)
    assert any(f["source_abspath"].endswith("REF_L_226644.nxs.h5") for f in refs)
    # tool_versions from state carried into the manifest
    assert man["tool_versions"]["state"]["analysis"]["plan"]["version"] == "0.7.2"


def test_include_bulky_copies_everything(tmp_path):
    s, _ = _simple_tree(tmp_path)
    pkg = str(tmp_path / "pkg")
    run_package(s, pkg, include_bulky=True)
    paths = _rel_paths(pkg)
    assert "results/problem-chain.mc.gz" in paths
    assert "results/final_state.json" in paths


def test_no_reports_skips_reports(tmp_path):
    s, _ = _simple_tree(tmp_path)
    pkg = str(tmp_path / "pkg")
    run_package(s, pkg, include_reports=False)
    assert not any(p.startswith("reports/") for p in _rel_paths(pkg))


# --- aure backend -------------------------------------------------------------

def test_aure_package_contents(tmp_path):
    s, _ = _aure_tree(tmp_path)
    pkg = str(tmp_path / "pkg")
    files, notes, meta = run_package(s, pkg)
    paths = _rel_paths(pkg)

    assert meta["analysis_backend"] == "aure"
    assert "results/problem.json" in paths
    assert "results/run_info.json" in paths
    # checkpoint .md summaries copied; big .json referenced not copied
    assert "results/checkpoints/001_intake.md" in paths
    assert "results/checkpoints/003_modeling.md" in paths
    assert "results/checkpoints/001_intake.json" not in paths
    # no model script / reports for aure — absent, but not fatal
    assert not any(p.startswith("model/") for p in paths)
    assert not any(p.startswith("reports/") for p in paths)
    assert any("no model script" in n for n in notes)
    # refl1d_output: ONLY the final iteration's data curves are kept
    assert "results/refl1d_output/fit_iter1_dream/x-1-refl.dat" in paths
    assert "results/refl1d_output/fit_iter1_dream/x-1-profile.dat" in paths
    assert "results/refl1d_output/fit_iter1_dream/x-err.json" in paths
    # the rejected earlier iteration is dropped entirely
    assert not any("fit_iter0_dream" in p for p in paths)
    # MCMC chains + final_state still excluded
    assert not any(p.endswith(".mc.gz") for p in paths)
    assert "results/final_state.json" not in paths
    # isaac_record still copied (both backends)
    assert "ai-ready/isaac_record_226644.json" in paths


# --- robustness ---------------------------------------------------------------

def test_missing_artifacts_recorded_not_fatal(tmp_path):
    out = str(tmp_path / "out")
    s = _base_state(out, str(tmp_path / "nope.md"))
    s["stages"]["analysis"] = {"status": "ok", "params": {"model_name": "X"},
        "artifacts": {"job_yaml": os.path.join(out, "gone", "job.yaml"),
                      "problem_json": os.path.join(out, "gone", "problem.json")}, "info": {}}
    pkg = str(tmp_path / "pkg")
    files, notes, meta = run_package(s, pkg)  # must not raise
    assert os.path.isfile(os.path.join(pkg, "MANIFEST.json"))
    assert notes  # discrepancies recorded


def test_main_writes_package(tmp_path):
    s, _ = _simple_tree(tmp_path)
    state_path = str(tmp_path / "state.json")
    save_state(s, state_path)
    pkg = str(tmp_path / "pkg")
    main(["--state", state_path, "-o", pkg])
    assert os.path.isfile(os.path.join(pkg, "MANIFEST.json"))
    assert os.path.isfile(os.path.join(pkg, "results", "problem.json"))
