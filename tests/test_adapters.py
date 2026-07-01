"""Tests for ``ndip_state.adapters`` — merging tool manifests into state."""

from __future__ import annotations

from ndip_state.adapters import merge_in
from ndip_state.state import empty_state


def _seeded():
    s = empty_state()
    s["inputs"]["operator"]["output_directory"] = "/out/sample5"
    s["inputs"]["derived"]["nexus_file"] = "/nexus/REF_L_226644.nxs.h5"
    return s


def test_reduction_merge_records_artifacts_and_template_provenance(tmp_path):
    template = tmp_path / "template_down.xml"
    template.write_text("<reduction/>")
    s = _seeded()
    s["inputs"]["operator"]["template_file"] = str(template)

    manifest = {
        "tool": "simple-reduction",
        "status": "ok",
        "params": {"q_step": -0.02},
        "artifacts": {"partial_file": "/out/sample5/partial.txt", "combined_file": "/out/sample5/combined.txt"},
        "info": {"first_run_of_set": 226642},
    }
    merge_in("reduction", s, manifest, exit_code=0)

    red = s["stages"]["reduction"]
    assert red["status"] == "ok"
    assert red["artifacts"]["partial_file"] == "/out/sample5/partial.txt"
    assert red["info"]["first_run_of_set"] == 226642
    # tool-reported param preserved
    assert red["params"]["q_step"] == -0.02
    # orchestrator-derived provenance: template path + content hash
    assert red["params"]["template_file"] == str(template)
    assert len(red["params"]["template_sha256"]) == 64


def test_tool_params_win_over_derived(tmp_path):
    template = tmp_path / "t.xml"
    template.write_text("x")
    s = _seeded()
    s["inputs"]["operator"]["template_file"] = str(template)
    manifest = {"status": "ok", "params": {"template_file": "/tool/says/this.xml"}}
    merge_in("reduction", s, manifest)
    assert s["stages"]["reduction"]["params"]["template_file"] == "/tool/says/this.xml"


def test_plan_and_analyze_both_fold_into_analysis():
    s = _seeded()
    merge_in("plan", s, {
        "status": "ok",
        "params": {"model_name": "Cu-D2O", "perform_assembly": True},
        "artifacts": {"job_yaml": "/out/sample5/plan/job.yaml"},
        "info": {"sequence_id": "Cu-D2O"},
    })
    merge_in("analyze", s, {
        "status": "ok",
        "artifacts": {"problem_json": "/out/sample5/results/problem.json"},
        "info": {"pipeline_status": "ok"},
    })
    ana = s["stages"]["analysis"]
    assert ana["status"] == "ok"
    assert ana["params"]["model_name"] == "Cu-D2O"
    assert ana["artifacts"]["job_yaml"].endswith("plan/job.yaml")
    assert ana["artifacts"]["problem_json"].endswith("results/problem.json")
    assert ana["info"]["sequence_id"] == "Cu-D2O"
    assert ana["info"]["pipeline_status"] == "ok"


def test_tool_version_captured_per_call_stage():
    s = _seeded()
    # plan (plan-data) and analyze (a different tool) both fold into analysis;
    # each version is kept under its own call-stage key, not overwritten.
    merge_in("plan", s, {
        "status": "ok", "tool": "plan-data", "tool_version": "0.7.2",
        "artifacts": {"job_yaml": "/out/sample5/plan/job.yaml"},
    })
    merge_in("analyze", s, {
        "status": "ok", "tool": "aure", "tool_version": "0.3.1",
        "artifacts": {"problem_json": "/out/sample5/results/problem.json"},
    })
    versions = s["stages"]["analysis"]["info"]["tool_versions"]
    assert versions["plan"] == {"tool": "plan-data", "version": "0.7.2"}
    assert versions["analyze"] == {"tool": "aure", "version": "0.3.1"}


def test_tool_version_absent_when_manifest_omits_it():
    s = _seeded()
    merge_in("plan", s, {"status": "ok", "artifacts": {"job_yaml": "/j.yaml"}})
    assert "tool_versions" not in s["stages"]["analysis"].get("info", {})


def test_ingest_derives_input_provenance():
    s = _seeded()
    s["stages"]["reduction"]["artifacts"]["partial_file"] = "/out/sample5/partial.txt"
    s["stages"]["analysis"]["status"] = "ok"
    s["stages"]["analysis"]["artifacts"]["problem_json"] = "/out/sample5/results/problem.json"

    merge_in("ingest", s, {
        "status": "ok",
        "artifacts": {"ingest_dir": "/out/sample5/assembled", "parquet_files": {"reflectivity": "/r.parquet"}},
        "info": {"ingest_status": "completed"},
    })
    asm = s["stages"]["assembly"]
    assert asm["params"]["nexus_input"] == "/nexus/REF_L_226644.nxs.h5"
    assert asm["params"]["reduced_input"] == "/out/sample5/partial.txt"
    assert asm["params"]["model_input"] == "/out/sample5/results/problem.json"
    assert asm["artifacts"]["parquet_files"] == {"reflectivity": "/r.parquet"}


def test_ingest_omits_model_input_when_analysis_not_ok():
    s = _seeded()
    s["stages"]["analysis"]["status"] = "skipped"
    merge_in("ingest", s, {"status": "ok", "artifacts": {"ingest_dir": "/d"}})
    assert "model_input" not in s["stages"]["assembly"]["params"]


def test_failure_records_error_and_status():
    s = _seeded()
    merge_in("convert", s, {"status": "failed", "messages": [{"level": "error", "text": "boom"}]}, exit_code=2)
    assert s["stages"]["assembly"]["status"] == "failed"
    assert s["errors"] == [{"stage": "assembly", "message": "boom", "exit_code": 2}]


def test_output_prefix_canonicalizes(tmp_path):
    # /SNS/REF_L -> /gpfs/.../REF_L symlink
    target = tmp_path / "gpfs" / "REF_L"
    target.mkdir(parents=True)
    sns = tmp_path / "SNS"
    sns.mkdir()
    (sns / "REF_L").symlink_to(target, target_is_directory=True)
    canonical = sns / "REF_L" / "sample5"
    canonical.mkdir()
    resolved = str(canonical.resolve())

    s = _seeded()
    s["inputs"]["operator"]["output_directory"] = str(canonical)
    manifest = {"status": "ok", "artifacts": {"problem_json": f"{resolved}/results/problem.json"}}
    merge_in("analyze", s, manifest, output_prefix=str(canonical))

    assert s["stages"]["analysis"]["artifacts"]["problem_json"] == f"{canonical}/results/problem.json"
