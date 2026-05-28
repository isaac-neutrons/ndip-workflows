"""No-Galaxy end-to-end gate for the decoupled chain.

Each pipeline tool is replaced with a tiny stub that reads the args ndip-run
projected for it and writes a canned ``ndip-tool-result/1`` manifest to
``--result-out``. Driving all five stages through ``run_stage`` then proves:

  - project-out hands each tool the right args from state,
  - merge-in folds each neutral manifest into the correct v2 stage record,
  - provenance (e.g. the reduction params) survives to the final document.

The stubs assert on the args they receive, so this also locks the
project-out contract end to end.
"""

from __future__ import annotations

import json
import os
import stat
import textwrap

from ndip_state.run import run_stage
from ndip_state.state import empty_state, overall_status, save_state


def _make_stub(path, body):
    script = "#!/usr/bin/env python3\n" + textwrap.dedent(body)
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# Each stub: parse its own args, write the manifest passed via --result-out.
_REDUCTION = '''
    import json, sys
    a = sys.argv[1:]
    assert "--event-file" in a and "--template" in a and "--output-dir" in a, a
    out = a[a.index("--result-out") + 1]
    json.dump({
        "tool": "simple-reduction", "status": "ok",
        "params": {"q_min": 0.005, "q_max": 0.2, "q_step": -0.02},
        "artifacts": {"partial_file": "/out/sample5/partial.txt",
                      "combined_file": "/out/sample5/combined.txt"},
        "info": {"first_run_of_set": 226642},
    }, open(out, "w"))
'''

_PLAN = '''
    import json, sys
    a = sys.argv[1:]
    # positionals: partial_file, context_file
    assert a[0].endswith("partial.txt"), a
    assert a[1].endswith("context.md"), a
    out = a[a.index("--result-out") + 1]
    json.dump({
        "tool": "plan-data", "status": "ok",
        "params": {"model_name": "Cu-D2O", "perform_assembly": True},
        "artifacts": {"job_yaml": "/out/sample5/plan/job.yaml"},
        "info": {"sequence_id": "Cu-D2O", "sequence_number": 3},
    }, open(out, "w"))
'''

_ANALYZE = '''
    import json, sys
    a = sys.argv[1:]
    assert a[0].endswith("job.yaml"), a  # positional CONFIG (job yaml)
    assert "--results-dir" in a, a
    out = a[a.index("--result-out") + 1]
    json.dump({
        "tool": "analyze-sample", "status": "ok",
        "artifacts": {"problem_json": "/out/sample5/results/problem.json",
                      "results_dir": "/out/sample5/results"},
        "info": {"pipeline_status": "ok", "completed_stages": ["partial", "fit"]},
    }, open(out, "w"))
'''

_INGEST = '''
    import json, sys
    a = sys.argv[1:]
    assert "-o" in a and "--nexus-file" in a, a
    out = a[a.index("--result-out") + 1]
    json.dump({
        "tool": "data-assembler", "status": "ok",
        "artifacts": {"ingest_dir": "/out/sample5/assembled",
                      "parquet_files": {"reflectivity": "/out/sample5/assembled/r.parquet"}},
        "info": {"ingest_status": "completed"},
    }, open(out, "w"))
'''

_CONVERT = '''
    import json, sys
    a = sys.argv[1:]
    assert a[0].endswith("assembled"), a  # positional ingest dir
    out = a[a.index("--result-out") + 1]
    json.dump({
        "tool": "nr-isaac-format", "status": "ok",
        "artifacts": {"isaac_record": "/out/sample5/assembled/isaac_record_226644.json"},
        "info": {"isaac_status": "converted"},
    }, open(out, "w"))
'''


def _seed_state(tmp_path):
    s = empty_state()
    s["workflow"] = {"run": 226644, "instrument": "REF_L", "ipts": "IPTS-36897"}
    template = tmp_path / "template_down.xml"
    template.write_text("<reduction/>")
    s["inputs"]["operator"] = {
        "sequence_total": 3,
        "output_directory": "/out/sample5",
        "template_file": str(template),
        "context_file": "/ctx/context.md",
        "llm": {"provider": "local", "model": "gpt-4", "base_url": "https://x/v1/"},
    }
    s["inputs"]["derived"] = {"nexus_file": "/nexus/REF_L_226644.nxs.h5"}
    return s


def test_full_chain_through_ndip_run(tmp_path):
    py = os.environ.get("PYTHON", "python3")
    stubs = {
        "reduction": _make_stub(tmp_path / "red.py", _REDUCTION),
        "plan": _make_stub(tmp_path / "plan.py", _PLAN),
        "analyze": _make_stub(tmp_path / "ana.py", _ANALYZE),
        "ingest": _make_stub(tmp_path / "ing.py", _INGEST),
        "convert": _make_stub(tmp_path / "con.py", _CONVERT),
    }

    state_path = str(tmp_path / "state.json")
    save_state(_seed_state(tmp_path), state_path)

    for stage in ("reduction", "plan", "analyze", "ingest", "convert"):
        rc = run_stage(stage, state_path, "%s %s" % (py, stubs[stage]))
        assert rc == 0, "stage %s exited %d" % (stage, rc)

    with open(state_path) as f:
        s = json.load(f)

    assert overall_status(s) == "ok"

    # Reduction provenance survived all the way to the final document.
    assert s["stages"]["reduction"]["params"]["q_step"] == -0.02
    assert s["stages"]["reduction"]["params"]["template_file"].endswith("template_down.xml")
    assert len(s["stages"]["reduction"]["params"]["template_sha256"]) == 64

    # Analysis merged from both plan + analyze.
    assert s["stages"]["analysis"]["params"]["model_name"] == "Cu-D2O"
    assert s["stages"]["analysis"]["artifacts"]["problem_json"].endswith("problem.json")
    assert s["stages"]["analysis"]["info"]["pipeline_status"] == "ok"

    # Assembly carries derived input provenance + final isaac record.
    asm = s["stages"]["assembly"]
    assert asm["params"]["nexus_input"] == "/nexus/REF_L_226644.nxs.h5"
    assert asm["params"]["reduced_input"] == "/out/sample5/partial.txt"
    assert asm["params"]["model_input"] == "/out/sample5/results/problem.json"
    assert asm["artifacts"]["isaac_record"].endswith("isaac_record_226644.json")
    assert asm["info"]["isaac_status"] == "converted"
    assert s["errors"] == []


def test_chain_records_failure_when_tool_errors(tmp_path):
    py = os.environ.get("PYTHON", "python3")
    # A reduction stub that exits non-zero and writes no manifest.
    bad = _make_stub(tmp_path / "bad.py", '''
        import sys
        sys.exit(7)
    ''')
    state_path = str(tmp_path / "state.json")
    save_state(_seed_state(tmp_path), state_path)

    rc = run_stage("reduction", state_path, "%s %s" % (py, bad))
    assert rc == 7

    with open(state_path) as f:
        s = json.load(f)
    assert s["stages"]["reduction"]["status"] == "failed"
    assert s["errors"][0]["stage"] == "reduction"
    assert s["errors"][0]["exit_code"] == 7
