"""Guards for the inlined orchestration shim used by the Galaxy tool XMLs.

``tools/ndip_shim.py`` is a self-contained bundle of the canonical
``ndip_state`` logic, inlined into each ``tools/*.xml`` via the generator
``tools/build_tool_xmls.py``. Two things must hold:

  1. **Parity** — the shim must behave like ``ndip_state`` for the operations
     the tools rely on (project-out, merge-in, status mapping).
  2. **Freshness** — the committed ``tools/*.xml`` must match what the generator
     produces from the current templates + shim.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIM_PATH = ROOT / "tools" / "ndip_shim.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


shim = _load(SHIM_PATH, "_ndip_shim_under_test")


def _state():
    """A fully-populated state covering every projection's inputs."""
    return {
        "schema_version": "2",
        "workflow": {"run": 226644, "instrument": "REF_L", "ipts": "IPTS-36897"},
        "inputs": {
            "operator": {
                "output_directory": "/out/sample5",
                "template_file": "/tmpl/t.xml",
                "context_file": "/ctx/context.md",
                "sequence_total": 3,
                "llm": {"provider": "local", "model": "gpt-4", "base_url": "https://x/v1/"},
            },
            "derived": {
                "nexus_file": "/nexus/REF_L_226644.nxs.h5",
                "data_directory": "/nexus",
            },
        },
        "stages": {
            "reduction": {"status": "ok", "params": {}, "info": {},
                          "artifacts": {"partial_file": "/out/sample5/p.txt",
                                        "combined_file": "/out/sample5/c.txt"}},
            "analysis": {"status": "ok", "params": {}, "info": {},
                         "artifacts": {"job_yaml": "/out/sample5/plan/job.yaml",
                                       "problem_json": "/out/sample5/results/problem.json"}},
            "assembly": {"status": "pending", "params": {}, "info": {},
                         "artifacts": {"ingest_dir": "/out/sample5/assembled"}},
        },
        "errors": [],
    }


# --- parity with ndip_state -------------------------------------------------

def test_projection_parity():
    from ndip_state.projection import project_out as ref_project
    state = _state()
    for stage in ("reduction", "plan", "analyze", "ingest", "convert"):
        assert shim.project_out(stage, state) == ref_project(stage, state), stage


def test_merge_in_parity(tmp_path):
    from ndip_state.adapters import merge_in as ref_merge

    template = tmp_path / "t.xml"
    template.write_text("<reduction/>")
    manifest = {
        "status": "ok",
        "params": {"q_step": -0.02},
        "artifacts": {"partial_file": "/p.txt", "combined_file": "/c.txt"},
        "info": {"first_run_of_set": 226642},
    }

    def _seeded():
        s = shim.empty_state()
        s["inputs"]["operator"]["template_file"] = str(template)
        return s

    assert shim.merge_in("reduction", _seeded(), dict(manifest), 0) == \
        ref_merge("reduction", _seeded(), dict(manifest), 0)


def test_status_mapping_parity():
    from ndip_state.state import _manifest_to_stage_status as ref
    for status in ("ok", "dry-run", "skipped", "failed", "needs-reprocessing", None):
        for code in (0, 1):
            assert shim._manifest_to_stage_status({"status": status}, code) == ref({"status": status}, code)


def test_empty_state_parity():
    from ndip_state.state import empty_state as ref
    assert shim.empty_state() == ref()


# --- CLI smoke --------------------------------------------------------------

def test_cli_project_out_and_merge_in(tmp_path):
    state_in = tmp_path / "state.json"
    state_in.write_text(json.dumps(_state()))

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        shim.main(["project-out", "reduction", str(state_in)])
    args = buf.getvalue()
    assert "--event-file" in args and "/nexus/REF_L_226644.nxs.h5" in args

    result = tmp_path / "result.json"
    result.write_text(json.dumps({
        "status": "ok",
        "params": {"model_name": "Cu-D2O", "perform_assembly": True},
        "artifacts": {"job_yaml": "/out/sample5/plan/job.yaml"},
    }))
    out = tmp_path / "out.json"
    shim.main(["merge-in", "plan", str(state_in), str(result), "0", str(out)])
    s = json.loads(out.read_text())
    assert s["stages"]["analysis"]["status"] == "ok"
    assert s["stages"]["analysis"]["params"]["model_name"] == "Cu-D2O"


def test_cli_set_llm_fills_blanks(tmp_path):
    s = _state()
    s["inputs"]["operator"]["llm"] = {}
    state_in = tmp_path / "state.json"
    state_in.write_text(json.dumps(s))
    out = tmp_path / "out.json"
    shim.main(["set-llm", str(state_in), "local", "gpt-4", "https://y/v1/", str(out)])
    loaded = json.loads(out.read_text())
    assert loaded["inputs"]["operator"]["llm"] == {
        "provider": "local", "model": "gpt-4", "base_url": "https://y/v1/",
    }


def test_cli_set_llm_does_not_override(tmp_path):
    state_in = tmp_path / "state.json"
    state_in.write_text(json.dumps(_state()))  # already has llm.model gpt-4
    out = tmp_path / "out.json"
    shim.main(["set-llm", str(state_in), "other", "other-model", "https://z/v1/", str(out)])
    loaded = json.loads(out.read_text())
    assert loaded["inputs"]["operator"]["llm"]["model"] == "gpt-4"


def test_cli_get(tmp_path):
    state_in = tmp_path / "state.json"
    state_in.write_text(json.dumps(_state()))

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        shim.main(["get", str(state_in), "inputs.operator.output_directory"])
    assert buf.getvalue() == "/out/sample5"


# --- generated XMLs are fresh ----------------------------------------------

def test_no_inline_cheetah_if_directives():
    """Cheetah's ``#if`` consumes the rest of the line as its condition, so an
    inline ``#if X foo #end if`` is parsed as an unterminated block. Every
    ``#if`` must live on its own directive line, paired with a ``#end if`` line.
    This is what planemo's Cheetah parser checks on lint."""
    import re
    for xml_path in sorted((ROOT / "tools").glob("*.xml")):
        text = xml_path.read_text()
        m = re.search(r"<command[^>]*><!\[CDATA\[(.*?)\]\]></command>", text, re.S)
        if not m:
            continue
        for i, ln in enumerate(m.group(1).split("\n"), 1):
            s = ln.strip()
            if s.startswith("#if ") and "#end" in s[3:]:
                raise AssertionError(
                    f"{xml_path.name}:{i}: inline '#if … #end if' — Cheetah "
                    f"will not parse this. Use block form on separate lines."
                )


def test_generated_xmls_are_well_formed_xml():
    """Galaxy lint parses the XML; any stray '<' or '&' in the inlined shim
    breaks element nesting. ``#raw``/``#end raw`` is a Cheetah directive — it
    does NOT escape XML — so the shim must contain no XML-hostile literals."""
    import xml.etree.ElementTree as ET
    for xml_path in sorted((ROOT / "tools").glob("*.xml")):
        # Will raise ParseError if any tag is unbalanced or any '<' appears
        # outside markup.
        ET.parse(xml_path)


def test_generated_xmls_are_up_to_date():
    gen = _load(ROOT / "tools" / "build_tool_xmls.py", "_ndip_build_tool_xmls")
    shim_text = (ROOT / "tools" / "ndip_shim.py").read_text().rstrip("\n")
    for template in sorted((ROOT / "tools").glob("*.xml.in")):
        committed = template.with_suffix("").read_text()
        expected = gen.render(template.read_text(), shim_text)
        assert committed == expected, (
            f"{template.with_suffix('').name} is stale — run "
            f"`python tools/build_tool_xmls.py` after editing the template or shim."
        )
