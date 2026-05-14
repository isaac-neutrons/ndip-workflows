"""Tests for the ndip_state workflow-state module."""

import json

import pytest

from ndip_state.state import (
    SCHEMA_VERSION,
    emit_env,
    empty_state,
    load_state,
    main,
    migrate_v0_to_v1,
    record_error,
    save_state,
    update_stage,
)


def test_empty_state_has_v1_skeleton():
    s = empty_state()
    assert s["schema_version"] == SCHEMA_VERSION
    assert s["paths"] == {}
    assert s["llm"] == {}
    assert s["reduction"] == {"success": None, "metadata": {}}
    assert s["analysis"] == {"success": None, "metadata": {}}
    assert s["assembly"] == {"success": None, "metadata": {}}
    assert s["errors"] == []


def test_migrate_v0_flat_to_v1():
    v0 = {
        "run": 226644,
        "sequence_total": 3,
        "prompt": "Cu / Ti / Si in D2O",
        "data_directory": "/SNS/REF_L/IPTS-36897/nexus",
        "output_directory": "/SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5",
        "template_file": "/SNS/template_down.xml",
        "context_file": "/SNS/context.md",
        "event_file": "/SNS/REF_L_226644.nxs.h5",
        "input_file": "/SNS/REF_L_226644.nxs.h5",
        "raw_data": "/SNS/REF_L_226644.nxs.h5",
        "export_path": "/SNS/export.gz",
        "llm_provider": "local",
        "llm_model": "gpt-4",
        "llm_base_url": "https://example.com/openai/v1/",
        "result_file": "/SNS/partial.txt",
        "partial_file": "/SNS/partial.txt",
        "combined_file": "/SNS/combined.txt",
        "model_available": True,
        "final_model": "/SNS/results/Cu-D2O-226642/problem.json",
    }
    s = migrate_v0_to_v1(v0)
    assert s["schema_version"] == "1"
    assert s["run"] == 226644
    assert s["sequence_total"] == 3
    assert s["prompt"] == "Cu / Ti / Si in D2O"
    assert s["paths"]["event_file"] == "/SNS/REF_L_226644.nxs.h5"
    assert s["paths"]["data_directory"] == "/SNS/REF_L/IPTS-36897/nexus"
    assert s["paths"]["export_path"] == "/SNS/export.gz"
    assert s["llm"]["provider"] == "local"
    assert s["llm"]["model"] == "gpt-4"
    assert s["llm"]["base_url"] == "https://example.com/openai/v1/"
    assert s["reduction"]["result_file"] == "/SNS/partial.txt"
    assert s["reduction"]["combined_file"] == "/SNS/combined.txt"
    assert s["analysis"]["success"] is True
    assert s["analysis"]["problem_json"] == "/SNS/results/Cu-D2O-226642/problem.json"


def test_migrate_preserves_unknown_top_level_keys():
    s = migrate_v0_to_v1({"some_future_key": {"x": 1}, "run": 42})
    assert s["some_future_key"] == {"x": 1}
    assert s["run"] == 42


def test_load_state_v0_input(tmp_path):
    p = tmp_path / "v0.json"
    p.write_text(json.dumps({"event_file": "/a.h5"}))
    s = load_state(str(p))
    assert s["schema_version"] == "1"
    assert s["paths"]["event_file"] == "/a.h5"


def test_load_state_v1_input(tmp_path):
    p = tmp_path / "v1.json"
    src = empty_state()
    src["paths"]["event_file"] = "/b.h5"
    src["reduction"]["success"] = True
    save_state(src, str(p))
    s = load_state(str(p))
    assert s["paths"]["event_file"] == "/b.h5"
    assert s["reduction"]["success"] is True


def test_load_state_missing_path_returns_empty():
    assert load_state("")["schema_version"] == "1"
    assert load_state(None)["schema_version"] == "1"


def test_update_stage_shallow_merges_metadata():
    s = empty_state()
    update_stage(s, "reduction", success=True, result_file="/r.txt", metadata={"foo": 1})
    update_stage(s, "reduction", metadata={"bar": 2})
    assert s["reduction"]["success"] is True
    assert s["reduction"]["result_file"] == "/r.txt"
    assert s["reduction"]["metadata"] == {"foo": 1, "bar": 2}


def test_record_error_appends():
    s = empty_state()
    record_error(s, "analysis", "oops", 1)
    record_error(s, "assembly", "bad", 2)
    assert len(s["errors"]) == 2
    assert s["errors"][0]["stage"] == "analysis"
    assert s["errors"][1]["exit_code"] == 2


def test_emit_env_writes_known_vars(tmp_path):
    s = empty_state()
    s["paths"]["event_file"] = "/data/a.h5"
    s["paths"]["output_directory"] = "/out"
    s["llm"]["model"] = "gpt-4"
    s["reduction"]["result_file"] = "/r.txt"
    s["analysis"]["success"] = True
    s["analysis"]["problem_json"] = "/p.json"

    env = tmp_path / "_env.sh"
    emit_env(s, str(env))
    txt = env.read_text()
    assert "export EVENT_FILE=/data/a.h5" in txt
    assert "export OUTPUT_DIR=/out" in txt
    assert "export LLM_MODEL=gpt-4" in txt
    assert "export REFLECTIVITY_FILE=/r.txt" in txt
    assert "export FINAL_MODEL=/p.json" in txt
    assert "export MODEL_AVAILABLE=1" in txt


def test_emit_env_quotes_shell_metachars(tmp_path):
    s = empty_state()
    s["prompt"] = "back ; rm -rf /"
    env = tmp_path / "_env.sh"
    emit_env(s, str(env))
    assert "'back ; rm -rf /'" in env.read_text()


def test_emit_env_empty_state(tmp_path):
    env = tmp_path / "_env.sh"
    emit_env(empty_state(), str(env))
    txt = env.read_text()
    assert "export EVENT_FILE=''" in txt
    assert "export MODEL_AVAILABLE=0" in txt


def test_save_load_roundtrip_preserves_v1(tmp_path):
    s = empty_state()
    s["run"] = 1234
    s["paths"]["event_file"] = "/x.h5"
    p = tmp_path / "state.json"
    save_state(s, str(p))
    s2 = load_state(str(p))
    assert s2["run"] == 1234
    assert s2["paths"]["event_file"] == "/x.h5"
    assert s2["schema_version"] == "1"


def test_cli_parse_config_emits_env(tmp_path):
    config = tmp_path / "config.json"
    s = empty_state()
    s["paths"]["event_file"] = "/e.h5"
    s["paths"]["output_directory"] = "/out"
    save_state(s, str(config))
    env_out = tmp_path / "_env.sh"
    main(["parse-config", str(config), str(env_out)])
    txt = env_out.read_text()
    assert "export EVENT_FILE=/e.h5" in txt
    assert "export OUTPUT_DIR=/out" in txt


def test_cli_parse_config_accepts_empty_path(tmp_path):
    env_out = tmp_path / "_env.sh"
    main(["parse-config", "", str(env_out)])
    txt = env_out.read_text()
    assert "export EVENT_FILE=''" in txt


def test_cli_merge_reduction(tmp_path):
    config = tmp_path / "config.json"
    save_state(empty_state(), str(config))
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"partial_file": "/p.txt", "combined_file": "/c.txt"}))
    out = tmp_path / "out.json"
    main(["merge-reduction", str(config), str(summary), str(out)])
    s = load_state(str(out))
    assert s["reduction"]["success"] is True
    assert s["reduction"]["partial_file"] == "/p.txt"
    assert s["reduction"]["combined_file"] == "/c.txt"
    assert s["reduction"]["result_file"] == "/p.txt"


def test_cli_merge_reduction_propagates_raw_data(tmp_path):
    config = tmp_path / "config.json"
    seed = empty_state()
    seed["paths"]["event_file"] = "/event.h5"
    save_state(seed, str(config))
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"partial_file": "/p.txt", "combined_file": "/c.txt"}))
    out = tmp_path / "out.json"
    main(["merge-reduction", str(config), str(summary), str(out)])
    s = load_state(str(out))
    assert s["paths"]["raw_data"] == "/event.h5"


def test_cli_merge_reduction_from_v0_input(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"event_file": "/legacy.h5"}))
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"partial_file": "/p.txt", "combined_file": "/c.txt"}))
    out = tmp_path / "out.json"
    main(["merge-reduction", str(config), str(summary), str(out)])
    s = load_state(str(out))
    assert s["schema_version"] == "1"
    assert s["paths"]["event_file"] == "/legacy.h5"
    assert s["paths"]["raw_data"] == "/legacy.h5"
    assert s["reduction"]["success"] is True


def test_cli_merge_analyzer_success(tmp_path):
    config = tmp_path / "config.json"
    save_state(empty_state(), str(config))
    out = tmp_path / "out.json"
    main([
        "merge-analyzer",
        str(config),
        "0",
        "Cu-D2O-226642",
        "/results/Cu-D2O-226642/problem.json",
        str(out),
    ])
    s = load_state(str(out))
    assert s["analysis"]["success"] is True
    assert s["analysis"]["model_name"] == "Cu-D2O-226642"
    assert s["analysis"]["problem_json"] == "/results/Cu-D2O-226642/problem.json"
    assert s["errors"] == []


def test_cli_merge_analyzer_failure(tmp_path):
    config = tmp_path / "config.json"
    save_state(empty_state(), str(config))
    out = tmp_path / "out.json"
    main(["merge-analyzer", str(config), "1", "", "", str(out)])
    s = load_state(str(out))
    assert s["analysis"]["success"] is False
    assert s["errors"][0]["stage"] == "analysis"
    assert s["errors"][0]["exit_code"] == 1


def test_cli_merge_assembler_success(tmp_path):
    config = tmp_path / "config.json"
    save_state(empty_state(), str(config))
    out = tmp_path / "out.json"
    main([
        "merge-assembler",
        str(config),
        "0",
        "/path/isaac_record_226644.json",
        str(out),
    ])
    s = load_state(str(out))
    assert s["assembly"]["success"] is True
    assert s["assembly"]["isaac_record"] == "/path/isaac_record_226644.json"


def test_cli_merge_assembler_failure(tmp_path):
    config = tmp_path / "config.json"
    save_state(empty_state(), str(config))
    out = tmp_path / "out.json"
    main(["merge-assembler", str(config), "2", "", str(out)])
    s = load_state(str(out))
    assert s["assembly"]["success"] is False
    assert s["errors"][0]["stage"] == "assembly"
    assert s["errors"][0]["exit_code"] == 2
