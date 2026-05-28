"""Tests for the seed-config CLI."""

import json
import os
import tempfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from yaml_parser.seed import main


def _make_run_layout(tmp_path: Path, run: int = 226644, instrument: str = "REF_L",
                     ipts: str = "IPTS-36897") -> dict:
    """Create a fake SNS-shaped tree and return the relevant paths.

    Layout::

        tmp_path/SNS/<INST>/<IPTS>/nexus/<INST>_<RUN>.nxs.h5
        tmp_path/SNS/<INST>/<IPTS>/shared/autoreduce/template_down.xml
        tmp_path/SNS/<INST>/<IPTS>/shared/isaac/context.md
    """
    ipts_root = tmp_path / "SNS" / instrument / ipts
    nexus_dir = ipts_root / "nexus"
    shared = ipts_root / "shared"
    nexus_dir.mkdir(parents=True)
    (shared / "autoreduce").mkdir(parents=True)
    (shared / "isaac").mkdir(parents=True)

    event = nexus_dir / f"{instrument}_{run}.nxs.h5"
    event.write_bytes(b"")
    template = shared / "autoreduce" / "template_down.xml"
    template.write_text("<template/>")
    context = shared / "isaac" / "context.md"
    context.write_text("# context")

    return {
        "event": event,
        "template": template,
        "context": context,
        "ipts_root": ipts_root,
        "shared": shared,
        "nexus_dir": nexus_dir,
        "instrument": instrument,
        "ipts": ipts,
        "run": run,
    }


def _write_seed(path: Path, data: dict, fmt: str = "json") -> None:
    if fmt == "json":
        path.write_text(json.dumps(data, indent=2))
    else:
        path.write_text(yaml.safe_dump(data))


def test_happy_path_json_seed(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/reduction/sample5",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
        "prompt": "Cu/Ti/Si in D2O",
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed, "json")

    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code == 0, result.output

    state = json.loads(out_path.read_text())
    assert state["schema_version"] == "2"

    wf = state["workflow"]
    assert wf["run"] == 226644
    assert wf["instrument"] == "REF_L"
    assert wf["ipts"] == "IPTS-36897"

    op = state["inputs"]["operator"]
    assert op["sequence_total"] == 3
    assert op["prompt"] == "Cu/Ti/Si in D2O"
    assert op["template_file"] == str(layout["template"])
    assert op["context_file"] == str(layout["context"])
    # output_directory was relative — should resolve to IPTS shared root
    assert op["output_directory"] == str(layout["shared"] / "isaac/reduction/sample5")
    assert op["llm"] == {
        "provider": "local",
        "model": "gpt-4",
        "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/",
    }

    der = state["inputs"]["derived"]
    assert der["nexus_file"] == str(layout["event"])
    assert der["data_directory"] == str(layout["nexus_dir"])
    assert der["ipts_shared_root"] == str(layout["shared"])

    # Stage blocks initialised pending.
    for stage in ("reduction", "analysis", "assembly"):
        assert state["stages"][stage] == {
            "status": "pending", "params": {}, "artifacts": {}, "info": {},
        }


def test_happy_path_yaml_seed(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/reduction/sample5",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.yaml"
    _write_seed(seed_path, seed, "yaml")

    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code == 0, result.output
    state = json.loads(out_path.read_text())
    assert state["workflow"]["run"] == 226644
    assert state["inputs"]["operator"]["template_file"] == str(layout["template"])


def test_absolute_paths_in_seed_pass_through(tmp_path):
    layout = _make_run_layout(tmp_path)
    # Place template + context outside the IPTS tree
    extra = tmp_path / "outside"
    extra.mkdir()
    abs_template = extra / "tpl.xml"
    abs_template.write_text("<template/>")
    abs_context = extra / "ctx.md"
    abs_context.write_text("# ctx")

    seed = {
        "template_file": str(abs_template),
        "output_directory": str(tmp_path / "outside/out"),
        "context_file": str(abs_context),
        "sequence_total": 5,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)

    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code == 0, result.output
    state = json.loads(out_path.read_text())
    op = state["inputs"]["operator"]
    assert op["template_file"] == str(abs_template)
    assert op["context_file"] == str(abs_context)
    assert op["output_directory"] == str(tmp_path / "outside/out")
    assert op["sequence_total"] == 5


def test_llm_overrides_from_seed(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
        "llm_provider": "openai",
        "llm_model": "gpt-5",
        "llm_base_url": "https://api.openai.com/v1/",
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code == 0, result.output
    state = json.loads(out_path.read_text())
    assert state["inputs"]["operator"]["llm"] == {
        "provider": "openai",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1/",
    }


def test_missing_required_key_errors(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {
        # missing context_file
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code != 0
    assert "context_file" in result.output


def test_multiple_missing_keys_listed(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {"sequence_total": 3}
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code != 0
    for key in ("template_file", "output_directory", "context_file"):
        assert key in result.output


def test_bad_event_file_pattern_errors(tmp_path):
    """An event file basename that doesn't match <INST>_<RUN>.nxs.h5 fails."""
    nexus_dir = tmp_path / "SNS/REF_L/IPTS-36897/nexus"
    nexus_dir.mkdir(parents=True)
    bad = nexus_dir / "not_a_run.h5"  # wrong extension/shape
    bad.write_bytes(b"")
    shared = tmp_path / "SNS/REF_L/IPTS-36897/shared/autoreduce"
    shared.mkdir(parents=True)
    (shared / "template_down.xml").write_text("<template/>")
    ctx = tmp_path / "SNS/REF_L/IPTS-36897/shared/isaac"
    ctx.mkdir(parents=True)
    (ctx / "context.md").write_text("# x")

    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(bad), str(seed_path), "-o", str(out_path)])
    assert result.exit_code != 0
    assert "INSTRUMENT" in result.output or "basename" in result.output.lower()


def test_missing_template_errors(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {
        "template_file": "autoreduce/does_not_exist.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code != 0
    assert "template_file" in result.output


def test_missing_context_errors(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/missing.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code != 0
    assert "context_file" in result.output


def test_no_ipts_segment_uses_fallback_shared_root(tmp_path):
    """When the path lacks IPTS-*, fall back to ../shared next to data_directory."""
    nexus_dir = tmp_path / "unstructured/data"
    shared = tmp_path / "unstructured/shared/autoreduce"
    ctx_dir = tmp_path / "unstructured/shared/isaac"
    nexus_dir.mkdir(parents=True)
    shared.mkdir(parents=True)
    ctx_dir.mkdir(parents=True)
    event = nexus_dir / "REF_L_999.nxs.h5"
    event.write_bytes(b"")
    (shared / "tpl.xml").write_text("<template/>")
    (ctx_dir / "ctx.md").write_text("# ctx")

    seed = {
        "template_file": "autoreduce/tpl.xml",
        "output_directory": "out",
        "context_file": "isaac/ctx.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(event), str(seed_path), "-o", str(out_path)])
    assert result.exit_code == 0, result.output
    state = json.loads(out_path.read_text())
    assert "ipts" not in state["workflow"]  # nothing to attach
    assert state["workflow"]["instrument"] == "REF_L"
    # output_directory resolved to ../shared/out
    assert state["inputs"]["operator"]["output_directory"].endswith("/unstructured/shared/out")


def test_invalid_seed_file_errors(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed_path = tmp_path / "seed.json"
    seed_path.write_text("[not, a, dict]")
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code != 0
    assert "mapping" in result.output.lower() or "object" in result.output.lower()


def test_seed_can_be_yaml_with_yaml_extension(tmp_path):
    """YAML content with a .yaml extension parses through the YAML fallback."""
    layout = _make_run_layout(tmp_path)
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text(
        "template_file: autoreduce/template_down.xml\n"
        "output_directory: isaac/out\n"
        "context_file: isaac/context.md\n"
        "sequence_total: 3\n"
    )
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code == 0, result.output


def test_run_extracted_as_int(tmp_path):
    layout = _make_run_layout(tmp_path, run=12345)
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    result = runner.invoke(main, [str(layout["event"]), str(seed_path), "-o", str(out_path)])
    assert result.exit_code == 0, result.output
    state = json.loads(out_path.read_text())
    assert state["workflow"]["run"] == 12345
    assert isinstance(state["workflow"]["run"], int)
