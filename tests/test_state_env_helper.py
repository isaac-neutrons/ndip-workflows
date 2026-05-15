"""Verify the inlined ``state_env`` configfile in each tool XML.

Step 9 of the state-handling refactor replaced the 296-line ``state_module``
configfile with a slim ~40-line ``state_env`` helper that only emits shell
exports the bash actually uses. This test guards two invariants:

  1. All three tool XMLs ship the same helper body.
  2. The helper actually emits the expected ``export KEY=value`` lines for
     both v1 (nested) and v0 (flat) state inputs.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_XMLS = [
    ROOT / "tools" / "reduction.xml",
    ROOT / "tools" / "simple_analyzer.xml",
    ROOT / "tools" / "data_assembler.xml",
]

_CONFIGFILE_RE = re.compile(
    r'<configfile name="state_env">#raw\n(.*?)\n#end raw</configfile>',
    re.DOTALL,
)


def _extract(xml_path: pathlib.Path) -> str:
    m = _CONFIGFILE_RE.search(xml_path.read_text())
    assert m is not None, f"no state_env configfile in {xml_path}"
    return m.group(1)


def test_all_tool_xmls_share_same_state_env_body():
    bodies = {p.name: _extract(p) for p in TOOL_XMLS}
    first_name, first_body = next(iter(bodies.items()))
    for name, body in bodies.items():
        assert body == first_body, (
            f"{name} state_env diverged from {first_name}. Keep all three in sync."
        )


def _run_helper(tmp_path, state):
    """Write the helper to disk, run it, return the resulting env file text."""
    helper = tmp_path / "state_env.py"
    helper.write_text(_extract(TOOL_XMLS[0]))
    state_path = tmp_path / "in.json"
    env_path = tmp_path / "_env.sh"
    if state is not None:
        state_path.write_text(json.dumps(state))
    rc = subprocess.run(
        [sys.executable, str(helper), str(state_path) if state is not None else "", str(env_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, rc.stderr
    return env_path.read_text()


def test_helper_emits_expected_vars_for_v1_state(tmp_path):
    state = {
        "schema_version": "1",
        "run": 226644,
        "sequence_total": 3,
        "paths": {
            "data_directory": "/SNS/REF_L/IPTS-36897/nexus",
            "output_directory": "/SNS/REF_L/IPTS-36897/shared/isaac/sample5",
            "template_file": "/SNS/template.xml",
            "context_file": "/SNS/context.md",
            "event_file": "/SNS/REF_L_226644.nxs.h5",
        },
        "llm": {"provider": "local", "model": "gpt-4", "base_url": "https://x/v1/"},
        "reduction": {"partial_file": "/SNS/partial.txt", "metadata": {}},
        "analysis": {"metadata": {}},
        "assembly": {"metadata": {}},
        "errors": [],
    }
    text = _run_helper(tmp_path, state)
    assert "export OUTPUT_DIR=/SNS/REF_L/IPTS-36897/shared/isaac/sample5" in text
    assert "export EVENT_FILE=/SNS/REF_L_226644.nxs.h5" in text
    assert "export TEMPLATE=/SNS/template.xml" in text
    assert "export DATA_DIR=/SNS/REF_L/IPTS-36897/nexus" in text
    assert "export CONTEXT_FILE=/SNS/context.md" in text
    assert "export REFLECTIVITY_FILE=/SNS/partial.txt" in text
    assert "export SEQUENCE_TOTAL=3" in text
    assert "export LLM_PROVIDER=local" in text
    assert "export LLM_MODEL=gpt-4" in text
    assert "export LLM_BASE_URL=https://x/v1/" in text


def test_helper_accepts_v0_flat_state(tmp_path):
    """Flat v0 keys still resolve via the (paths.* or top-level) fallback."""
    state = {
        "event_file": "/legacy.h5",
        "partial_file": "/legacy/p.txt",
        "llm_model": "gpt-3",
        "output_directory": "/legacy/out",
    }
    text = _run_helper(tmp_path, state)
    assert "export EVENT_FILE=/legacy.h5" in text
    assert "export REFLECTIVITY_FILE=/legacy/p.txt" in text
    assert "export LLM_MODEL=gpt-3" in text
    assert "export OUTPUT_DIR=/legacy/out" in text


def test_helper_handles_missing_state_path(tmp_path):
    """An empty / nonexistent input path is OK — emits empty exports."""
    text = _run_helper(tmp_path, state=None)
    assert "export OUTPUT_DIR=''" in text
    assert "export LLM_PROVIDER=''" in text


def test_helper_quotes_shell_metachars(tmp_path):
    state = {"paths": {"output_directory": "/safe; rm -rf /"}}
    text = _run_helper(tmp_path, state)
    # shlex.quote wraps the string so the metachars can't break out
    assert "'/safe; rm -rf /'" in text


def test_helper_derives_data_dir_from_event_file(tmp_path):
    """When paths.data_directory is absent, DATA_DIR falls back to dirname(event_file)."""
    state = {"paths": {"event_file": "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5"}}
    text = _run_helper(tmp_path, state)
    assert "export DATA_DIR=/SNS/REF_L/IPTS-36897/nexus" in text
