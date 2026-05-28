"""Tests for ``ndip_state.projection`` — building tool args from state."""

from __future__ import annotations

import pytest

from ndip_state.projection import ProjectionError, project_out, project_out_shell


def _state():
    return {
        "schema_version": "2",
        "inputs": {
            "operator": {
                "output_directory": "/out/sample5",
                "template_file": "/tmpl/template_down.xml",
                "context_file": "/ctx/context.md",
                "sequence_total": 3,
                "llm": {"provider": "local", "model": "gpt-4", "base_url": "https://x/v1/"},
            },
            "derived": {"nexus_file": "/nexus/REF_L_226644.nxs.h5"},
        },
        "stages": {
            "reduction": {"status": "ok", "artifacts": {"partial_file": "/out/sample5/partial.txt"}},
            "analysis": {
                "status": "ok",
                "artifacts": {"job_yaml": "/out/sample5/plan/job.yaml", "problem_json": "/out/sample5/results/problem.json"},
            },
            "assembly": {"status": "pending", "artifacts": {"ingest_dir": "/out/sample5/assembled"}},
        },
    }


def test_reduction_projection():
    assert project_out("reduction", _state()) == [
        "--event-file", "/nexus/REF_L_226644.nxs.h5",
        "--template", "/tmpl/template_down.xml",
        "--output-dir", "/out/sample5",
    ]


def test_plan_projection_positionals_and_llm():
    args = project_out("plan", _state())
    assert args[:2] == ["/out/sample5/partial.txt", "/ctx/context.md"]
    assert "--output-dir" in args and "/out/sample5/plan" in args
    assert "--sequence-total" in args and "3" in args
    assert "--llm-model" in args and "gpt-4" in args


def test_analyze_projection_passes_job_yaml_positionally():
    args = project_out("analyze", _state())
    # job yaml is the positional CONFIG for analyze-sample
    assert args[0] == "/out/sample5/plan/job.yaml"
    assert "--results-dir" in args and "/out/sample5/results" in args
    assert "--reports-dir" in args and "/out/sample5/reports" in args
    assert "--models-dir" not in args


def test_ingest_projection_includes_model_when_analysis_ok():
    args = project_out("ingest", _state())
    assert "-o" in args and "/out/sample5/assembled" in args
    assert "--reduced" in args and "/out/sample5/partial.txt" in args
    assert "--nexus-file" in args and "/nexus/REF_L_226644.nxs.h5" in args
    assert "--model" in args and "/out/sample5/results/problem.json" in args


def test_ingest_projection_drops_model_when_analysis_not_ok():
    s = _state()
    s["stages"]["analysis"]["status"] = "skipped"
    args = project_out("ingest", s)
    assert "--model" not in args


def test_convert_projection():
    args = project_out("convert", _state())
    assert args[0] == "/out/sample5/assembled"  # positional ingest dir
    assert "--raw" in args  # nr-isaac-format uses --raw, not --nexus-file


def test_missing_required_raises():
    s = _state()
    del s["inputs"]["derived"]["nexus_file"]
    with pytest.raises(ProjectionError):
        project_out("reduction", s)


def test_unknown_stage_raises():
    with pytest.raises(ProjectionError):
        project_out("nope", _state())


def test_optional_missing_is_omitted():
    s = _state()
    del s["inputs"]["operator"]["llm"]
    del s["inputs"]["operator"]["sequence_total"]
    args = project_out("plan", s)
    assert "--llm-model" not in args
    assert "--sequence-total" not in args
    # required positionals + --output-dir still present
    assert args[:2] == ["/out/sample5/partial.txt", "/ctx/context.md"]


def test_project_out_shell_quotes():
    s = _state()
    s["inputs"]["operator"]["output_directory"] = "/out/with space"
    shell = project_out_shell("reduction", s)
    assert "'/out/with space'" in shell
