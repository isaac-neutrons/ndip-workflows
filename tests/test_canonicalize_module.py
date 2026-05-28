"""Tests for ``ndip_state.canonicalize`` — the in-process path canonicalizer.

This is the module that replaces the inlined ``canonicalize_paths`` configfile
duplicated across the tool XMLs. The symlink fixtures mirror
``tests/test_canonicalize_paths.py`` so we know the lifted logic behaves
identically, but here we import and call it directly (no subprocess / no XML
extraction).
"""

from __future__ import annotations

import json

from ndip_state.canonicalize import canonicalize_file, canonicalize_paths


def _symlinked_output(tmp_path):
    """Set up /SNS/REF_L -> /gpfs/.../REF_L and return (canonical_output, resolved)."""
    target = tmp_path / "gpfs" / "neutronsfs" / "instruments" / "REF_L"
    target.mkdir(parents=True)
    sns = tmp_path / "SNS"
    sns.mkdir()
    (sns / "REF_L").symlink_to(target, target_is_directory=True)

    canonical_output = sns / "REF_L" / "IPTS-36897" / "shared" / "isaac" / "sample5"
    canonical_output.mkdir(parents=True)
    resolved_output = str(canonical_output.resolve())
    assert resolved_output != str(canonical_output), "symlink fixture failed to set up"
    return canonical_output, resolved_output


def test_no_symlink_leaves_object_unchanged(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    obj = {
        "inputs": {"operator": {"output_directory": str(output_dir)}},
        "stages": {"analysis": {"artifacts": {"results_dir": str(output_dir / "results")}}},
    }
    assert canonicalize_paths(obj, str(output_dir)) == obj


def test_empty_canonical_is_a_noop():
    obj = {"inputs": {"operator": {"output_directory": "/whatever"}}}
    assert canonicalize_paths(obj, "") == obj


def test_rewrites_resolved_prefix_back_to_canonical(tmp_path):
    canonical_output, resolved_output = _symlinked_output(tmp_path)
    obj = {
        "stages": {
            "analysis": {
                "artifacts": {
                    "results_dir": f"{resolved_output}/results",
                    "reports_dir": f"{resolved_output}/reports",
                    "problem_json": f"{resolved_output}/results/problem.json",
                },
                "params": {"job_yaml": f"{resolved_output}/plan/job.yaml"},
            }
        },
        # Already-canonical path stays put.
        "inputs": {"operator": {"output_directory": str(canonical_output)}},
        # Unrelated path is untouched.
        "unrelated": "/var/log/foo.log",
    }
    result = canonicalize_paths(obj, str(canonical_output))

    expected = str(canonical_output)
    art = result["stages"]["analysis"]["artifacts"]
    assert art["results_dir"] == f"{expected}/results"
    assert art["reports_dir"] == f"{expected}/reports"
    assert art["problem_json"] == f"{expected}/results/problem.json"
    assert result["stages"]["analysis"]["params"]["job_yaml"] == f"{expected}/plan/job.yaml"
    assert result["inputs"]["operator"]["output_directory"] == str(canonical_output)
    assert result["unrelated"] == "/var/log/foo.log"


def test_rewrites_siblings_above_output_dir(tmp_path):
    canonical_output, _ = _symlinked_output(tmp_path)
    resolved_root = str((tmp_path / "SNS" / "REF_L").resolve())
    sibling_resolved = f"{resolved_root}/IPTS-36897/nexus/REF_L_226644.nxs.h5"

    obj = {"inputs": {"derived": {"nexus_file": sibling_resolved}}}
    result = canonicalize_paths(obj, str(canonical_output))

    assert result["inputs"]["derived"]["nexus_file"] == str(
        tmp_path / "SNS" / "REF_L" / "IPTS-36897" / "nexus" / "REF_L_226644.nxs.h5"
    )


def test_canonicalize_file_in_place(tmp_path):
    canonical_output, resolved_output = _symlinked_output(tmp_path)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"a": f"{resolved_output}/results/problem.json"}))

    canonicalize_file(str(state_path), str(canonical_output))

    data = json.loads(state_path.read_text())
    assert data["a"] == f"{canonical_output}/results/problem.json"


def test_canonicalize_file_missing_path_is_noop(tmp_path):
    # Should not raise.
    canonicalize_file(str(tmp_path / "nope.json"), str(tmp_path))
