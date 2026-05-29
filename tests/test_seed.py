"""Tests for the seed-config CLI.

Run identity (run, instrument, ipts) is read from the NeXus file *contents*
with h5py, not from the filename — Galaxy stages uploads as
``dataset_<uuid>.dat``. The canonical paths are then reconstructed under
``--facility-root``.
"""

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from yaml_parser.seed import main

h5py = pytest.importorskip("h5py")


def _write_nexus(path: Path, *, run: int = 226644, instrument: str = "REF_L",
                 ipts: str = "IPTS-36897", with_ipts: bool = True) -> None:
    """Write a minimal NeXus file with the fields seed-config reads."""
    with h5py.File(path, "w") as f:
        entry = f.create_group("entry")
        entry.attrs["NX_class"] = b"NXentry"
        entry["run_number"] = [str(run).encode()]
        entry["entry_identifier"] = [str(run).encode()]
        if with_ipts:
            entry["experiment_identifier"] = [ipts.encode()]
        inst = entry.create_group("instrument")
        inst.attrs["NX_class"] = b"NXinstrument"
        name = inst.create_dataset("name", data=[instrument.encode()])
        name.attrs["short_name"] = instrument.encode()


def _make_run_layout(tmp_path: Path, run: int = 226644, instrument: str = "REF_L",
                     ipts: str = "IPTS-36897", with_ipts: bool = True) -> dict:
    """Build a facility tree + an opaquely-named (Galaxy-style) event file.

    Layout::

        tmp_path/SNS/<INST>/<IPTS>/nexus/            (reconstructed nexus dir)
        tmp_path/SNS/<INST>/<IPTS>/shared/autoreduce/template_down.xml
        tmp_path/SNS/<INST>/<IPTS>/shared/isaac/context.md
        tmp_path/uploads/dataset_<uuid>.dat          (the real HDF5 we pass in)
    """
    facility_root = tmp_path / "SNS"
    ipts_root = facility_root / instrument / ipts
    nexus_dir = ipts_root / "nexus"
    shared = ipts_root / "shared"
    nexus_dir.mkdir(parents=True)
    (shared / "autoreduce").mkdir(parents=True)
    (shared / "isaac").mkdir(parents=True)

    template = shared / "autoreduce" / "template_down.xml"
    template.write_text("<template/>")
    context = shared / "isaac" / "context.md"
    context.write_text("# context")

    # Galaxy-style opaque name, deliberately NOT matching <INST>_<RUN>.nxs.h5.
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    event = uploads / "dataset_ea91e004-5838-4cb0-9152-0ad4684b0c1b.dat"
    _write_nexus(event, run=run, instrument=instrument, ipts=ipts, with_ipts=with_ipts)

    return {
        "event": event,
        "facility_root": facility_root,
        "template": template,
        "context": context,
        "ipts_root": ipts_root,
        "shared": shared,
        "nexus_dir": nexus_dir,
        "instrument": instrument,
        "ipts": ipts,
        "run": run,
        # the canonical nexus path the tool reconstructs (need not exist)
        "nexus_file": nexus_dir / f"{instrument}_{run}.nxs.h5",
    }


def _write_seed(path: Path, data: dict, fmt: str = "json") -> None:
    if fmt == "json":
        path.write_text(json.dumps(data, indent=2))
    else:
        path.write_text(yaml.safe_dump(data))


def _invoke(layout: dict, seed_path: Path, out_path: Path):
    runner = CliRunner()
    return runner.invoke(main, [
        str(layout["event"]), str(seed_path),
        "-o", str(out_path),
        "--facility-root", str(layout["facility_root"]),
    ])


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
    result = _invoke(layout, seed_path, out_path)
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
    # nexus_file is RECONSTRUCTED from content, not the opaque uploaded filename
    assert der["nexus_file"] == str(layout["nexus_file"])
    assert der["nexus_file"] != str(layout["event"])
    assert der["data_directory"] == str(layout["nexus_dir"])
    assert der["ipts_shared_root"] == str(layout["shared"])

    # Stage blocks initialised pending.
    for stage in ("reduction", "analysis", "assembly"):
        assert state["stages"][stage] == {
            "status": "pending", "params": {}, "artifacts": {}, "info": {},
        }


def test_filename_is_ignored(tmp_path):
    """The opaque dataset_<uuid>.dat name must not break extraction."""
    layout = _make_run_layout(tmp_path, run=999111, instrument="REF_L")
    assert "dataset_" in layout["event"].name  # sanity: not a REF_L_*.nxs.h5 name
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    result = _invoke(layout, seed_path, out_path)
    assert result.exit_code == 0, result.output
    state = json.loads(out_path.read_text())
    assert state["workflow"]["run"] == 999111
    assert state["inputs"]["derived"]["nexus_file"].endswith("/nexus/REF_L_999111.nxs.h5")


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
    result = _invoke(layout, seed_path, out_path)
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
    result = _invoke(layout, seed_path, out_path)
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
    result = _invoke(layout, seed_path, out_path)
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
    result = _invoke(layout, seed_path, out_path)
    assert result.exit_code != 0
    assert "context_file" in result.output


def test_multiple_missing_keys_listed(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed = {"sequence_total": 3}
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    result = _invoke(layout, seed_path, out_path)
    assert result.exit_code != 0
    for key in ("template_file", "output_directory", "context_file"):
        assert key in result.output


def test_non_hdf5_event_file_errors(tmp_path):
    """A non-HDF5 event file (e.g. a truncated/garbage upload) fails clearly."""
    layout = _make_run_layout(tmp_path)
    bad = tmp_path / "uploads" / "dataset_garbage.dat"
    bad.write_bytes(b"not an hdf5 file")
    layout["event"] = bad
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    result = _invoke(layout, seed_path, out_path)
    assert result.exit_code != 0
    assert "NeXus" in result.output or "HDF5" in result.output


def test_missing_experiment_identifier_errors(tmp_path):
    """Without /entry/experiment_identifier we can't reconstruct the IPTS path."""
    layout = _make_run_layout(tmp_path, with_ipts=False)
    seed = {
        "template_file": "autoreduce/template_down.xml",
        "output_directory": "isaac/out",
        "context_file": "isaac/context.md",
        "sequence_total": 3,
    }
    seed_path = tmp_path / "seed.json"
    _write_seed(seed_path, seed)
    out_path = tmp_path / "out.json"
    result = _invoke(layout, seed_path, out_path)
    assert result.exit_code != 0
    assert "experiment_identifier" in result.output or "IPTS" in result.output


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
    result = _invoke(layout, seed_path, out_path)
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
    result = _invoke(layout, seed_path, out_path)
    assert result.exit_code != 0
    assert "context_file" in result.output


def test_invalid_seed_file_errors(tmp_path):
    layout = _make_run_layout(tmp_path)
    seed_path = tmp_path / "seed.json"
    seed_path.write_text("[not, a, dict]")
    out_path = tmp_path / "out.json"
    result = _invoke(layout, seed_path, out_path)
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
    result = _invoke(layout, seed_path, out_path)
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
    result = _invoke(layout, seed_path, out_path)
    assert result.exit_code == 0, result.output
    state = json.loads(out_path.read_text())
    assert state["workflow"]["run"] == 12345
    assert isinstance(state["workflow"]["run"], int)
