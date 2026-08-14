"""``ndip-workbench`` -- hand one finished measurement to an nr-workbench project.

The pipeline ends with an answer nobody has to accept: AuRE's fit, an ISAAC
record, and a provenance package. What it does not produce is a *place to keep
working*. nr-workbench is that place -- it can drive a coding harness unattended
(``nrw agent run``) and then hand the result to a person (``nrw handoff``) -- but
it will not start without one specific thing: prose under ``## Fits to perform``
in ``samples/<id>/sample.md``. That is its only task channel, and nothing else
substitutes for it.

So this stage writes that file, plus the data and register beside it, and stops.

Three rules shape it, and each exists because of something that would otherwise
go wrong.

**The glue conforms to the tool, never the reverse.** nr-workbench is not
modified and is not imported. Everything written here is a file format
nr-workbench already documents and already reads: ``nrw-sample/1`` for the
register (``sample.yaml``), the section anatomy of the scaffolded ``sample.md``,
and the reduced-file names its own filename grammar matches. If a format here
disagrees with nr-workbench, this file is what is wrong.

**Never overwrite prose a person wrote.** A target project may already hold a
worked analysis -- the jen-jun2026 reference project has fifty recorded fits and
a hand-written ``## Fits to perform`` declaring co-refinements no generator would
invent. So a ``sample.md`` without this tool's marker is refused outright, and
nothing is written at all without ``--write``.

**One handoff is one measurement.** The angle segments of that measurement are
co-refined against each other, because that is what the sample notes ask for and
what AuRE did -- but a second measurement is never pulled in. Multi-measurement
co-refinement is judgement work that belongs to the person at ``nrw handoff``,
not to an unattended session, and keeping the scope at one measurement is also
what lets the task text be generated deterministically instead of guessed at.

Every artifact is resolved from the paths recorded in the state, exactly as
``ndip-package`` does, so this works for the flat tool layout and for
``ndip-run``'s per-run subdirs without knowing which produced it.
"""

import argparse
import datetime
import json
import math
import os
import re
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version

from .adapters import _sha256
from .package import _final_fit_dir, _partials_from_job
from .projection import _get
from .state import load_state


#: Marks a ``sample.md`` this tool owns. A file without it was written by a
#: person (or by nr-workbench's own scaffold) and is never overwritten.
MARKER = "<!-- ndip-workbench:generated"

#: The subdirectories nr-workbench's own ``nrw sample new`` creates. Made in
#: full rather than only the one we write into, so a sample handed over this way
#: is indistinguishable from a scaffolded one.
SAMPLE_SUBDIRS = (
    "data/steady",
    "data/tnr",
    "data/raw",
    "models",
    "assessments",
    "results",
    "reports",
)

#: How close to a bound counts as sitting on it, as a fraction of the range.
#: The same 0.01 nr-workbench's ``fitting/assess.py`` and AuRE both use, so all
#: three agree about the same fit rather than reporting three different verdicts.
BOUND_TOLERANCE = 0.01

#: Nominal SLDs in 1e-6 A^-2 for the handful of materials this beamline sees,
#: used only to say "the fit put this material well away from its book value".
#: Deliberately short: a name absent here is skipped in silence, because a wrong
#: nominal would manufacture a finding, which is worse than missing one.
NOMINAL_SLD = {
    "air": 0.0,
    "vacuum": 0.0,
    "si": 2.07,
    "sio2": 3.47,
    "d2o": 6.36,
    "h2o": -0.56,
    "cu": 6.55,
    "cuox": 5.36,
    "cuoxide": 5.36,
    "cu2o": 5.36,
    "cuo": 6.46,
    "ti": -1.95,
    "cr": 3.03,
    "au": 4.66,
}

#: How far a fitted SLD may sit from its nominal before it is worth reporting,
#: in 1e-6 A^-2. Generous -- oxides are genuinely porous and hydrated, and a
#: solvent legitimately varies -- so this only catches the gross case.
SLD_TOLERANCE = 0.8

#: A ``## Measurements`` bullet naming one run: ``- 230536  `Title`  condition``.
RUN_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<run>\d{6})\b(?P<rest>.*)$")

#: A backticked run title in that bullet.
BULLET_TITLE_RE = re.compile(r"`(?P<title>[^`]+)`")

#: The two run-range phrasings seen in a ``context.md`` that describes several
#: physical samples in one file, rather than listing runs one per line.
LOWER_THAN_RE = re.compile(r"for runs?\s+lower than\s+(?P<run>\d{6})", re.IGNORECASE)
STARTING_FROM_RE = re.compile(r"starting (?:from|at) run\s+(?P<run>\d{6})", re.IGNORECASE)

#: Phrases in a run's own bullet that mean "this one is not for analysis".
EXCLUDED_RE = re.compile(r"do not use|\bskip\b|\bunusable\b", re.IGNORECASE)

#: The ``# Meta:`` line carrying the reduction's JSON metadata block.
META_PREFIX = "# Meta:"

#: The column-title line, the only place on disk that says whether the 4th
#: column is a full width or a standard deviation. The two differ by 2.355.
DQ_COLUMN_RE = re.compile(r"^#.*\bdQ\b\s*\[\s*(?P<label>[^\]]*?)\s*\]", re.IGNORECASE)

#: Header lines to scan before giving up. The header is a dozen lines.
MAX_HEADER_LINES = 60


#: The accepted fit's chi-squared as the finalize node states it in its prose.
#:
#: Anchored on the two phrasings that mean *the whole fit*, and deliberately not
#: on a bare ``χ² =``. A fitting checkpoint prints a ``Per-file χ²:`` block, so
#: the last bare match in one of those files is a single segment's chi-squared,
#: not the overall figure -- on run 230553 that was 11.47 against a true 3.93.
#: Reporting that to a session is precisely the plausible-wrong-number this whole
#: design exists to avoid, so an unanchored match is not used at all.
FINAL_CHISQ_RES = (
    re.compile(r"Final\s+model:.*?(?:χ²|chi2|chi-squared)\s*=\s*"
               r"(?P<value>[0-9]*\.?[0-9]+)", re.IGNORECASE | re.DOTALL),
    re.compile(r"Final\s+(?:χ²|chi2|chi-squared)\s*=\s*"
               r"(?P<value>[0-9]*\.?[0-9]+)", re.IGNORECASE),
)


class WorkbenchError(RuntimeError):
    """The handoff could not be prepared. The message is for a person."""


class Source(object):
    """Where the artifacts of one finished measurement actually are.

    Two shapes reach this tool and both are legitimate:

    * a **provenance package** from ``ndip-package`` -- self-contained, hashed,
      git-storable, and the only form that survives being moved to another
      machine, because everything it needs is inside it;
    * a **live workflow state** -- the paths as the pipeline wrote them, which
      resolve only where it ran.

    Resolving them to the same four locations here means nothing downstream has
    to care which it was given.
    """

    def __init__(self, kind, root, state, context_file, job_yaml, reduced_dir,
                 analysis_dir):
        self.kind = kind
        self.root = root
        self.state = state
        self.context_file = context_file
        self.job_yaml = job_yaml
        self.reduced_dir = reduced_dir
        self.analysis_dir = analysis_dir


def open_source(path):
    """Resolve a provenance package or a state file into a :class:`Source`.

    Args:
        path: A directory holding ``MANIFEST.json`` (a provenance package), or a
            workflow-state JSON file.

    Returns:
        The resolved :class:`Source`.

    Raises:
        WorkbenchError: If the path is neither, or a package is missing a part
            this handoff cannot do without.
    """
    if os.path.isdir(path):
        return _open_package(path)
    if os.path.isfile(path):
        return _open_state(path)
    raise WorkbenchError(
        "%r is neither a provenance package directory nor a state file." % path
    )


def _open_package(path):
    """A package is read from the inside, so a moved copy still works."""
    if not os.path.isfile(os.path.join(path, "MANIFEST.json")):
        raise WorkbenchError(
            "%s has no MANIFEST.json, so it is not a provenance package.\n"
            "Build one with `ndip-package --state <state.json> -o %s`, or pass "
            "the state file directly." % (path, path)
        )
    state_path = os.path.join(path, "state.json")
    if not os.path.isfile(state_path):
        raise WorkbenchError("%s has no state.json" % path)
    state = load_state(state_path)

    inputs = os.path.join(path, "inputs")
    # The package copies the context file under its own basename. Prefer that
    # name, then any single .md in inputs/ -- never a path from the state, which
    # points at the machine the pipeline ran on.
    named = _get(state, "inputs", "operator", "context_file")
    context = os.path.join(inputs, os.path.basename(named)) if named else ""
    if not (context and os.path.isfile(context)):
        candidates = sorted(
            os.path.join(inputs, name) for name in os.listdir(inputs)
            if name.endswith(".md")
        ) if os.path.isdir(inputs) else []
        context = candidates[0] if len(candidates) == 1 else ""
    if not context:
        raise WorkbenchError(
            "%s/inputs/ holds no context file, and it is the only statement of "
            "what this sample is and what to fit." % path
        )

    plan_dir = os.path.join(path, "plan")
    plans = sorted(
        os.path.join(plan_dir, name) for name in os.listdir(plan_dir)
        if name.endswith((".yaml", ".yml"))
    ) if os.path.isdir(plan_dir) else []
    if len(plans) != 1:
        raise WorkbenchError(
            "%s/plan/ holds %d plan YAML(s); expected exactly one."
            % (path, len(plans))
        )

    return Source(
        kind="package", root=path, state=state, context_file=context,
        job_yaml=plans[0], reduced_dir=inputs,
        analysis_dir=os.path.join(path, "results"),
    )


def _open_state(path):
    """A live state: every location is an absolute path it recorded."""
    state = load_state(path)
    job_yaml = _get(state, "stages", "analysis", "artifacts", "job_yaml")
    if not job_yaml:
        raise WorkbenchError(
            "%s records no analysis job YAML, so there is no measurement to "
            "hand over. Run the plan stage first." % path
        )
    partial = _get(state, "stages", "reduction", "artifacts", "partial_file")
    problem = _get(state, "stages", "analysis", "artifacts", "problem_json")
    return Source(
        kind="state", root=os.path.dirname(os.path.abspath(path)), state=state,
        context_file=_get(state, "inputs", "operator", "context_file"),
        job_yaml=job_yaml,
        reduced_dir=os.path.dirname(partial) if partial else (
            _get(state, "inputs", "operator", "output_directory") or ""),
        analysis_dir=os.path.dirname(problem) if problem else "",
    )


def _now():
    """An ISO-8601 UTC stamp at second precision.

    Second precision, not microsecond, and it matters. A microseconds field is
    exactly six digits bounded by ``.`` and ``+`` -- which is precisely what
    nr-workbench's ``\\b(\\d{6})\\b`` run-number scan matches, so a full-precision
    timestamp anywhere in ``sample.md`` invents a run the project does not have
    and reports it as missing forever. Second precision is also what
    nr-workbench stamps its own records with.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.replace(microsecond=0).isoformat()


def _self_version():
    try:
        return version("yaml-parser")
    except PackageNotFoundError:
        return None


# --------------------------------------------------------------------------
# The reduced files: names, headers
# --------------------------------------------------------------------------

#: nr-workbench's own filename grammar, from ``project/scan.py``. Copied rather
#: than loosened: ``REFL_230536_2_230537_partial.txt`` holds two six-digit
#: numbers, and a bare ``\d{6}`` search reads the subrun as a second run --
#: which is a real neighbouring measurement at a beamline that numbers
#: consecutively.
PARTIAL_RE = re.compile(r"^REFL_(?P<run>\d+)_(?P<seg>\d+)_(?P<subrun>\d+)_partial\.txt$")
COMBINED_RE = re.compile(r"^REFL_(?P<run>\d+)_combined_data_auto\.txt$")


def read_header(path):
    """Return the reduction metadata a reduced REF_L file records about itself.

    Args:
        path: The reduced ``.txt`` file.

    Returns:
        A dict with ``theta_deg``, ``run_title``, ``norm_run``,
        ``sequence_number``, ``sequence_id``, ``dq_is_fwhm`` and ``dq_label``.
        Keys whose source is absent are ``None`` rather than guessed.

    Raises:
        WorkbenchError: If the file cannot be read, or carries no ``# Meta:``
            block. Everything downstream depends on ``theta``, so a missing
            header is a stop rather than a default.
    """
    meta = None
    dq_label = None
    try:
        with open(path) as handle:
            for index, line in enumerate(handle):
                if index >= MAX_HEADER_LINES:
                    break
                if line.startswith(META_PREFIX):
                    meta = json.loads(line[len(META_PREFIX):].strip())
                found = DQ_COLUMN_RE.match(line)
                if found:
                    dq_label = found.group("label").strip().lower()
    except (OSError, ValueError) as exc:
        raise WorkbenchError("cannot read the header of %s: %s" % (path, exc))

    if not isinstance(meta, dict):
        raise WorkbenchError(
            "%s carries no '# Meta:' block, so its incident angle is unknown.\n"
            "theta sets the resolution of every point through dT = dq/q*tan(theta); "
            "a plausible wrong value produces a fit rather than a failure, so this "
            "will not be guessed." % path
        )

    # Angles are stored in radians. 0.00786 rad is 0.45 deg; reading it as
    # degrees is the kind of error that produces a fit rather than an error.
    theta = meta.get("theta")
    return {
        "theta_deg": math.degrees(theta) if isinstance(theta, (int, float)) else None,
        "run_title": meta.get("run_title"),
        "norm_run": meta.get("norm_run"),
        "sequence_number": meta.get("sequence_number"),
        "sequence_id": meta.get("sequence_id"),
        "dq_label": dq_label,
        # The reduction writes FWHM today and there is an intention to move to
        # sigma, so this is read per file and never assumed.
        "dq_is_fwhm": None if dq_label is None else dq_label.startswith("fwhm"),
    }


def resolve_segments(source):
    """Locate the reduced files of the measurement this source describes.

    The plan YAML lists them by bare basename, which is what makes a provenance
    package portable: the same names resolve against ``inputs/`` inside the
    package or against the reduction's output directory on the machine that ran
    it, with nothing in the plan needing to change.

    Args:
        source: The resolved :class:`Source`.

    Returns:
        ``(run, [{"path", "name", "seg", "subrun"} ...])`` sorted by segment.

    Raises:
        WorkbenchError: If the plan names no data, a named file is missing, or
            the set is not a whole measurement.
    """
    job_yaml = source.job_yaml
    names = _partials_from_job(job_yaml)
    if not names:
        raise WorkbenchError("%s lists no files under states[].data" % job_yaml)

    reduced_dir = source.reduced_dir
    segments = []
    missing = []
    for name in names:
        base = os.path.basename(name)
        # The name is validated before the file is looked for: a plan naming the
        # wrong *kind* of file is a plan error whether or not it happens to be on
        # this disk, and reporting it as merely "missing" hides that.
        found = PARTIAL_RE.match(base)
        if not found:
            raise WorkbenchError(
                "%s is not a reduced angle segment. This stage hands over one "
                "steady-state measurement as its angle segments; a combined file "
                "or a time-resolved slice is not that." % base
            )
        # Inside a package, resolve against the package. An absolute path in the
        # plan points at the machine the pipeline ran on, and following it out of
        # a package that was copied elsewhere either fails or -- worse -- finds a
        # different file with the same name.
        if source.kind == "package" or not os.path.isabs(name):
            path = os.path.join(reduced_dir, base)
        else:
            path = name
        if not os.path.isfile(path):
            missing.append(path)
            continue
        segments.append({
            "path": path,
            "name": base,
            "run": int(found.group("run")),
            "seg": int(found.group("seg")),
            "subrun": int(found.group("subrun")),
        })

    if missing:
        raise WorkbenchError(
            "the plan names %d file(s) that are not on disk:\n  %s\n"
            "A measurement is handed over whole or not at all -- fitting two "
            "segments of three returns a plausible number, not an error."
            % (len(missing), "\n  ".join(missing))
        )

    runs = {s["run"] for s in segments}
    if len(runs) != 1:
        raise WorkbenchError(
            "the plan mixes runs %s. One handoff is one measurement."
            % ", ".join(str(r) for r in sorted(runs))
        )
    run = segments[0]["run"]
    segments.sort(key=lambda s: s["seg"])

    # The same two rules nr-workbench's watcher uses to decide a measurement is
    # trustworthy. Checked here so a measurement it would quarantine is never
    # handed to it in the first place.
    indices = [s["seg"] for s in segments]
    if indices != list(range(1, len(indices) + 1)):
        raise WorkbenchError(
            "run %d has angle segments %s, which are not contiguous from 1. A "
            "segment is missing, or a file from another run is in the plan."
            % (run, indices)
        )
    for entry in segments:
        expected = run + entry["seg"] - 1
        if entry["subrun"] != expected:
            raise WorkbenchError(
                "run %d segment %d names subrun %d, but consecutive segments "
                "should be subrun %d. These files are probably not all the same "
                "measurement." % (run, entry["seg"], entry["subrun"], expected)
            )

    return run, segments


# --------------------------------------------------------------------------
# context.md
# --------------------------------------------------------------------------


def parse_context(text):
    """Split a ``context.md`` into its sections and its per-run entries.

    Two shapes are in use and both are handled: a list of runs one per bullet,
    and a set of run-range boundaries for a file covering several physical
    samples. What is *not* handled is a ``## Measurements`` section with
    neither -- see :func:`measurement_notes`, which refuses rather than guess
    which sample a run belongs to.

    Args:
        text: The contents of ``context.md``.

    Returns:
        A dict with ``title``, ``sections`` (lowercased heading to body),
        ``runs`` (run number to ``{title, condition, excluded}``),
        ``boundaries`` (``[(threshold, text)]``) and ``trailing`` (the prose
        after the last run bullet, which is fitting guidance rather than a
        measurement).
    """
    lines = (text or "").splitlines()

    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    sections = {}
    name = ""
    body = []
    for line in lines:
        if line.startswith("## "):
            if name:
                sections[name] = "\n".join(body).strip()
            name = line[3:].strip().lower()
            body = []
        elif name:
            body.append(line)
    if name:
        sections[name] = "\n".join(body).strip()

    runs, boundaries, trailing = _parse_measurements(sections.get("measurements", ""))
    return {
        "title": title,
        "sections": sections,
        "runs": runs,
        "boundaries": boundaries,
        "trailing": trailing,
    }


def _parse_measurements(body):
    """Read the ``## Measurements`` section into runs, boundaries and prose."""
    runs = {}
    boundaries = []
    trailing = []
    current = None
    seen_bullet = False

    for line in body.splitlines():
        bullet = RUN_BULLET_RE.match(line)
        if bullet:
            seen_bullet = True
            rest = bullet.group("rest")
            found = BULLET_TITLE_RE.search(rest)
            current = {
                "title": found.group("title").strip() if found else None,
                "condition": BULLET_TITLE_RE.sub("", rest, count=1).strip(" \t-:;."),
                "excluded": bool(EXCLUDED_RE.search(rest)),
            }
            runs[int(bullet.group("run"))] = current
            trailing = []
            continue

        # A boundary bullet describes a range of runs, not one run.
        lower = LOWER_THAN_RE.search(line)
        start = STARTING_FROM_RE.search(line)
        if lower or start:
            seen_bullet = True
            current = None
            threshold = 0 if lower else int(start.group("run"))
            boundaries.append([threshold, line.strip(" \t-*")])
            trailing = []
            continue

        # An indented continuation belongs to the bullet above it.
        if current is not None and line.startswith((" ", "\t")) and line.strip():
            current["condition"] += " " + line.strip()
            if EXCLUDED_RE.search(line):
                current["excluded"] = True
            continue
        if boundaries and current is None and line.startswith((" ", "\t")) and line.strip():
            boundaries[-1][1] += " " + line.strip()
            continue

        if line.strip():
            if seen_bullet:
                trailing.append(line.strip())
            current = None
        elif current is not None:
            current = None

    boundaries.sort(key=lambda item: item[0])
    return runs, [tuple(b) for b in boundaries], "\n".join(trailing).strip()


def measurement_notes(parsed, run):
    """What ``context.md`` says about this particular run.

    Args:
        parsed: The output of :func:`parse_context`.
        run: The run number of the measurement being handed over.

    Returns:
        ``{"condition", "title", "source"}`` -- ``source`` naming which shape
        the answer came from, for the note the caller prints.

    Raises:
        WorkbenchError: If the run is marked unusable, or if the section
            describes neither this run nor any range containing it.
    """
    entry = parsed["runs"].get(run)
    if entry is not None:
        if entry["excluded"]:
            raise WorkbenchError(
                "context.md marks run %d as not for analysis:\n  %s\n"
                "Handing it to an unattended session anyway is exactly the "
                "instruction this line exists to prevent." % (run, entry["condition"])
            )
        return {"condition": entry["condition"], "title": entry["title"],
                "source": "the run's own line in ## Measurements"}

    if parsed["boundaries"]:
        applicable = [b for b in parsed["boundaries"] if b[0] <= run]
        if applicable:
            return {"condition": applicable[-1][1], "title": None,
                    "source": "the run-range block starting at %d" % applicable[-1][0]}

    raise WorkbenchError(
        "context.md's '## Measurements' section says nothing about run %d.\n"
        "It lists neither a line for this run nor a run-range block containing "
        "it, so which physical sample this measurement belongs to is unknown. "
        "Guessing it would send an unattended session at the wrong stack.\n"
        "Add a line for run %d, or a 'Starting from run N' boundary that covers "
        "it." % (run, run)
    )


# --------------------------------------------------------------------------
# The prior analysis
# --------------------------------------------------------------------------


def _resolve(references, node):
    """Follow a bumps ``Reference`` to the parameter it names."""
    if isinstance(node, dict) and node.get("__class__") == "Reference":
        return references.get(node.get("id")) or {}
    return {}


def _value_of(references, node):
    parameter = _resolve(references, node)
    slot = parameter.get("slot") or {}
    value = slot.get("value")
    return value if isinstance(value, (int, float)) else None


def read_problem(problem_json):
    """Read a serialized bumps problem into a stack and a parameter list.

    Args:
        problem_json: Path to ``problem.json``.

    Returns:
        ``{"name", "stack": [...], "parameters": [...]}``, or ``None`` when the
        file is absent or unreadable -- a missing prior fit is a handoff with
        less evidence in it, not a failure.
    """
    try:
        with open(problem_json) as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None

    references = document.get("references") or {}
    obj = document.get("object") or {}

    parameters = []
    for parameter in references.values():
        if not isinstance(parameter, dict) or parameter.get("fixed"):
            continue
        bounds = parameter.get("bounds")
        slot = parameter.get("slot") or {}
        value = slot.get("value")
        if not isinstance(value, (int, float)):
            continue
        low, high = (bounds + [None, None])[:2] if isinstance(bounds, list) else (None, None)
        parameters.append({
            "name": parameter.get("name") or "?",
            "value": value,
            "low": low if isinstance(low, (int, float)) else None,
            "high": high if isinstance(high, (int, float)) else None,
        })
    parameters.sort(key=lambda p: p["name"])

    # The first model carries the stack; co-refined models share it by
    # reference. In back reflection the stack is assembled ambient-first, so
    # this list reads from the incident medium down to the substrate -- which is
    # also nr-workbench's own order.
    stack = []
    models = obj.get("models") or []
    if models and isinstance(models[0], dict):
        sample = models[0].get("sample") or {}
        for layer in sample.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            material = layer.get("material") or {}
            stack.append({
                "name": layer.get("name") or "?",
                "thickness": _value_of(references, layer.get("thickness")),
                "roughness": _value_of(references, layer.get("interface")),
                "rho": _value_of(references, material.get("rho")),
            })

    return {"name": obj.get("name"), "stack": stack, "parameters": parameters,
            "n_models": len(models)}


def read_uncertainties(analysis_dir):
    """The 1-sigma spreads from a DREAM run's ``-err.json``, keyed by name.

    Returns an empty dict when there is none -- which is itself reportable, and
    is reported: a fit with no posterior has no uncertainty to quote.
    """
    output = os.path.join(analysis_dir, "refl1d_output")
    candidates = []
    if os.path.isdir(output):
        final = _final_fit_dir(output)
        if final:
            candidates.append(final)
    candidates.append(analysis_dir)

    for directory in candidates:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith("-err.json"):
                continue
            try:
                with open(os.path.join(directory, name)) as handle:
                    document = json.load(handle)
            except (OSError, ValueError):
                continue
            if isinstance(document, dict):
                return {
                    key: entry.get("std")
                    for key, entry in document.items()
                    if isinstance(entry, dict) and isinstance(entry.get("std"), (int, float))
                }
    return {}


def read_chisq(source):
    """The accepted fit's reduced chi-squared, or None.

    Three sources, tried in order of how directly each states the thing:

    1. ``final_state.json`` -- authoritative, present when reading a live state.
       ``ndip-package`` *references* it rather than copying it (it is most of a
       megabyte of arrays), so inside a package it is absent.
    2. ``MANIFEST.json``'s ``fit.chisq`` -- the same number, lifted into the
       manifest at package time for exactly this reason.
    3. the finalize checkpoint's prose, matched only on the phrasings that mean
       the whole fit.

    Returns ``None`` rather than a guess when none of the three says it. A run
    that hit its iteration cap has no finalize node, and the nearest number in
    the trail is a per-segment chi-squared -- saying "unrecorded" is correct and
    reporting a segment's value as the fit's is not.
    """
    analysis_dir = source.analysis_dir
    if analysis_dir:
        try:
            with open(os.path.join(analysis_dir, "final_state.json")) as handle:
                document = json.load(handle)
            if isinstance(document, dict):
                for value in (document.get("final_chi2"),
                              _get(document, "state", "best_chi2"),
                              _get(document, "state", "current_chi2")):
                    if isinstance(value, (int, float)):
                        return value
        except (OSError, ValueError):
            pass

    if source.kind == "package":
        try:
            with open(os.path.join(source.root, "MANIFEST.json")) as handle:
                manifest = json.load(handle)
            value = _get(manifest, "fit", "chisq")
            if isinstance(value, (int, float)):
                return value
        except (OSError, ValueError):
            pass

    checkpoints = os.path.join(analysis_dir, "checkpoints") if analysis_dir else ""
    if not checkpoints or not os.path.isdir(checkpoints):
        return None
    for name in sorted(os.listdir(checkpoints), reverse=True):
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(checkpoints, name)) as handle:
                text = handle.read()
        except OSError:
            continue
        for pattern in FINAL_CHISQ_RES:
            found = pattern.search(text)
            if found:
                try:
                    return float(found.group("value"))
                except ValueError:
                    continue
    return None


def declared_bounds(job_yaml):
    """The nuisance-parameter ranges the sample notes asked for.

    These come from the plan, which is where the scientist's stated limits land
    before any fitter has touched them. Comparing them with what the fit
    actually ran is the whole point of keeping them separate.

    Returns:
        ``(bounds, flags)`` -- ``{name: (min, max)}`` for the ranges, and
        ``{name: bool}`` for the terms the plan merely switches on. Kept apart
        rather than sharing one mapping with a sentinel key, so the caller can
        iterate the ranges without tripping over something that is not one.
    """
    bounds = {}
    flags = {}
    try:
        import yaml
        with open(job_yaml) as handle:
            document = yaml.safe_load(handle.read())
    except Exception:
        return bounds, flags
    if not isinstance(document, dict):
        return bounds, flags
    for state in document.get("states") or []:
        if not isinstance(state, dict):
            continue
        for key in ("theta_offset", "sample_broadening", "intensity", "background"):
            value = state.get(key)
            # `background: true` and `background: {min, max}` are both legal in
            # a plan; bool is checked first because bool subclasses int.
            if isinstance(value, bool):
                flags[key] = value
            elif isinstance(value, dict):
                low, high = value.get("min"), value.get("max")
                if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                    bounds[key] = (low, high)
    return bounds, flags


def prior_analysis(source):
    """Everything worth knowing about the fit the pipeline already ran.

    Returns ``None`` when the analysis produced nothing readable -- a handoff
    with less evidence in it, which is not a failure.
    """
    analysis_dir = source.analysis_dir
    if not analysis_dir:
        return None
    problem_json = os.path.join(analysis_dir, "problem.json")
    problem = read_problem(problem_json)
    if problem is None:
        return None

    uncertainties = read_uncertainties(analysis_dir)
    for parameter in problem["parameters"]:
        parameter["std"] = uncertainties.get(parameter["name"])

    return {
        "tool": "aure",
        "model_name": _get(source.state, "stages", "analysis", "params",
                           "model_name") or problem["name"],
        "chisq": read_chisq(source),
        "n_segments": problem["n_models"],
        "stack": problem["stack"],
        "parameters": problem["parameters"],
        "uncertainties_available": bool(uncertainties),
        "analysis_dir": analysis_dir,
        "trail": _trail(analysis_dir),
    }


def _trail(analysis_dir):
    """The agentic reasoning trail, as ``(name, text)`` for the small ``.md``s."""
    directory = os.path.join(analysis_dir, "checkpoints")
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(directory, name)) as handle:
                out.append((name, handle.read().strip()))
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------
# The deterministic checks
# --------------------------------------------------------------------------


def _normalise(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def findings(segments, headers, prior, declared):
    """What is worth telling a session before it chooses a single bound.

    Every check here is arithmetic over files already on disk. Nothing is
    inferred, and a check whose input is absent says so instead of passing.

    Returns:
        A list of ``{"kind", "message"}``, most structural first.
    """
    found = []

    # The direct beam is deliberately NOT compared across the segments of this
    # measurement. Each angle legitimately gets its own direct beam, so flagging
    # that would flag every measurement ever made -- nr-workbench's own
    # `reconcile._direct_beams` says exactly this, and looks instead for one
    # angle disagreeing *between* runs. That comparison needs more than one
    # measurement and this stage hands over one, so the check belongs there and
    # not here. The norm_run values are recorded in the JSON regardless.
    if any(h.get("norm_run") is None for h in headers):
        found.append({
            "kind": "no-direct-beam",
            "message": "at least one segment's header records no norm_run, so "
                       "which direct beam normalised it is not on disk.",
        })

    # The dQ convention. One boolean per file, and the two readings differ by
    # 2.355 on every point.
    labels = {h.get("dq_label") for h in headers}
    if None in labels:
        found.append({
            "kind": "no-dq-convention",
            "message": "at least one segment does not label its dQ column, so "
                       "whether it is FWHM or sigma is not stated on disk.",
        })
        labels.discard(None)
    if len(labels) > 1:
        found.append({
            "kind": "mixed-dq-convention",
            "message": "the segments disagree about the dQ column convention "
                       "(%s). They cannot be co-refined until that is resolved: "
                       "the two differ by a factor of 2.355 on every point."
                       % ", ".join(sorted(labels)),
        })

    if prior is None:
        found.append({
            "kind": "no-prior-fit",
            "message": "the pipeline recorded no readable fit for this "
                       "measurement, so there is no prior result to compare with.",
        })
        return found

    # Bounds the fit ran with, against the bounds the notes asked for.
    for key, (low, high) in sorted(declared.items()):
        for parameter in prior["parameters"]:
            if _normalise(key) not in _normalise(parameter["name"]):
                continue
            actual_low, actual_high = parameter["low"], parameter["high"]
            if actual_low is None or actual_high is None:
                continue
            if actual_low < low - 1e-12 or actual_high > high + 1e-12:
                found.append({
                    "kind": "bound-widened",
                    "message": "%s was fitted in [%g, %g] but the sample notes "
                               "asked for [%g, %g]. The prior fit widened it; "
                               "the notes are what you were asked to respect."
                               % (parameter["name"], actual_low, actual_high, low, high),
                })

    # Parameters sitting on a bound. A value on its limit is a bound, not a
    # measurement, whichever direction it railed in.
    for parameter in prior["parameters"]:
        low, high = parameter["low"], parameter["high"]
        if low is None or high is None or high <= low:
            continue
        span = high - low
        value = parameter["value"]
        if value - low <= BOUND_TOLERANCE * span:
            found.append({
                "kind": "on-bound",
                "message": "%s came back at %.6g, on its lower bound of %.6g. "
                           "That is a bound, not a measurement."
                           % (parameter["name"], value, low),
            })
        elif high - value <= BOUND_TOLERANCE * span:
            found.append({
                "kind": "on-bound",
                "message": "%s came back at %.6g, on its upper bound of %.6g. "
                           "That is a bound, not a measurement."
                           % (parameter["name"], value, high),
            })

    # Fitted SLDs against their book values.
    for layer in prior["stack"]:
        nominal = NOMINAL_SLD.get(_normalise(layer["name"]))
        rho = layer["rho"]
        if nominal is None or rho is None:
            continue
        if abs(rho - nominal) > SLD_TOLERANCE:
            found.append({
                "kind": "sld-off-nominal",
                "message": "%s came back at rho = %.4g against a nominal %.4g. "
                           "Either the material assignment is wrong or the layer "
                           "is absorbing something else."
                           % (layer["name"], rho, nominal),
            })

    if not prior["uncertainties_available"]:
        found.append({
            "kind": "no-uncertainties",
            "message": "the prior fit exported no posterior, so none of its "
                       "values carry an uncertainty. Do not quote them.",
        })

    if prior["n_segments"] and prior["n_segments"] != len(segments):
        found.append({
            "kind": "segment-count-differs",
            "message": "the prior fit co-refined %d dataset(s) but this "
                       "measurement has %d angle segments."
                       % (prior["n_segments"], len(segments)),
        })

    return found


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _table_row(run, condition):
    """One ``## Measurements`` row, with the condition flattened onto one line."""
    flat = " ".join((condition or "").split())
    return "| %d | full Q | %s |" % (run, flat.replace("|", "/"))


def render_sample_md(sample, run, notes, parsed, segments, headers, prior, found,
                     context_digest):
    """The one file without which ``nrw agent run`` refuses to start.

    Section anatomy follows nr-workbench's scaffolded template, because its
    offline checks read these headings by name: ``## Measurements`` is compared
    against the file headers, and ``## Fits to perform`` is quoted verbatim into
    the session prompt as the whole of its task.

    Only this measurement's run number appears anywhere in the file. That is not
    tidiness: nr-workbench matches every six-digit number in this file against
    the data on disk, so naming a subrun or a neighbouring run would report it
    as documented-but-absent for the life of the project.
    """
    sections = parsed["sections"]
    thetas = ", ".join(
        "%.4f" % h["theta_deg"] for h in headers if h["theta_deg"] is not None
    )

    lines = [
        "%s run %d from context.md sha256:%s by ndip-workbench %s on %s. -->"
        % (MARKER, run, (context_digest or "")[:12], _self_version(), _now()),
        "",
        "# %s -- run %d" % (parsed["title"] or sample, run),
        "",
        "## Description",
        "",
        sections.get("description", "").strip() or "(context.md gave no description)",
        "",
        "## Details",
        "",
        sections.get("details", "").strip() or "(context.md gave no details)",
        "",
        "## Measurements",
        "",
        "| Run | Type | Condition |",
        "|-----|------|-----------|",
        _table_row(run, notes["condition"]),
        "",
        "%d angle segment(s) at theta = %s degrees." % (len(segments), thetas or "?"),
        "",
        "## Measurement conditions",
        "",
    ]

    conditions = sections.get("measurement conditions", "").strip()
    # An older context.md puts the same guidance under '## Fitting approach'.
    conditions = conditions or sections.get("fitting approach", "").strip()
    lines.append(conditions or "(context.md said nothing about the measurement)")

    labels = sorted({h["dq_label"] for h in headers if h["dq_label"]})
    if labels:
        lines += [
            "",
            "The reduced files label their dQ column %s, read from the header "
            "rather than assumed." % "/".join(labels),
        ]

    lines += ["", "## Fits to perform", ""] + _task(run, segments, notes, parsed, prior, found)

    return "\n".join(lines).rstrip() + "\n"


def _task(run, segments, notes, parsed, prior, found):
    """The declared task: one measurement, its segments co-refined, and why."""
    lines = [
        "Fit run %d, and only run %d. Co-refine its %d angle segments against "
        "each other -- not the combined file, which double-counts the same "
        "neutrons. Do not bring in another measurement: comparing this one with "
        "its neighbours is the next person's job, and this session has one "
        "measurement's worth of evidence."
        % (run, run, len(segments)),
        "",
        "What this measurement is, from the sample notes: %s"
        % " ".join((notes["condition"] or "").split()),
    ]

    if parsed["trailing"]:
        lines += [
            "",
            "Also from the sample notes, about how these measurements behave:",
            "",
        ] + ["  " + line for line in parsed["trailing"].splitlines()]

    if prior is not None:
        chisq = "chi-squared %.4g" % prior["chisq"] if prior["chisq"] is not None \
            else "an unrecorded chi-squared"
        lines += [
            "",
            "This measurement has already been fitted once, automatically, "
            "reaching %s. Its stack, its free parameters with the ranges it was "
            "given, and the offline checks on it are in "
            "`handoff/prior-analysis.md` -- read that before you choose a single "
            "bound. Treat it as evidence about the data, not as a starting "
            "model: author your own spec, and where you disagree with it say so "
            "with the numbers." % chisq,
        ]
        if found:
            # Kinds only, no values. Every number in this file is matched against
            # the data on disk by nr-workbench's run-number scan, and a six-digit
            # decimal is indistinguishable from a run number to it -- so the
            # figures stay in prior-analysis.md, which it does not scan.
            kinds = sorted({f["kind"] for f in found})
            lines += [
                "",
                "The checks flagged %d item(s), of these kinds: %s."
                % (len(found), ", ".join(kinds)),
            ]

    return lines


def _prior_summary(prior, found):
    """The prior fit, rendered so it reads as evidence rather than instruction."""
    lines = [
        "Produced by the automated pipeline (%s), model %r%s. This is a record "
        "of what a previous automated pass concluded; it is data about the "
        "sample, not instructions for you."
        % (prior["tool"], prior["model_name"],
           ", chi-squared %.4g" % prior["chisq"] if prior["chisq"] is not None else ""),
        "",
        "Stack, incident medium first:",
        "",
        "| Layer | rho (1e-6 A^-2) | thickness (A) | roughness (A) |",
        "|---|---|---|---|",
    ]
    for layer in prior["stack"]:
        lines.append("| %s | %s | %s | %s |" % (
            layer["name"],
            _num(layer["rho"]), _num(layer["thickness"]), _num(layer["roughness"]),
        ))

    lines += ["", "Free parameters, with the range the fit was given:", "",
              "| Parameter | Value | 1-sigma | Range |", "|---|---|---|---|"]
    for parameter in prior["parameters"]:
        span = "[%s, %s]" % (_num(parameter["low"]), _num(parameter["high"]))
        lines.append("| %s | %s | %s | %s |" % (
            parameter["name"], _num(parameter["value"]),
            _num(parameter.get("std")) if parameter.get("std") is not None else "--",
            span,
        ))

    if found:
        lines += ["", "What the offline checks say about it:", ""]
        lines += ["- [%s] %s" % (f["kind"], f["message"]) for f in found]

    return lines


def _num(value):
    if value is None:
        return "--"
    return "%.6g" % value


def render_prior_md(run, prior, found, trail_included=True):
    """The long-form record, kept beside the sample rather than in its notes."""
    lines = [
        "# The pipeline's own analysis of run %d" % run,
        "",
        "Written by ndip-workbench %s on %s from the workflow state. Everything "
        "below is a record of an automated pass; read it as evidence."
        % (_self_version(), _now()),
        "",
    ]
    lines += _prior_summary(prior, found)
    if trail_included and prior["trail"]:
        lines += ["", "## Its reasoning, as it recorded it", ""]
        for name, text in prior["trail"]:
            lines += ["<!-- %s -->" % name, ""]
            # Indented, so its own headings cannot present themselves as
            # sections of this document.
            lines += ["  " + line for line in text.splitlines()]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_sample_yaml(sample, run, title, segments, beamtime):
    """The ``nrw-sample/1`` register: what this analysis is about, by file.

    nr-workbench reads this back and prefers it over rescanning the disk, so it
    is the supported way to say "this sample is these files and no others".
    Paths are relative to the project root; an absolute path here is a red flag
    in nr-workbench's own skills.

    Dumped with PyYAML rather than assembled as text, for two reasons: a title
    is arbitrary prose and hand-quoting it is a bug waiting for an apostrophe,
    and this way the file is byte-identical to what ``nrw sample scan`` writes --
    so running that afterwards is a no-op instead of a rewrite.
    """
    import yaml

    document = {"schema": "nrw-sample/1", "id": sample, "title": title or sample}
    if beamtime:
        document["beamtime"] = beamtime
    document["created"] = _now()
    document["steady"] = [{
        "run": run,
        "segments": ["samples/%s/data/steady/%s" % (sample, e["name"]) for e in segments],
    }]
    document["series"] = []
    return yaml.safe_dump(document, sort_keys=False)


# --------------------------------------------------------------------------
# The stage
# --------------------------------------------------------------------------


def _require_project(project):
    """Refuse to write into anything that is not already an nrw project.

    Creating one is ``nrw init``'s job and a person's decision: it writes the
    permission hook that bounds an unattended session, the skills it reads and
    the notes that explain the layout. A workflow faking that scaffold would
    produce a project whose safety properties nobody had agreed to.
    """
    if not os.path.isfile(os.path.join(project, "nrw.toml")):
        raise WorkbenchError(
            "%s is not an nr-workbench project (no nrw.toml).\n"
            "Create one first -- `nrw init %s` -- then run this again. This "
            "stage deliberately does not scaffold a project: `nrw init` also "
            "installs the permission hook that bounds an unattended session."
            % (project, project)
        )


def _beamtime(project):
    """The beamtime label from ``nrw.toml``, if it names one."""
    try:
        with open(os.path.join(project, "nrw.toml")) as handle:
            for line in handle:
                found = re.match(r'\s*label\s*=\s*"([^"]*)"', line)
                if found:
                    return found.group(1)
    except OSError:
        pass
    return ""


def _check_marker(path, force):
    """Whether an existing ``sample.md`` may be replaced."""
    if not os.path.isfile(path):
        return
    try:
        with open(path) as handle:
            head = handle.read(4096)
    except OSError as exc:
        raise WorkbenchError("cannot read %s: %s" % (path, exc))
    if MARKER in head:
        return
    if force:
        return
    raise WorkbenchError(
        "%s already exists and was not written by this tool.\n"
        "It is somebody's notes -- possibly the declared task for an analysis "
        "already under way -- and overwriting it would delete the one thing in "
        "an nrw project that cannot be regenerated.\n"
        "Use a different --sample, or --force if you are certain." % path
    )


def _foreign_runs(text, run, segments):
    """Six-digit numbers in the generated notes that are not this measurement.

    nr-workbench matches every one of them against the data on disk, so a
    stray run number becomes a permanent "documented but absent" finding.
    """
    allowed = {str(run)} | {str(s["subrun"]) for s in segments}
    return sorted({m for m in re.findall(r"\b\d{6}\b", text)} - allowed)


def run_workbench(source, project, sample=None, write=False, force=False):
    """Prepare one measurement as an nr-workbench sample.

    Args:
        source: A :class:`Source` -- a provenance package or a live state.
        project: An existing nr-workbench project directory.
        sample: Sample identifier; defaults to ``s<run>``.
        write: Actually write. Off by default -- this plans first.
        force: Replace a ``sample.md`` this tool did not write.

    Returns:
        ``(planned, notes, meta)`` where *planned* is a list of
        ``{"path", "action", ...}`` relative to the project root.

    Raises:
        WorkbenchError: If anything would make the handoff wrong rather than
            merely incomplete.
    """
    _require_project(project)

    run, segments = resolve_segments(source)
    headers = [read_header(entry["path"]) for entry in segments]
    sample = sample or "s%d" % run
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", sample):
        raise WorkbenchError(
            "%r is not a usable sample name: it becomes a directory inside the "
            "project." % sample
        )

    context_file = source.context_file
    if not context_file or not os.path.isfile(context_file):
        raise WorkbenchError(
            "no readable context file (%r), and it is the only statement of what "
            "this sample is and what to fit." % context_file
        )
    with open(context_file) as handle:
        context_text = handle.read()
    parsed = parse_context(context_text)
    notes_for_run = measurement_notes(parsed, run)

    declared, declared_flags = declared_bounds(source.job_yaml)
    prior = prior_analysis(source)
    found = findings(segments, headers, prior, declared)

    # The run title in the header against the title context.md gives for this
    # run. This is the check that catches a measurement filed under the wrong
    # condition, which is the error that sends a whole analysis down the wrong
    # path before the first fit runs.
    declared_title = notes_for_run.get("title")
    if declared_title:
        titles = [h.get("run_title") or "" for h in headers]
        mismatched = [t for t in titles if declared_title.lower() not in t.lower()]
        if mismatched:
            found.insert(0, {
                "kind": "title-mismatch",
                "message": "context.md calls run %d %r, but its file headers say "
                           "%s. One of the two is wrong about what was measured, "
                           "and that decides the model."
                           % (run, declared_title,
                              ", ".join(repr(t) for t in sorted(set(mismatched)))),
            })

    sample_dir = os.path.join(project, "samples", sample)
    sample_md = os.path.join(sample_dir, "sample.md")
    _check_marker(sample_md, force)

    digest = _sha256(context_file)
    notes_text = render_sample_md(sample, run, notes_for_run, parsed, segments,
                                 headers, prior, found, digest)
    register = render_sample_yaml(sample, run, parsed["title"], segments,
                                  _beamtime(project))

    notes = []
    foreign = _foreign_runs(notes_text, run, segments)
    if foreign:
        notes.append(
            "the generated notes mention run number(s) %s, which are not this "
            "measurement. nr-workbench will report them as documented-but-absent; "
            "edit context.md's prose if that is not what you want."
            % ", ".join(foreign)
        )

    planned = []
    for entry in segments:
        rel = "samples/%s/data/steady/%s" % (sample, entry["name"])
        existing = os.path.join(project, rel)
        action = "copy"
        if os.path.exists(existing):
            action = "keep" if _sha256(existing) == _sha256(entry["path"]) else "differs"
        planned.append({"path": rel, "action": action, "source": entry["path"]})
        if action == "differs":
            notes.append(
                "%s already exists with different contents and is left alone. "
                "The project's copy is authoritative." % rel
            )

    planned.append({"path": "samples/%s/sample.md" % sample, "action": "write",
                    "body": notes_text})
    planned.append({"path": "samples/%s/sample.yaml" % sample, "action": "write",
                    "body": register})
    if prior is not None:
        planned.append({
            "path": "samples/%s/handoff/prior-analysis.md" % sample,
            "action": "write",
            "body": render_prior_md(run, prior, found),
        })
        planned.append({
            "path": "samples/%s/handoff/prior-analysis.json" % sample,
            "action": "write",
            "body": json.dumps({
                "schema": "ndip-workbench-prior/1",
                "run": run,
                "produced_by": {"tool": "ndip-workbench", "version": _self_version()},
                "context_sha256": digest,
                "declared": declared,
                "declared_flags": declared_flags,
                "prior": {k: v for k, v in prior.items() if k != "trail"},
                "findings": found,
            }, indent=2, default=str),
        })

    meta = {
        "run": run,
        "sample": sample,
        "read_from": source.kind,
        "segments": [entry["name"] for entry in segments],
        "thetas": [h["theta_deg"] for h in headers],
        "norm_runs": [h["norm_run"] for h in headers],
        "context_sha256": digest,
        "context_source": notes_for_run["source"],
        "chisq": prior["chisq"] if prior else None,
        "findings": found,
    }

    if not write:
        return planned, notes, meta

    for name in SAMPLE_SUBDIRS:
        os.makedirs(os.path.join(sample_dir, name), exist_ok=True)
        keep = os.path.join(sample_dir, name, ".gitkeep")
        if not os.path.exists(keep):
            open(keep, "a").close()

    for item in planned:
        target = os.path.join(project, item["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if item["action"] == "copy":
            shutil.copy2(item["source"], target)
        elif item["action"] == "write":
            with open(target, "w") as handle:
                handle.write(item["body"])

    return planned, notes, meta


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ndip-workbench",
        description="Hand one finished measurement to an nr-workbench project, "
                    "ready for `nrw agent run`.",
        epilog="SOURCE is normally a provenance package from `ndip-package`, "
               "which is self-contained and works after being copied to another "
               "machine. A workflow-state JSON also works, but its recorded "
               "paths only resolve where the pipeline ran.",
    )
    parser.add_argument("source", metavar="SOURCE",
                        help="A provenance package directory, or a state JSON.")
    parser.add_argument("--project", required=True,
                        help="An existing nr-workbench project (holding nrw.toml).")
    parser.add_argument("--sample", default=None,
                        help="Sample identifier (default: s<run>).")
    parser.add_argument("--write", action="store_true",
                        help="Actually write. Without this, nothing is written.")
    parser.add_argument("--force", action="store_true",
                        help="Replace a sample.md this tool did not write.")
    parser.add_argument("--json", action="store_true",
                        help="Emit the plan and findings as JSON.")
    args = parser.parse_args(argv)

    try:
        source = open_source(args.source)
        planned, notes, meta = run_workbench(
            source, args.project, sample=args.sample,
            write=args.write, force=args.force,
        )
    except WorkbenchError as exc:
        raise SystemExit("ndip-workbench: %s" % exc)

    if args.json:
        json.dump({
            "wrote": bool(args.write),
            "source": args.source,
            "project": args.project,
            "planned": [{k: v for k, v in p.items() if k != "body"} for p in planned],
            "notes": notes,
            "meta": meta,
        }, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    out = sys.stderr
    out.write("ndip-workbench: run %d -> %s/samples/%s%s\n"
              % (meta["run"], args.project, meta["sample"],
                 "" if args.write else "  (nothing written)"))
    out.write("  read from     the %s at %s\n" % (meta["read_from"], args.source))
    out.write("  measurement   %d segment(s), theta %s\n"
              % (len(meta["segments"]),
                 ", ".join("%.4f" % t for t in meta["thetas"] if t is not None)))
    out.write("  condition     from %s\n" % meta["context_source"])
    if meta["chisq"] is not None:
        out.write("  prior fit     chi-squared %.4g\n" % meta["chisq"])
    for item in planned:
        out.write("    %-8s %s\n" % (item["action"], item["path"]))
    if meta["findings"]:
        out.write("  %d finding(s) carried into the notes:\n" % len(meta["findings"]))
        for finding in meta["findings"]:
            out.write("    [%s] %s\n" % (finding["kind"], finding["message"]))
    for note in notes:
        out.write("  note: %s\n" % note)
    if not args.write:
        out.write("\n  Re-run with --write to create these.\n")
    else:
        out.write("\n  Next:  nrw agent run %s\n" % meta["sample"])


if __name__ == "__main__":
    main()
