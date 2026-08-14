"""Tests for ``ndip_state.workbench`` — handing a measurement to nr-workbench.

Two kinds of test here, and the second kind is the reason this file is long.

The ordinary kind checks the parsers and the renderers. The other kind guards
properties of the *generated* ``sample.md`` that nr-workbench enforces from the
outside and that are invisible to anything local: it matches every six-digit
number in that file against the data on disk, so a stray one becomes a permanent
"documented but absent" finding. A full-precision ISO timestamp has a six-digit
microseconds field and a ``%.6g`` float can render as ``0.999774`` — both of
which that scan reads as run numbers. Those are asserted explicitly.
"""

from __future__ import annotations

import json
import math
import os
import re

import pytest
import yaml

from ndip_state.state import empty_state, save_state
from ndip_state.workbench import (
    BOUND_TOLERANCE,
    MARKER,
    WorkbenchError,
    declared_bounds,
    findings,
    main,
    measurement_notes,
    open_source,
    parse_context,
    parse_context as _parse,
    read_header,
    read_problem,
    render_sample_yaml,
    resolve_segments,
    run_workbench,
)


# nr-workbench's own `project/scan.py::RUN_IN_PROSE_RE`, copied verbatim. The
# point of these tests is to agree with what it does, not with what we wish it
# did, so it is deliberately the loose version.
NRW_RUN_RE = re.compile(r"\b(\d{6})\b")


def _w(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


# --------------------------------------------------------------------------
# Fixtures: a reduced file, a plan, a fit, a project
# --------------------------------------------------------------------------

#: 0.45036 degrees, as the reduction actually stores it — in radians.
THETA_RAD = 0.007860240490788184


def _reduced(path, run, seg, subrun, title="Sample1_Cu_air", norm=230530,
             theta=THETA_RAD, dq_label="FWHM", meta=True):
    """A reduced REF_L partial file with a realistic header."""
    lines = [
        "# Experiment IPTS-36897 Run %d" % run,
        "# Run title: %s-%d-%d." % (title, run, seg),
    ]
    if meta:
        block = {
            "theta": theta, "run_number": str(run),
            "run_title": "%s-%d-%d." % (title, run, seg),
            "norm_run": norm, "sequence_number": seg, "sequence_id": run,
            "dq_over_q": 0.0219,
        }
        lines.append("# Meta:" + json.dumps(block))
    if dq_label:
        lines.append("# Q [1/Angstrom]   R    dR    dQ [%s]" % dq_label)
    lines.append("  0.0104343515290010    0.9236196480099899    0.1012    0.000228")
    return _w(path, "\n".join(lines) + "\n")


_CONTEXT = """\
# copper | copper oxide — jen-jun2026 sample1

## Description
Deposited 50 nm copper on 3 nm titanium on a silicon substrate, in D2O electrolyte.

## Details
Electrolyte is pH 8.25, 0.1 M NaHCO3 sparged with nitrogen.

## Measurement conditions
The lowest angle always needs sample broadening — try up to 0.1 first.
Allow for an angle offset up to +/- 0.01.

## Measurements
Each line gives the run number, the run title, and the condition.

- 230536  `Sample1_Cu_air`   Full Q — measured in AIR, cell empty, in the normal cell
  geometry (still back reflection). The ambient medium is air, not D2O.
- 230539  `Sample1_Cu-echm`  Full Q — OCV, in D2O electrolyte.
- 230542  `Sample1_tNR`      tNR / PEIS — not spliced yet, DO NOT USE.
- 230549  `Sample1_OCV`      Single run OCV — skip.

The copper thickness is expected to change between data sets as the copper oxide
on the surface reduces under negative potential.
"""


_RANGE_CONTEXT = """\
# Copper film

## Description
Deposited 40 nm copper on 3 nm titanium on silicon, in D2O.

## Measurements

- For run lower than 230556, assume the sample description above.
- Starting from run 230556, the sample is changed to 100nm ionomer on 15 nm copper.
- Starting from run 230578, the sample is 3 nm copper oxide on 40 nm copper.
"""


_JOB_YAML = """\
describe: 'Air / 3 nm CuOx / 50 nm Cu / 3 nm Ti on Si.'
states:
- name: run_230536_air
  data:
  - REFL_230536_1_230536_partial.txt
  - REFL_230536_2_230537_partial.txt
  - REFL_230536_3_230538_partial.txt
  theta_offset:
    init: 0.0
    min: -0.01
    max: 0.01
  sample_broadening:
    init: 0.0
    min: 0.0
    max: 0.1
  background: true
  back_reflection: true
model_name: sample1_cu_air_230536
metadata:
  perform_assembly: true
"""


def _parameter(name, value, bounds):
    return {
        "name": name, "fixed": bounds is None,
        "slot": {"value": value, "__class__": "bumps.parameter.Variable"},
        "bounds": bounds, "__class__": "bumps.parameter.Parameter",
    }


def _problem_json(path, layers, extra=()):
    """A serialized bumps problem: layers plus any extra free parameters.

    *layers* is ``[(name, rho, thickness, roughness, rho_bounds)]``; the stack is
    ambient-first, which is how back reflection is assembled.
    """
    references = {}
    stack = []

    def add(name, value, bounds):
        key = "id-%s" % name.replace(" ", "-")
        references[key] = _parameter(name, value, bounds)
        return {"id": key, "__class__": "Reference"}

    for name, rho, thickness, roughness, rho_bounds in layers:
        stack.append({
            "name": name,
            "thickness": add("%s thickness" % name, thickness, None),
            "interface": add("%s interface" % name, roughness, [5.0, 30.0]),
            "material": {
                "name": name,
                "rho": add("%s rho" % name, rho, rho_bounds),
                "irho": add("%s irho" % name, 0.0, None),
                "__class__": "refl1d.sample.material.SLD",
            },
            "__class__": "refl1d.sample.layers.Slab",
        })

    for name, value, bounds in extra:
        add(name, value, bounds)

    document = {
        "$schema": "bumps-draft-03",
        "object": {
            "name": "sample1_cu_air_230536",
            "models": [{"sample": {"layers": stack}}],
            "__class__": "bumps.fitproblem.FitProblem",
        },
        "references": references,
    }
    return _w(path, json.dumps(document))


_LAYERS = [
    ("air", 0.0, 0.0, 13.87, None),
    ("CuOx", 5.21, 25.38, 10.60, [2.5, 6.5]),
    ("Cu", 6.518, 374.4, 7.82, [4.55, 8.55]),
    ("Ti", -2.30, 28.27, 5.33, [-5.0, 1.0]),
    ("Si", 2.07, 0.0, 0.0, None),
]


@pytest.fixture
def bundle(tmp_path):
    """A workflow state plus the artifacts it points at, and an nrw project."""
    out = str(tmp_path / "out")
    data = str(tmp_path / "data")
    project = str(tmp_path / "proj")

    ctx = _w(str(tmp_path / "context.md"), _CONTEXT)
    for seg, subrun in ((1, 230536), (2, 230537), (3, 230538)):
        _reduced(os.path.join(data, "REFL_230536_%d_%d_partial.txt" % (seg, subrun)),
                 230536, seg, subrun, norm=230530 + seg - 1)
    job = _w(os.path.join(out, "230536", "plan", "job_230536.yaml"), _JOB_YAML)
    results = os.path.join(out, "230536", "results")
    problem = _problem_json(os.path.join(results, "problem.json"), _LAYERS)
    _w(os.path.join(results, "final_state.json"), json.dumps({"final_chi2": 1.5566}))
    _w(os.path.join(project, "nrw.toml"),
       'contract_version = 1\n\n[beamtime]\nlabel = "jen-jun2026"\n')

    state = empty_state()
    state["workflow"] = {"run": 230536, "instrument": "REF_L", "ipts": "IPTS-36897"}
    state["inputs"]["operator"] = {
        "output_directory": out, "context_file": ctx, "sequence_total": 3,
    }
    state["stages"]["reduction"] = {
        "status": "ok", "params": {},
        "artifacts": {"partial_file": os.path.join(
            data, "REFL_230536_3_230538_partial.txt")},
        "info": {},
    }
    state["stages"]["analysis"] = {
        "status": "ok",
        "params": {"model_name": "sample1_cu_air_230536"},
        "artifacts": {"job_yaml": job, "problem_json": problem},
        "info": {},
    }
    state_path = save_state(state, str(tmp_path / "state.json")) or str(
        tmp_path / "state.json")
    return {"state": state, "state_path": state_path, "project": project,
            "out": out, "data": data, "context": ctx, "job": job,
            "results": results, "tmp": str(tmp_path)}


def _source(bundle):
    """A Source over the live state, rebuilt so edits to the job YAML are seen."""
    return open_source(bundle["state_path"])


# --------------------------------------------------------------------------
# context.md
# --------------------------------------------------------------------------


def test_parse_context_reads_sections_and_title():
    parsed = _parse(_CONTEXT)
    assert parsed["title"] == "copper | copper oxide — jen-jun2026 sample1"
    assert "50 nm copper" in parsed["sections"]["description"]
    assert "pH 8.25" in parsed["sections"]["details"]
    assert "sample broadening" in parsed["sections"]["measurement conditions"]


def test_parse_context_reads_per_run_bullets_with_titles():
    runs = _parse(_CONTEXT)["runs"]
    assert set(runs) == {230536, 230539, 230542, 230549}
    assert runs[230536]["title"] == "Sample1_Cu_air"
    assert runs[230539]["title"] == "Sample1_Cu-echm"


def test_parse_context_folds_indented_continuation_into_its_bullet():
    runs = _parse(_CONTEXT)["runs"]
    # The second line of the 230536 bullet is indented, so it belongs to it.
    assert "ambient medium is air" in runs[230536]["condition"]
    # ...and must not leak into the next run's condition.
    assert "ambient medium" not in runs[230539]["condition"]


def test_parse_context_marks_excluded_runs():
    runs = _parse(_CONTEXT)["runs"]
    assert runs[230542]["excluded"] is True   # DO NOT USE
    assert runs[230549]["excluded"] is True   # skip
    assert runs[230536]["excluded"] is False


def test_parse_context_keeps_trailing_prose_but_not_the_preamble():
    parsed = _parse(_CONTEXT)
    assert "copper thickness is expected to change" in parsed["trailing"]
    # The line before the first bullet describes the file format, not the science.
    assert "Each line gives" not in parsed["trailing"]


def test_parse_context_reads_run_range_boundaries():
    parsed = _parse(_RANGE_CONTEXT)
    assert parsed["runs"] == {}
    assert [b[0] for b in parsed["boundaries"]] == [0, 230556, 230578]


def test_measurement_notes_prefers_the_runs_own_line():
    notes = measurement_notes(_parse(_CONTEXT), 230539)
    assert "OCV" in notes["condition"]
    assert notes["title"] == "Sample1_Cu-echm"


def test_measurement_notes_refuses_an_excluded_run():
    with pytest.raises(WorkbenchError, match="not for analysis"):
        measurement_notes(_parse(_CONTEXT), 230542)


def test_measurement_notes_selects_the_containing_range_block():
    # The last boundary at or below the run wins. The lowest block here points
    # back at ## Description rather than restating the stack, which is why the
    # generated notes carry that section too.
    parsed = _parse(_RANGE_CONTEXT)
    assert "lower than 230556" in measurement_notes(parsed, 230536)["condition"]
    assert "ionomer" in measurement_notes(parsed, 230560)["condition"]
    assert "copper oxide" in measurement_notes(parsed, 230580)["condition"]
    # ...and the boundary that applies is named, so the choice is auditable.
    assert "230578" in measurement_notes(parsed, 230580)["source"]


def test_measurement_notes_refuses_a_run_it_cannot_place():
    # A per-run listing that simply does not mention this run: guessing which
    # physical sample it belongs to would aim a session at the wrong stack.
    with pytest.raises(WorkbenchError, match="says nothing about run 999999"):
        measurement_notes(_parse(_CONTEXT), 999999)


# --------------------------------------------------------------------------
# Reduced-file headers
# --------------------------------------------------------------------------


def test_read_header_converts_theta_from_radians_to_degrees(tmp_path):
    path = _reduced(str(tmp_path / "REFL_230536_1_230536_partial.txt"),
                    230536, 1, 230536)
    header = read_header(path)
    assert header["theta_deg"] == pytest.approx(math.degrees(THETA_RAD))
    assert header["theta_deg"] == pytest.approx(0.4504, abs=1e-4)


def test_read_header_reads_norm_run_and_dq_convention(tmp_path):
    path = _reduced(str(tmp_path / "a.txt"), 230536, 1, 230536, norm=230530)
    header = read_header(path)
    assert header["norm_run"] == 230530
    assert header["dq_is_fwhm"] is True


def test_read_header_reports_a_sigma_dq_column_as_not_fwhm(tmp_path):
    path = _reduced(str(tmp_path / "a.txt"), 230536, 1, 230536, dq_label="sigma")
    assert read_header(path)["dq_is_fwhm"] is False


def test_read_header_refuses_a_file_with_no_meta_block(tmp_path):
    path = _reduced(str(tmp_path / "a.txt"), 230536, 1, 230536, meta=False)
    with pytest.raises(WorkbenchError, match="no '# Meta:' block"):
        read_header(path)


def test_read_header_leaves_an_unlabelled_dq_column_unknown(tmp_path):
    path = _reduced(str(tmp_path / "a.txt"), 230536, 1, 230536, dq_label=None)
    assert read_header(path)["dq_is_fwhm"] is None


# --------------------------------------------------------------------------
# Locating the measurement
# --------------------------------------------------------------------------


def test_resolve_segments_finds_the_whole_measurement(bundle):
    run, segments = resolve_segments(_source(bundle))
    assert run == 230536
    assert [s["seg"] for s in segments] == [1, 2, 3]
    assert [s["subrun"] for s in segments] == [230536, 230537, 230538]


def test_resolve_segments_refuses_a_partial_measurement(bundle):
    os.remove(os.path.join(bundle["data"], "REFL_230536_2_230537_partial.txt"))
    with pytest.raises(WorkbenchError, match="not on disk"):
        resolve_segments(_source(bundle))


def test_resolve_segments_refuses_non_contiguous_segments(bundle):
    # Segments 1 and 3 with no 2: a plausible fit from two thirds of the data.
    _w(bundle["job"], _JOB_YAML.replace(
        "  - REFL_230536_2_230537_partial.txt\n", ""))
    with pytest.raises(WorkbenchError, match="not contiguous from 1"):
        resolve_segments(_source(bundle))


def test_resolve_segments_refuses_a_subrun_that_does_not_follow(bundle):
    # subrun 230999 for segment 2 should be 230537 — these are not one
    # measurement, and nr-workbench's watcher would quarantine them.
    _reduced(os.path.join(bundle["data"], "REFL_230536_2_230999_partial.txt"),
             230536, 2, 230999)
    _w(bundle["job"], _JOB_YAML.replace(
        "REFL_230536_2_230537_partial.txt", "REFL_230536_2_230999_partial.txt"))
    with pytest.raises(WorkbenchError, match="should be subrun 230537"):
        resolve_segments(_source(bundle))


def test_resolve_segments_refuses_a_combined_file(bundle):
    _w(os.path.join(bundle["data"], "REFL_230536_combined_data_auto.txt"), "x\n")
    _w(bundle["job"], _JOB_YAML.replace(
        "REFL_230536_2_230537_partial.txt", "REFL_230536_combined_data_auto.txt"))
    with pytest.raises(WorkbenchError, match="not a reduced angle segment"):
        resolve_segments(_source(bundle))


# --------------------------------------------------------------------------
# The prior fit and the checks over it
# --------------------------------------------------------------------------


def test_read_problem_resolves_references_into_a_stack(bundle):
    problem = read_problem(os.path.join(bundle["results"], "problem.json"))
    assert [layer["name"] for layer in problem["stack"]] == [
        "air", "CuOx", "Cu", "Ti", "Si"]
    cu = [layer for layer in problem["stack"] if layer["name"] == "Cu"][0]
    assert cu["rho"] == pytest.approx(6.518)
    assert cu["thickness"] == pytest.approx(374.4)


def test_read_problem_omits_fixed_parameters(bundle):
    problem = read_problem(os.path.join(bundle["results"], "problem.json"))
    names = {p["name"] for p in problem["parameters"]}
    assert "Cu rho" in names
    assert "Si rho" not in names      # fixed: no bounds
    assert "air thickness" not in names


def test_read_problem_returns_none_for_an_unreadable_file(tmp_path):
    assert read_problem(str(tmp_path / "absent.json")) is None
    assert read_problem(_w(str(tmp_path / "bad.json"), "not json")) is None


def test_declared_bounds_separates_ranges_from_switches(bundle):
    bounds, flags = declared_bounds(bundle["job"])
    assert bounds["theta_offset"] == (-0.01, 0.01)
    assert bounds["sample_broadening"] == (0.0, 0.1)
    # `background: true` is a switch, not a range — and bool subclasses int, so
    # this is the check that it is not read as one.
    assert flags["background"] is True
    assert "background" not in bounds


def _prior(parameters, stack=None, uncertainties=True):
    return {
        "tool": "aure", "model_name": "m", "chisq": 1.5, "n_segments": 3,
        "stack": stack or [], "parameters": parameters,
        "uncertainties_available": uncertainties, "analysis_dir": "", "trail": [],
    }


def _segments_and_headers(n=3):
    segments = [{"seg": i, "subrun": 230535 + i, "run": 230536, "name": "f%d" % i,
                 "path": ""} for i in range(1, n + 1)]
    headers = [{"norm_run": 230530, "dq_label": "fwhm", "theta_deg": 0.45,
                "run_title": "t"} for _ in range(n)]
    return segments, headers


def test_findings_flags_a_parameter_on_its_lower_bound():
    segments, headers = _segments_and_headers()
    prior = _prior([{"name": "Ti interface", "value": 5.02, "low": 5.0,
                     "high": 30.0, "std": 1.0}])
    found = findings(segments, headers, prior, {})
    assert [f["kind"] for f in found] == ["on-bound"]
    assert "lower bound" in found[0]["message"]


def test_findings_flags_a_parameter_on_its_upper_bound():
    segments, headers = _segments_and_headers()
    prior = _prior([{"name": "D2O interface", "value": 29.999, "low": 0.0,
                     "high": 30.0, "std": 0.1}])
    found = findings(segments, headers, prior, {})
    assert [f["kind"] for f in found] == ["on-bound"]
    assert "upper bound" in found[0]["message"]


def test_findings_leaves_a_parameter_inside_its_range_alone():
    segments, headers = _segments_and_headers()
    # 0.33 away from the floor of a span of 25; the tolerance is 0.25.
    assert BOUND_TOLERANCE * 25 < 0.33
    prior = _prior([{"name": "Ti interface", "value": 5.33, "low": 5.0,
                     "high": 30.0, "std": 1.0}])
    assert findings(segments, headers, prior, {}) == []


def test_findings_flags_a_fit_that_widened_a_declared_bound():
    segments, headers = _segments_and_headers()
    prior = _prior([{"name": "run_x theta_offset", "value": 0.02, "low": -0.05,
                     "high": 0.08, "std": 0.001}])
    found = findings(segments, headers, prior, {"theta_offset": (-0.01, 0.01)})
    kinds = [f["kind"] for f in found]
    assert "bound-widened" in kinds
    widened = [f for f in found if f["kind"] == "bound-widened"][0]
    assert "asked for [-0.01, 0.01]" in widened["message"]


def test_findings_accepts_a_fit_that_respected_the_declared_bound():
    segments, headers = _segments_and_headers()
    prior = _prior([{"name": "run_x theta_offset", "value": 0.004, "low": -0.01,
                     "high": 0.01, "std": 0.001}])
    found = findings(segments, headers, prior, {"theta_offset": (-0.01, 0.01)})
    assert [f["kind"] for f in found] == []


def test_findings_flags_an_sld_far_from_its_nominal():
    segments, headers = _segments_and_headers()
    prior = _prior([], stack=[{"name": "D2O", "rho": 4.50, "thickness": 0.0,
                              "roughness": 30.0}])
    found = findings(segments, headers, prior, {})
    assert [f["kind"] for f in found] == ["sld-off-nominal"]
    assert "nominal 6.36" in found[0]["message"]


def test_findings_says_nothing_about_a_material_it_has_no_nominal_for():
    segments, headers = _segments_and_headers()
    prior = _prior([], stack=[{"name": "piperION ionomer", "rho": 3.0,
                              "thickness": 1000.0, "roughness": 10.0}])
    assert findings(segments, headers, prior, {}) == []


def test_findings_reports_a_fit_with_no_posterior():
    segments, headers = _segments_and_headers()
    found = findings(segments, headers, _prior([], uncertainties=False), {})
    assert [f["kind"] for f in found] == ["no-uncertainties"]


def test_findings_does_not_flag_one_direct_beam_per_angle(bundle):
    # Each angle legitimately gets its own direct beam. nr-workbench's
    # `reconcile._direct_beams` looks for one angle differing BETWEEN runs, a
    # comparison a single measurement cannot make — so this must stay silent
    # rather than contradict it on every measurement ever made.
    segments, headers = _segments_and_headers()
    for index, header in enumerate(headers):
        header["norm_run"] = 230530 + index
    assert findings(segments, headers, _prior([]), {}) == []


def test_findings_reports_a_header_with_no_direct_beam():
    segments, headers = _segments_and_headers()
    headers[1]["norm_run"] = None
    found = findings(segments, headers, _prior([]), {})
    assert [f["kind"] for f in found] == ["no-direct-beam"]


def test_findings_reports_segments_that_disagree_about_dq():
    segments, headers = _segments_and_headers()
    headers[2]["dq_label"] = "sigma"
    found = findings(segments, headers, _prior([]), {})
    assert "mixed-dq-convention" in [f["kind"] for f in found]


# --------------------------------------------------------------------------
# The register
# --------------------------------------------------------------------------


def test_render_sample_yaml_matches_the_nrw_sample_schema():
    segments = [{"name": "REFL_230536_%d_%d_partial.txt" % (i, 230535 + i)}
                for i in (1, 2, 3)]
    document = yaml.safe_load(render_sample_yaml("s230536", 230536, "T", segments, "bt"))
    assert document["schema"] == "nrw-sample/1"
    assert document["id"] == "s230536"
    assert document["beamtime"] == "bt"
    assert document["series"] == []
    assert document["steady"][0]["run"] == 230536
    assert document["steady"][0]["segments"] == [
        "samples/s230536/data/steady/REFL_230536_%d_%d_partial.txt" % (i, 230535 + i)
        for i in (1, 2, 3)
    ]


def test_render_sample_yaml_survives_a_title_with_an_apostrophe():
    # Hand-quoting is what this replaced; a title is arbitrary prose.
    text = render_sample_yaml("s1", 1, "Jen's sample — \"air\"", [{"name": "f.txt"}], "")
    assert yaml.safe_load(text)["title"] == "Jen's sample — \"air\""


# --------------------------------------------------------------------------
# The stage
# --------------------------------------------------------------------------


def test_run_workbench_refuses_a_directory_that_is_not_an_nrw_project(bundle, tmp_path):
    with pytest.raises(WorkbenchError, match="not an nr-workbench project"):
        run_workbench(_source(bundle), str(tmp_path / "nope"))


def test_run_workbench_writes_nothing_without_write(bundle):
    planned, _notes, meta = run_workbench(_source(bundle), bundle["project"])
    assert meta["run"] == 230536
    assert meta["sample"] == "s230536"
    assert planned
    assert not os.path.exists(os.path.join(bundle["project"], "samples"))


def test_run_workbench_creates_the_sample(bundle):
    run_workbench(_source(bundle), bundle["project"], write=True)
    root = os.path.join(bundle["project"], "samples", "s230536")
    assert os.path.isfile(os.path.join(root, "sample.md"))
    assert os.path.isfile(os.path.join(root, "sample.yaml"))
    assert os.path.isfile(os.path.join(root, "handoff", "prior-analysis.md"))
    assert os.path.isfile(os.path.join(root, "handoff", "prior-analysis.json"))
    for seg, subrun in ((1, 230536), (2, 230537), (3, 230538)):
        assert os.path.isfile(os.path.join(
            root, "data", "steady", "REFL_230536_%d_%d_partial.txt" % (seg, subrun)))
    # The full set of subdirectories nrw's own `sample new` makes, so a sample
    # handed over this way is indistinguishable from a scaffolded one.
    for name in ("models", "results", "reports", "assessments", "data/tnr", "data/raw"):
        assert os.path.isdir(os.path.join(root, name))


def test_generated_notes_carry_the_headings_the_agent_reads(bundle):
    run_workbench(_source(bundle), bundle["project"], write=True)
    with open(os.path.join(bundle["project"], "samples", "s230536", "sample.md")) as f:
        text = f.read()
    for heading in ("## Description", "## Details", "## Measurements",
                    "## Measurement conditions", "## Fits to perform"):
        assert heading in text


def test_fits_to_perform_holds_prose_and_names_only_this_run(bundle):
    run_workbench(_source(bundle), bundle["project"], write=True)
    with open(os.path.join(bundle["project"], "samples", "s230536", "sample.md")) as f:
        text = f.read()
    task = text.split("## Fits to perform", 1)[1]
    # `agent/session.py::compose` strips HTML comments and refuses an empty
    # section, so the task has to be real prose.
    assert re.sub(r"<!--.*?-->", "", task, flags=re.DOTALL).strip()
    assert "Fit run 230536, and only run 230536" in task
    assert "230539" not in task


def test_generated_notes_contain_no_number_nrw_would_read_as_a_foreign_run(bundle):
    """The property that cannot be checked from inside this repo.

    nr-workbench matches ``\\b(\\d{6})\\b`` over the whole of ``sample.md`` and
    compares every hit against the data on disk. Two things put a six-digit run
    of digits in this file without anyone meaning to: a full-precision ISO
    timestamp (its microseconds field is exactly six digits, bounded by ``.``
    and ``+``) and a ``%.6g`` float such as ``0.999774``. Either becomes a run
    the project does not have, reported as missing for the life of the project.
    """
    run_workbench(_source(bundle), bundle["project"], write=True)
    with open(os.path.join(bundle["project"], "samples", "s230536", "sample.md")) as f:
        text = f.read()
    allowed = {"230536", "230537", "230538"}
    assert set(NRW_RUN_RE.findall(text)) <= allowed


def test_prior_analysis_is_kept_out_of_the_notes_but_pointed_at(bundle):
    run_workbench(_source(bundle), bundle["project"], write=True)
    root = os.path.join(bundle["project"], "samples", "s230536")
    with open(os.path.join(root, "sample.md")) as f:
        notes = f.read()
    with open(os.path.join(root, "handoff", "prior-analysis.md")) as f:
        prior = f.read()
    assert "handoff/prior-analysis.md" in notes
    # The parameter table is the thing that would smuggle six-digit decimals in.
    assert "| Parameter | Value |" not in notes
    assert "| Parameter | Value |" in prior
    assert "chi-squared 1.557" in notes


def test_run_workbench_refuses_to_overwrite_notes_it_did_not_write(bundle):
    root = os.path.join(bundle["project"], "samples", "s230536")
    _w(os.path.join(root, "sample.md"), "# my analysis\n\n## Fits to perform\n\nmine\n")
    with pytest.raises(WorkbenchError, match="was not written by this tool"):
        run_workbench(_source(bundle), bundle["project"], write=True)
    with open(os.path.join(root, "sample.md")) as f:
        assert "my analysis" in f.read()


def test_run_workbench_replaces_its_own_notes(bundle):
    run_workbench(_source(bundle), bundle["project"], write=True)
    path = os.path.join(bundle["project"], "samples", "s230536", "sample.md")
    with open(path) as f:
        assert MARKER in f.read()
    # Idempotent: a second pass owns the file and rewrites it without --force.
    run_workbench(_source(bundle), bundle["project"], write=True)


def test_force_overrides_a_foreign_sample_md(bundle):
    root = os.path.join(bundle["project"], "samples", "s230536")
    _w(os.path.join(root, "sample.md"), "# mine\n")
    run_workbench(_source(bundle), bundle["project"], write=True, force=True)
    with open(os.path.join(root, "sample.md")) as f:
        assert MARKER in f.read()


def test_run_workbench_refuses_an_excluded_run(bundle):
    # Re-point the whole bundle at 230542, which context.md marks DO NOT USE.
    for seg, subrun in ((1, 230542), (2, 230543), (3, 230544)):
        _reduced(os.path.join(bundle["data"],
                              "REFL_230542_%d_%d_partial.txt" % (seg, subrun)),
                 230542, seg, subrun)
    _w(bundle["job"], _JOB_YAML.replace("230536_1_230536", "230542_1_230542")
                              .replace("230536_2_230537", "230542_2_230543")
                              .replace("230536_3_230538", "230542_3_230544"))
    with pytest.raises(WorkbenchError, match="not for analysis"):
        run_workbench(_source(bundle), bundle["project"], write=True)


def test_run_workbench_refuses_an_unusable_sample_name(bundle):
    with pytest.raises(WorkbenchError, match="not a usable sample name"):
        run_workbench(_source(bundle), bundle["project"], sample="../escape")


def test_run_workbench_leaves_a_differing_existing_data_file_alone(bundle):
    target = os.path.join(bundle["project"], "samples", "s230536",
                          "data", "steady", "REFL_230536_1_230536_partial.txt")
    _w(target, "# the project's own copy\n")
    planned, notes, _meta = run_workbench(_source(bundle), bundle["project"], write=True)
    with open(target) as f:
        assert "the project's own copy" in f.read()
    assert any(p["action"] == "differs" for p in planned)
    assert any("left alone" in n for n in notes)


def test_run_workbench_reports_a_title_that_disagrees_with_the_notes(bundle):
    # context.md calls 230536 `Sample1_Cu_air`; the headers say it was the
    # electrochemistry run. One of the two decides the ambient medium.
    for seg, subrun in ((1, 230536), (2, 230537), (3, 230538)):
        _reduced(os.path.join(bundle["data"],
                              "REFL_230536_%d_%d_partial.txt" % (seg, subrun)),
                 230536, seg, subrun, title="Sample1_Cu-echm")
    _planned, _notes, meta = run_workbench(_source(bundle), bundle["project"])
    assert meta["findings"][0]["kind"] == "title-mismatch"


def test_run_workbench_still_works_with_no_prior_fit(bundle):
    os.remove(os.path.join(bundle["results"], "problem.json"))
    planned, _notes, meta = run_workbench(_source(bundle), bundle["project"], write=True)
    assert meta["chisq"] is None
    assert "no-prior-fit" in [f["kind"] for f in meta["findings"]]
    assert not any("prior-analysis" in p["path"] for p in planned)


def test_run_workbench_refuses_a_missing_context_file(bundle):
    # Re-saved, because the source is read back from disk rather than from the
    # in-memory dict.
    bundle["state"]["inputs"]["operator"]["context_file"] = "/nope/context.md"
    save_state(bundle["state"], bundle["state_path"])
    with pytest.raises(WorkbenchError, match="no readable context file"):
        run_workbench(_source(bundle), bundle["project"])


def test_prior_analysis_json_keeps_declared_and_fitted_bounds_apart(bundle):
    run_workbench(_source(bundle), bundle["project"], write=True)
    path = os.path.join(bundle["project"], "samples", "s230536",
                        "handoff", "prior-analysis.json")
    with open(path) as f:
        document = json.load(f)
    assert document["schema"] == "ndip-workbench-prior/1"
    assert document["declared"]["theta_offset"] == [-0.01, 0.01]
    assert document["declared_flags"]["background"] is True
    assert document["prior"]["chisq"] == pytest.approx(1.5566)


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------


def test_main_writes_and_reports(bundle, tmp_path, capsys):
    main([bundle["state_path"], "--project", bundle["project"], "--write"])
    err = capsys.readouterr().err
    assert "run 230536" in err
    assert "nrw agent run s230536" in err


def test_main_json_is_machine_readable(bundle, tmp_path, capsys):
    main([bundle["state_path"], "--project", bundle["project"], "--json"])
    document = json.loads(capsys.readouterr().out)
    assert document["wrote"] is False
    assert document["meta"]["sample"] == "s230536"
    # The rendered bodies are not part of the machine payload.
    assert all("body" not in item for item in document["planned"])


def test_main_exits_with_a_message_rather_than_a_traceback(bundle, tmp_path):
    with pytest.raises(SystemExit) as caught:
        main([bundle["state_path"], "--project", str(tmp_path / "not-a-project")])
    assert "not an nr-workbench project" in str(caught.value)


# --------------------------------------------------------------------------
# Reading a provenance package
# --------------------------------------------------------------------------


def _packaged(bundle, tmp_path, name="pkg"):
    """Build a real provenance package from the fixture with ``ndip-package``.

    Built by the actual packager rather than a hand-made imitation, so these
    tests fail if ``ndip-package`` changes what it copies -- which is exactly the
    coupling worth catching, since this reader depends on it.
    """
    from ndip_state.package import run_package

    package_dir = str(tmp_path / name)
    run_package(bundle["state"], package_dir)
    return package_dir


def test_open_source_refuses_a_directory_that_is_not_a_package(tmp_path):
    os.makedirs(str(tmp_path / "plain"))
    with pytest.raises(WorkbenchError, match="not a provenance package"):
        open_source(str(tmp_path / "plain"))


def test_open_source_refuses_a_path_that_is_neither(tmp_path):
    with pytest.raises(WorkbenchError, match="neither a provenance package"):
        open_source(str(tmp_path / "absent"))


def test_package_is_detected_and_resolved_from_the_inside(bundle, tmp_path):
    source = open_source(_packaged(bundle, tmp_path))
    assert source.kind == "package"
    # inputs/, plan/ and results/ all inside the package, not the original tree.
    assert source.reduced_dir.endswith(os.path.join("pkg", "inputs"))
    assert source.analysis_dir.endswith(os.path.join("pkg", "results"))
    assert source.context_file.endswith(os.path.join("inputs", "context.md"))


def test_package_hands_over_the_same_measurement_as_the_state(bundle, tmp_path):
    from_state = run_workbench(_source(bundle), bundle["project"])
    package = open_source(_packaged(bundle, tmp_path))
    from_package = run_workbench(package, bundle["project"], sample="p230536")

    assert from_state[2]["run"] == from_package[2]["run"] == 230536
    assert from_state[2]["segments"] == from_package[2]["segments"]
    assert from_state[2]["thetas"] == from_package[2]["thetas"]
    assert from_state[2]["read_from"] == "state"
    assert from_package[2]["read_from"] == "package"


def test_package_reads_chisq_from_the_checkpoint_trail(bundle, tmp_path):
    """The property that makes the package sufficient.

    ``ndip-package`` references ``final_state.json`` instead of copying it -- it
    is most of a megabyte of arrays -- so the number it states is not in the
    package. The finalize checkpoint's prose is, and carries the same figure.
    """
    _w(os.path.join(bundle["results"], "checkpoints", "006_finalize.md"),
       "# Checkpoint: finalize (iteration 1)\n\n"
       "**Final model:** iteration 0 (χ² = 1.5566, 16 free parameters).\n")
    package = open_source(_packaged(bundle, tmp_path))
    assert not os.path.isfile(os.path.join(package.analysis_dir, "final_state.json"))
    _planned, _notes, meta = run_workbench(package, bundle["project"],
                                           sample="p230536")
    assert meta["chisq"] == pytest.approx(1.5566)


def test_a_moved_package_still_works(bundle, tmp_path):
    """Portability is the whole reason to prefer a package over a state.

    The state records absolute paths into the tree the pipeline ran in. A
    package carries its inputs, so it survives being copied somewhere else --
    and to prove it, the original tree is deleted first.
    """
    import shutil

    package_dir = _packaged(bundle, tmp_path)
    moved = str(tmp_path / "elsewhere" / "230536")
    shutil.copytree(package_dir, moved)
    shutil.rmtree(bundle["out"])
    shutil.rmtree(bundle["data"])
    os.remove(bundle["context"])

    _planned, _notes, meta = run_workbench(open_source(moved), bundle["project"],
                                           write=True)
    assert meta["run"] == 230536
    assert len(meta["segments"]) == 3
    root = os.path.join(bundle["project"], "samples", "s230536")
    assert os.path.isfile(os.path.join(root, "sample.md"))
    for seg, subrun in ((1, 230536), (2, 230537), (3, 230538)):
        assert os.path.isfile(os.path.join(
            root, "data", "steady", "REFL_230536_%d_%d_partial.txt" % (seg, subrun)))


def test_a_package_resolves_segments_inside_itself_not_by_absolute_path(bundle, tmp_path):
    """An absolute path in the plan must not lead out of a moved package.

    Following it either fails or -- worse -- finds a different file with the same
    name on the host it was copied to.
    """
    import shutil

    _w(bundle["job"], _JOB_YAML.replace(
        "  - REFL_230536_1_230536_partial.txt",
        "  - " + os.path.join(bundle["data"], "REFL_230536_1_230536_partial.txt")))
    package_dir = _packaged(bundle, tmp_path)
    shutil.rmtree(bundle["data"])

    run, segments = resolve_segments(open_source(package_dir))
    assert run == 230536
    assert all(s["path"].startswith(package_dir) for s in segments)


def test_main_accepts_a_package_positionally(bundle, tmp_path, capsys):
    package_dir = _packaged(bundle, tmp_path)
    main([package_dir, "--project", bundle["project"], "--json"])
    document = json.loads(capsys.readouterr().out)
    assert document["meta"]["read_from"] == "package"
    assert document["meta"]["run"] == 230536


def test_chisq_is_never_taken_from_a_per_segment_value(bundle, tmp_path):
    """The regression for a plausible wrong number.

    A run that hits its iteration cap has no finalize checkpoint. The nearest
    chi-squared in the trail is then a ``Per-file χ²`` entry, and taking the last
    bare match in the file reported 11.47 for run 230553 against a true 3.93.
    An unrecorded chi-squared is the correct answer here; a segment's is not.
    """
    _w(os.path.join(bundle["results"], "checkpoints", "004_fitting.md"),
       "**χ² progression:** iter 0: 7.54\n\n"
       "**Fit Results:** χ² = 7.54\n\n"
       "**Per-file χ²:**\n- a: χ² = 14.57\n- b: χ² = 6.14\n- c: χ² = 1.41\n")
    os.remove(os.path.join(bundle["results"], "final_state.json"))
    package = open_source(_packaged(bundle, tmp_path))
    _planned, _notes, meta = run_workbench(package, bundle["project"])
    assert meta["chisq"] is None


def test_chisq_comes_from_the_finalize_statement_when_there_is_one(bundle, tmp_path):
    _w(os.path.join(bundle["results"], "checkpoints", "004_fitting.md"),
       "**Per-file χ²:**\n- a: χ² = 14.57\n- b: χ² = 1.41\n")
    _w(os.path.join(bundle["results"], "checkpoints", "006_finalize.md"),
       "**Final model:** iteration 0 (χ² = 1.5566, 16 free parameters).\n")
    os.remove(os.path.join(bundle["results"], "final_state.json"))
    package = open_source(_packaged(bundle, tmp_path))
    _planned, _notes, meta = run_workbench(package, bundle["project"])
    assert meta["chisq"] == pytest.approx(1.5566)


def test_chisq_prefers_the_manifest_over_the_prose(bundle, tmp_path):
    """The manifest value is lifted from final_state.json, so it is the real one.

    The trail here would answer 1.5566 from its finalize line; the manifest
    records the authoritative 2.0671. The manifest must win.
    """
    _w(os.path.join(bundle["results"], "checkpoints", "006_finalize.md"),
       "**Final model:** iteration 0 (χ² = 1.5566, 16 free parameters).\n")
    _w(os.path.join(bundle["results"], "final_state.json"),
       json.dumps({"final_chi2": 2.067054452415206, "iterations": 5}))
    package_dir = _packaged(bundle, tmp_path)
    with open(os.path.join(package_dir, "MANIFEST.json")) as f:
        assert json.load(f)["fit"]["chisq"] == pytest.approx(2.067054452415206)
    _planned, _notes, meta = run_workbench(open_source(package_dir),
                                          bundle["project"])
    assert meta["chisq"] == pytest.approx(2.067054452415206)


def test_state_and_package_agree_on_chisq(bundle, tmp_path):
    """The two input paths must not disagree about the headline number."""
    _w(os.path.join(bundle["results"], "final_state.json"),
       json.dumps({"final_chi2": 3.9310436721007624, "iterations": 5}))
    from_state = run_workbench(_source(bundle), bundle["project"])[2]
    from_package = run_workbench(open_source(_packaged(bundle, tmp_path)),
                                 bundle["project"], sample="p230536")[2]
    assert from_state["chisq"] == from_package["chisq"] == pytest.approx(3.9310436721007624)
