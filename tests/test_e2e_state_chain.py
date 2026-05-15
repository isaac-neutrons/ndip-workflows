"""End-to-end smoke test for the v1 state contract across all stages.

The real CLIs (mantid, the LLM-driven plan-data, refl1d, data-assembler,
nr-isaac-format) live in foreign containers and aren't available here.
This test stands in for each stage with the ``merge-*`` subcommands of
``python -m ndip_state.state`` plus a small ``build_state`` seed, and
asserts the contract between stages holds:

  seed-config-shaped seed
      → merge-reduction      (adds reduction.{success, partial_file, combined_file}
                              + paths.raw_data)
      → merge-analyzer       (adds analysis.{success, model_name, problem_json})
      → merge-assembler      (adds assembly.{success, isaac_record})

If a stage ever stops reading or writing a key its downstream successor
expects, this test fails fast — that's the kind of drift the per-stage
unit tests don't catch.
"""

from __future__ import annotations

import json

import pytest

from ndip_state.state import build_state, load_state, main, save_state


def _seed(tmp_path):
    """Build the v1 state that seed-config / yaml-parser would emit."""
    flat = {
        "run": 226644,
        "instrument": "REF_L",
        "ipts": "IPTS-36897",
        "sequence_total": 3,
        "prompt": "Cu / Ti / Si in D2O",
        "data_directory": str(tmp_path / "nexus"),
        "output_directory": str(tmp_path / "shared/isaac/sample5"),
        "template_file": str(tmp_path / "shared/autoreduce/template_down.xml"),
        "context_file": str(tmp_path / "shared/isaac/context.md"),
        "event_file": str(tmp_path / "nexus/REF_L_226644.nxs.h5"),
        "input_file": str(tmp_path / "nexus/REF_L_226644.nxs.h5"),
        "llm_provider": "local",
        "llm_model": "gpt-4",
        "llm_base_url": "https://example/v1/",
    }
    state = build_state(flat)
    path = tmp_path / "seed.json"
    save_state(state, str(path))
    return state, path


def _reduction_summary(tmp_path, partial: str, combined: str):
    """Stand-in for the JSON summary simple-reduction would write."""
    p = tmp_path / "summary.json"
    p.write_text(json.dumps({"partial_file": partial, "combined_file": combined}))
    return p


def test_state_threads_through_all_four_stages(tmp_path):
    seed_state, seed_path = _seed(tmp_path)

    # Stage 1: reduction
    partial = str(tmp_path / "shared/isaac/sample5/REFL_226642_3_226644_partial.txt")
    combined = str(tmp_path / "shared/isaac/sample5/REFL_226642_combined_data_auto.txt")
    summary = _reduction_summary(tmp_path, partial, combined)
    after_reduction = tmp_path / "after_reduction.json"
    main(["merge-reduction", str(seed_path), str(summary), str(after_reduction)])
    s = load_state(str(after_reduction))

    # seed fields preserved
    assert s["schema_version"] == "1"
    assert s["run"] == 226644
    assert s["instrument"] == "REF_L"
    assert s["ipts"] == "IPTS-36897"
    assert s["paths"]["event_file"] == seed_state["paths"]["event_file"]
    assert s["llm"]["model"] == "gpt-4"

    # reduction filled in
    assert s["reduction"]["success"] is True
    assert s["reduction"]["partial_file"] == partial
    assert s["reduction"]["combined_file"] == combined
    # event_file gets promoted to raw_data so the assembler can use it
    assert s["paths"]["raw_data"] == s["paths"]["event_file"]

    # Stage 2: analysis (success)
    model_name = "Cu-D2O-226642"
    problem_json = str(tmp_path / "results" / model_name / "problem.json")
    after_analysis = tmp_path / "after_analysis.json"
    main([
        "merge-analyzer",
        str(after_reduction),
        "0", model_name, problem_json,
        str(after_analysis),
    ])
    s = load_state(str(after_analysis))

    # earlier stage's contributions still there
    assert s["reduction"]["partial_file"] == partial
    assert s["paths"]["raw_data"] == seed_state["paths"]["event_file"]

    # analysis filled in
    assert s["analysis"]["success"] is True
    assert s["analysis"]["model_name"] == model_name
    assert s["analysis"]["problem_json"] == problem_json
    assert s["errors"] == []

    # Stage 3: assembly (success)
    isaac_record = str(tmp_path / "shared/isaac/sample5/assembled/isaac_record_226644.json")
    after_assembly = tmp_path / "after_assembly.json"
    main([
        "merge-assembler",
        str(after_analysis),
        "0", isaac_record,
        str(after_assembly),
    ])
    final = load_state(str(after_assembly))

    # full provenance carried through
    assert final["schema_version"] == "1"
    assert final["run"] == 226644
    assert final["reduction"]["success"] is True
    assert final["reduction"]["partial_file"] == partial
    assert final["analysis"]["success"] is True
    assert final["analysis"]["problem_json"] == problem_json
    assert final["assembly"]["success"] is True
    assert final["assembly"]["isaac_record"] == isaac_record
    assert final["errors"] == []


def test_analysis_failure_records_error_and_blocks_assembly_success(tmp_path):
    """A failed analyzer run must surface as analysis.success=false and
    errors[]; an assembler run that follows without a problem.json must
    still write success=true only if its own work completed (here we
    simulate the assembler also failing — assembly.success=false)."""
    _, seed_path = _seed(tmp_path)

    summary = _reduction_summary(tmp_path, "/p.txt", "/c.txt")
    after_reduction = tmp_path / "r.json"
    main(["merge-reduction", str(seed_path), str(summary), str(after_reduction)])

    after_analysis = tmp_path / "a.json"
    main([
        "merge-analyzer",
        str(after_reduction),
        "1", "", "",  # exit_code=1, no model, no problem_json
        str(after_analysis),
    ])
    s = load_state(str(after_analysis))
    assert s["analysis"]["success"] is False
    assert s["analysis"]["model_name"] is None
    assert s["analysis"]["problem_json"] is None
    assert s["errors"][0]["stage"] == "analysis"
    assert s["errors"][0]["exit_code"] == 1

    # Assembler also fails downstream
    after_assembly = tmp_path / "as.json"
    main([
        "merge-assembler",
        str(after_analysis),
        "2", "",
        str(after_assembly),
    ])
    s = load_state(str(after_assembly))
    assert s["assembly"]["success"] is False
    # Two errors total, both stages recorded in order
    stages = [e["stage"] for e in s["errors"]]
    assert stages == ["analysis", "assembly"]


def test_chain_starting_from_empty_seed(tmp_path):
    """A pipeline kicked off without seed-config (no config_json on stage 1)
    should still produce a coherent final state — every stage either fills its
    block or records an error."""
    summary = _reduction_summary(tmp_path, "/p.txt", "/c.txt")
    after_reduction = tmp_path / "r.json"
    # Empty CONFIG path → merge-reduction starts from empty_state.
    main(["merge-reduction", "", str(summary), str(after_reduction)])
    s = load_state(str(after_reduction))
    assert s["schema_version"] == "1"
    assert s["reduction"]["success"] is True
    # No event_file in the seed → no raw_data alias set
    assert "raw_data" not in s["paths"]
