"""Tests for ``ndip_state.state`` — the workflow-state document."""

from __future__ import annotations

import json

import pytest

from ndip_state.state import (
    SCHEMA_VERSION,
    build_state,
    empty_state,
    load_state,
    merge_stage,
    overall_status,
    record_error,
    save_state,
)


def test_schema_version_is_2():
    assert SCHEMA_VERSION == "2"


def test_empty_state_skeleton():
    s = empty_state()
    assert s["schema_version"] == "2"
    assert s["workflow"] == {}
    assert s["inputs"] == {"operator": {}, "derived": {}}
    assert set(s["stages"]) == {"reduction", "analysis", "assembly"}
    assert s["stages"]["reduction"] == {
        "status": "pending", "params": {}, "artifacts": {}, "info": {},
    }
    assert s["errors"] == []


def test_build_state_from_flat():
    s = build_state({
        "run": 226644,
        "instrument": "REF_L",
        "ipts": "IPTS-36897",
        "sequence_total": 3,
        "prompt": "Cu on Ti on Si",
        "template_file": "/t.xml",
        "context_file": "/c.md",
        "output_directory": "/out/sample5",
        "event_file": "/nexus/REF_L_226644.nxs.h5",
        "data_directory": "/nexus",
        "ipts_shared_root": "/SNS/REF_L/IPTS-36897/shared",
        "llm_provider": "local",
        "llm_model": "gpt-4",
        "llm_base_url": "https://x/v1/",
    })
    assert s["workflow"] == {"run": 226644, "instrument": "REF_L", "ipts": "IPTS-36897"}
    op = s["inputs"]["operator"]
    assert op["sequence_total"] == 3
    assert op["template_file"] == "/t.xml"
    assert op["llm"] == {"provider": "local", "model": "gpt-4", "base_url": "https://x/v1/"}
    der = s["inputs"]["derived"]
    assert der["nexus_file"] == "/nexus/REF_L_226644.nxs.h5"
    assert der["data_directory"] == "/nexus"
    assert der["ipts_shared_root"] == "/SNS/REF_L/IPTS-36897/shared"


def test_build_state_drops_unknown_keys():
    s = build_state({"run": 1, "future_field": "x"})
    assert s["workflow"]["run"] == 1
    assert "future_field" not in s
    assert "future_field" not in s["workflow"]
    assert "future_field" not in s["inputs"]["operator"]


def test_load_state_empty_path_returns_skeleton():
    assert load_state("")["schema_version"] == "2"
    assert load_state(None)["schema_version"] == "2"


def test_load_state_roundtrip(tmp_path):
    s = build_state({"run": 42, "template_file": "/t.xml"})
    p = tmp_path / "state.json"
    save_state(s, str(p))
    assert load_state(str(p)) == s


def test_load_state_rejects_wrong_schema_version(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"schema_version": "1", "paths": {}}))
    with pytest.raises(ValueError):
        load_state(str(p))


def test_merge_stage_routes_four_fields():
    s = empty_state()
    manifest = {
        "tool": "simple-reduction",
        "status": "ok",
        "params": {"q_step": -0.02, "template_file": "/t.xml"},
        "artifacts": {"partial_file": "/p.txt", "combined_file": "/c.txt"},
        "info": {"first_run_of_set": 226642},
    }
    merge_stage(s, "reduction", manifest, exit_code=0)
    red = s["stages"]["reduction"]
    assert red["status"] == "ok"
    assert red["params"]["q_step"] == -0.02
    assert red["artifacts"]["partial_file"] == "/p.txt"
    assert red["info"]["first_run_of_set"] == 226642
    assert s["errors"] == []


def test_merge_stage_dry_run_is_ok():
    s = empty_state()
    merge_stage(s, "analysis", {"status": "dry-run"}, exit_code=0)
    assert s["stages"]["analysis"]["status"] == "ok"


def test_merge_stage_skipped():
    s = empty_state()
    merge_stage(s, "analysis", {"status": "skipped"}, exit_code=0)
    assert s["stages"]["analysis"]["status"] == "skipped"
    assert s["errors"] == []


def test_merge_stage_failure_records_error():
    s = empty_state()
    manifest = {
        "status": "failed",
        "messages": [{"level": "error", "text": "mantid blew up"}],
    }
    merge_stage(s, "reduction", manifest, exit_code=3)
    assert s["stages"]["reduction"]["status"] == "failed"
    assert s["errors"] == [
        {"stage": "reduction", "message": "mantid blew up", "exit_code": 3}
    ]


def test_merge_stage_nonzero_exit_forces_failure():
    s = empty_state()
    merge_stage(s, "assembly", {"status": "ok"}, exit_code=1)
    assert s["stages"]["assembly"]["status"] == "failed"
    assert s["errors"][0]["stage"] == "assembly"


def test_record_error_appends():
    s = empty_state()
    record_error(s, "reduction", "boom", exit_code=7)
    assert s["errors"] == [{"stage": "reduction", "message": "boom", "exit_code": 7}]


def test_overall_status_rollup():
    s = empty_state()
    assert overall_status(s) == "pending"
    merge_stage(s, "reduction", {"status": "ok"})
    merge_stage(s, "analysis", {"status": "ok"})
    merge_stage(s, "assembly", {"status": "ok"})
    assert overall_status(s) == "ok"
    merge_stage(s, "assembly", {"status": "failed"}, exit_code=2)
    assert overall_status(s) == "failed"
