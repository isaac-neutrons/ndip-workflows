"""Verify the inlined ``canonicalize_paths`` helper in simple_analyzer.xml.

plan-data / analyze-sample realpath their output directory, so on SNS
hosts the state JSON they write embeds ``/gpfs/neutronsfs/instruments/...``
paths even when the operator supplied ``/SNS/...``. The post-processor
in simple_analyzer.xml maps the resolved prefix back to the canonical
one. These tests guard that behavior.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_XMLS = [
    ROOT / "tools" / "simple_analyzer.xml",
    ROOT / "tools" / "data_assembler.xml",
]

_CONFIGFILE_RE = re.compile(
    r'<configfile name="canonicalize_paths">#raw\n(.*?)\n#end raw</configfile>',
    re.DOTALL,
)


def _extract(xml_path: pathlib.Path) -> str:
    m = _CONFIGFILE_RE.search(xml_path.read_text())
    assert m is not None, f"no canonicalize_paths configfile in {xml_path}"
    return m.group(1)


def test_all_tool_xmls_share_same_canonicalize_paths_body():
    bodies = {p.name: _extract(p) for p in TOOL_XMLS}
    first_name, first_body = next(iter(bodies.items()))
    for name, body in bodies.items():
        assert body == first_body, (
            f"{name} canonicalize_paths diverged from {first_name}. Keep them in sync."
        )


def _run(tmp_path, config: dict | None, canonical: str):
    helper = tmp_path / "canonicalize_paths.py"
    helper.write_text(_extract(TOOL_XMLS[0]))
    config_path = tmp_path / "state.json"
    if config is not None:
        config_path.write_text(json.dumps(config))
    rc = subprocess.run(
        [sys.executable, str(helper), str(config_path), canonical],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, rc.stderr
    if config_path.exists():
        return json.loads(config_path.read_text())
    return None


def test_no_symlink_leaves_json_unchanged(tmp_path):
    """When OUTPUT_DIR has no symlink ancestor, JSON passes through unchanged."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    config = {
        "paths": {"output_directory": str(output_dir)},
        "analysis": {"metadata": {"results_dir": str(output_dir / "results")}},
    }
    result = _run(tmp_path, config, str(output_dir))
    assert result == config


def test_missing_config_file_is_a_noop(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    # No config written — helper should exit cleanly.
    result = _run(tmp_path, config=None, canonical=str(output_dir))
    assert result is None


def test_empty_canonical_arg_is_a_noop(tmp_path):
    config = {"paths": {"output_directory": "/whatever"}}
    result = _run(tmp_path, config, canonical="")
    assert result == config


def test_rewrites_resolved_prefix_back_to_canonical(tmp_path):
    """Simulates /SNS/REF_L -> /gpfs/neutronsfs/instruments/REF_L via a symlink."""
    target = tmp_path / "gpfs" / "neutronsfs" / "instruments" / "REF_L"
    target.mkdir(parents=True)
    sns = tmp_path / "SNS"
    sns.mkdir()
    (sns / "REF_L").symlink_to(target, target_is_directory=True)

    canonical_output = sns / "REF_L" / "IPTS-36897" / "shared" / "isaac" / "sample5"
    canonical_output.mkdir(parents=True)
    resolved_output = str(canonical_output.resolve())
    assert resolved_output != str(canonical_output), "symlink fixture failed to set up"

    config = {
        "paths": {"output_directory": str(canonical_output)},
        "reduction": {"partial_file": str(canonical_output / "partial.txt")},
        "analysis": {
            "metadata": {
                "results_dir": f"{resolved_output}/results",
                "reports_dir": f"{resolved_output}/reports",
                "job_yaml": f"{resolved_output}/plan/job.yaml",
            },
            "problem_json": f"{resolved_output}/results/problem.json",
        },
        # Unrelated path that should not be rewritten.
        "unrelated": "/var/log/foo.log",
    }
    result = _run(tmp_path, config, str(canonical_output))

    expected_prefix = str(canonical_output)
    assert result["analysis"]["metadata"]["results_dir"] == f"{expected_prefix}/results"
    assert result["analysis"]["metadata"]["reports_dir"] == f"{expected_prefix}/reports"
    assert result["analysis"]["metadata"]["job_yaml"] == f"{expected_prefix}/plan/job.yaml"
    assert result["analysis"]["problem_json"] == f"{expected_prefix}/results/problem.json"
    # /SNS path that was already canonical stays put.
    assert result["paths"]["output_directory"] == str(canonical_output)
    # Strings that don't share the resolved prefix are untouched.
    assert result["unrelated"] == "/var/log/foo.log"


def test_rewrites_resolved_paths_above_output_dir(tmp_path):
    """References to siblings of OUTPUT_DIR (still under the symlink) are remapped too."""
    target = tmp_path / "gpfs" / "neutronsfs" / "instruments" / "REF_L"
    target.mkdir(parents=True)
    sns = tmp_path / "SNS"
    sns.mkdir()
    (sns / "REF_L").symlink_to(target, target_is_directory=True)

    canonical_output = sns / "REF_L" / "IPTS-36897" / "shared" / "isaac" / "sample5"
    canonical_output.mkdir(parents=True)

    # A reference to the sibling 'nexus' dir under the same IPTS.
    resolved_root = str((sns / "REF_L").resolve())
    sibling_resolved = f"{resolved_root}/IPTS-36897/nexus/REF_L_226644.nxs.h5"

    config = {"paths": {"event_file": sibling_resolved}}
    result = _run(tmp_path, config, str(canonical_output))

    assert result["paths"]["event_file"] == str(
        sns / "REF_L" / "IPTS-36897" / "nexus" / "REF_L_226644.nxs.h5"
    )
