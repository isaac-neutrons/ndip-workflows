"""
Bootstrap a workflow-state JSON for the ISAAC reflectivity pipeline.

``seed-config`` has three modes, picked by how you invoke it:

  Default — from a run's event NeXus file (all stages start pending)::

      seed-config EVENT_FILE SEED_FILE -o state.json

    Run identity (run, instrument, ipts) is read from the **contents** of the
    event file with h5py, not its filename: Galaxy stages uploaded datasets
    under opaque names like ``dataset_<uuid>.dat``, so the basename cannot be
    trusted. From that metadata the canonical analysis paths (nexus_file,
    data_directory, ipts_shared_root) are reconstructed under the facility
    root (``/SNS`` by default), and relative seed paths resolve against the
    reconstructed IPTS shared root.

  ``--from-reduced REDUCED_FILE`` — start past reduction, from an
  already-reduced dataset (the partial file plan-data consumes)::

      seed-config SEED_FILE --from-reduced REFL_..._partial.txt -o state.json

    ``stages.reduction`` is pre-filled (status ``ok``, ``artifacts.partial_file``
    set to REDUCED_FILE) so the analyzer runs plan-data directly.

  ``--from-plan PLAN_FILE`` — start past planning, from an existing plan (the
  job YAML plan-data emits)::

      seed-config SEED_FILE --from-plan job_<id>.yaml -o state.json

    ``stages.analysis.artifacts.job_yaml`` is set to PLAN_FILE so the analyze
    step (analyze-sample / aure analyze) runs without re-planning. The stage
    stays ``pending`` because the fit itself has not run yet.

The seed carries only what the chosen mode can't derive:

  Default required:      template_file, output_directory, context_file, sequence_total
  --from-reduced needs:  output_directory, context_file
  --from-plan needs:     output_directory
  Optional everywhere:   prompt, run, instrument, ipts,
                         llm_provider, llm_model, llm_base_url

The two ``--from-*`` modes are local-first: there is no event file, so run /
instrument / ipts are optional *metadata* (recorded as-is for the eventual
ISAAC record — no canonical /SNS paths are fabricated from them), relative
seed paths resolve against the current directory, and absolute paths pass
through. ``--facility-root`` applies to the default (event-file) mode only.
"""

import json
import os
from pathlib import Path

import click
import yaml

from ndip_state.state import build_state


# Seed keys each mode insists on. The default (event-file) mode needs all four;
# the --from-* modes start further down the pipeline and need less.
_REQUIRED_KEYS = ("template_file", "output_directory", "context_file", "sequence_total")
_REQUIRED_FROM_REDUCED = ("output_directory", "context_file")
_REQUIRED_FROM_PLAN = ("output_directory",)

_DEFAULT_LLM = {
    "provider": "local",
    "model": "gpt-4",
    "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/",
}

# Mounted facility data root. The canonical run paths are reconstructed as
# <root>/<INSTRUMENT>/<IPTS>/{nexus,shared}/...; override with --facility-root.
DEFAULT_FACILITY_ROOT = "/SNS"


def _load_seed(seed_path: str) -> dict:
    """Read seed file as JSON first, fall back to YAML."""
    text = Path(seed_path).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise click.ClickException(
                f"Could not parse seed file as JSON or YAML: {exc}"
            ) from exc
    if not isinstance(data, dict):
        raise click.ClickException("Seed file must be a JSON object / YAML mapping.")
    return data


def _read_nexus_metadata(event_file: str) -> dict:
    """Read ``run``, ``instrument`` and ``ipts`` from a NeXus file's contents.

    Reads ``/entry/run_number``, ``/entry/instrument/name`` and
    ``/entry/experiment_identifier`` with h5py. We deliberately ignore the
    filename: Galaxy stages uploaded datasets as ``dataset_<uuid>.dat``, so the
    basename carries no run identity.
    """
    import h5py
    import numpy as np

    def _value(group, name):
        ds = group.get(name)
        if ds is None:
            return None
        val = ds[()]
        if isinstance(val, np.ndarray):
            if val.size == 0:
                return None
            val = val.reshape(-1)[0]
        if isinstance(val, (bytes, bytearray)):  # np.bytes_ is a bytes subclass
            return val.decode("utf-8", "replace").strip()
        return str(val).strip()

    try:
        h5 = h5py.File(event_file, "r")
    except OSError as exc:
        raise click.UsageError(
            f"could not open event_file as a NeXus/HDF5 file: {event_file} ({exc})"
        ) from exc

    with h5:
        entry = None
        for key in h5:
            obj = h5[key]
            if not isinstance(obj, h5py.Group):
                continue
            nxclass = obj.attrs.get("NX_class")
            if isinstance(nxclass, (bytes, bytearray)):
                nxclass = nxclass.decode("utf-8", "replace")
            if nxclass == "NXentry" or key == "entry":
                entry = obj
                break
        if entry is None:
            raise click.UsageError(f"no NXentry group found in {event_file}")

        run = _value(entry, "run_number") or _value(entry, "entry_identifier")
        instrument = None
        inst = entry.get("instrument")
        if isinstance(inst, h5py.Group):
            instrument = _value(inst, "name")
            if not instrument and "name" in inst:
                short = inst["name"].attrs.get("short_name")
                if isinstance(short, (bytes, bytearray)):
                    instrument = short.decode("utf-8", "replace").strip()
        ipts = _value(entry, "experiment_identifier")

    if not run or not str(run).isdigit():
        raise click.UsageError(
            f"could not read a numeric /entry/run_number from {event_file}"
        )
    if not instrument:
        raise click.UsageError(
            f"could not read /entry/instrument/name from {event_file}"
        )
    if not ipts:
        raise click.UsageError(
            f"could not read /entry/experiment_identifier (IPTS) from {event_file}"
        )
    return {"run": int(run), "instrument": instrument, "ipts": ipts}


def _reconstruct_paths(meta: dict, facility_root: str) -> dict:
    """Build the canonical analysis paths from run metadata + facility root.

    Standard layout: ``<root>/<INSTRUMENT>/<IPTS>/nexus/<INSTRUMENT>_<RUN>.nxs.h5``
    for the event file, with the ``shared`` tree alongside ``nexus``.
    """
    run, instrument, ipts = meta["run"], meta["instrument"], meta["ipts"]
    ipts_root = os.path.join(facility_root, instrument, ipts)
    return {
        "run": run,
        "instrument": instrument,
        "ipts": ipts,
        "data_directory": os.path.join(ipts_root, "nexus"),
        "event_file": os.path.join(ipts_root, "nexus", f"{instrument}_{run}.nxs.h5"),
        "ipts_shared_root": os.path.join(ipts_root, "shared"),
    }


def _resolve_path(value: str, root: str) -> str:
    """Resolve a seed path: absolute → as-is; relative → joined to *root*."""
    p = Path(value)
    if p.is_absolute():
        return str(p)
    return str(Path(root) / value)


def _require(seed: dict, keys) -> None:
    """Raise a UsageError listing any of *keys* missing from *seed*."""
    missing = [k for k in keys if k not in seed]
    if missing:
        raise click.UsageError(
            f"seed is missing required key(s): {', '.join(missing)}"
        )


def _build_state(event_file: str, seed: dict, facility_root: str) -> dict:
    """Validate inputs and build the state document."""
    _require(seed, _REQUIRED_KEYS)

    derived = _reconstruct_paths(_read_nexus_metadata(event_file), facility_root)
    root = derived["ipts_shared_root"]

    template_path = _resolve_path(str(seed["template_file"]), root)
    context_path = _resolve_path(str(seed["context_file"]), root)
    output_dir = _resolve_path(str(seed["output_directory"]), root)

    if not Path(template_path).is_file():
        raise click.UsageError(f"template_file does not exist: {template_path}")
    if not Path(context_path).is_file():
        raise click.UsageError(f"context_file does not exist: {context_path}")

    flat: dict = {
        # workflow identity
        "run": derived["run"],
        "instrument": derived["instrument"],
        "ipts": derived["ipts"],
        # operator inputs
        "sequence_total": int(seed["sequence_total"]),
        "template_file": template_path,
        "context_file": context_path,
        "output_directory": output_dir,
        "llm_provider": seed.get("llm_provider", _DEFAULT_LLM["provider"]),
        "llm_model": seed.get("llm_model", _DEFAULT_LLM["model"]),
        "llm_base_url": seed.get("llm_base_url", _DEFAULT_LLM["base_url"]),
        # derived
        "event_file": derived["event_file"],         # -> inputs.derived.nexus_file
        "data_directory": derived["data_directory"],
        "ipts_shared_root": root,
    }
    if seed.get("prompt"):
        flat["prompt"] = seed["prompt"]

    return build_state(flat)


def _common_flat(seed: dict) -> dict:
    """Build the flat dict shared by the --from-* modes (sans stage records).

    These modes have no event file, so they are local-first: run / instrument /
    ipts are optional *metadata* recorded as-is (no canonical /SNS paths are
    fabricated from them), relative operator paths resolve against the current
    directory, and absolute paths pass through. The LLM defaults are applied so
    plan-data / aure analyze always have an endpoint.
    """
    flat: dict = {}

    run = seed.get("run")
    if run is not None:
        run_str = str(run).strip()
        flat["run"] = int(run_str) if run_str.isdigit() else run
    if seed.get("instrument"):
        flat["instrument"] = seed["instrument"]
    if seed.get("ipts"):
        flat["ipts"] = seed["ipts"]

    flat["llm_provider"] = seed.get("llm_provider", _DEFAULT_LLM["provider"])
    flat["llm_model"] = seed.get("llm_model", _DEFAULT_LLM["model"])
    flat["llm_base_url"] = seed.get("llm_base_url", _DEFAULT_LLM["base_url"])

    if seed.get("sequence_total") is not None:
        flat["sequence_total"] = int(seed["sequence_total"])
    if seed.get("prompt"):
        flat["prompt"] = seed["prompt"]

    root = os.getcwd()
    for key in ("template_file", "context_file", "output_directory"):
        if seed.get(key):
            flat[key] = _resolve_path(str(seed[key]), root)
    return flat


def _build_state_from_reduced(reduced_file: str, seed: dict) -> dict:
    """Build a state that starts past reduction, from an already-reduced file.

    The given *reduced_file* is recorded as ``stages.reduction`` ``partial_file``
    (the artifact plan-data consumes) with the stage marked ``ok``, so the
    analyzer runs plan-data directly.
    """
    _require(seed, _REQUIRED_FROM_REDUCED)
    flat = _common_flat(seed)

    context_path = flat.get("context_file")
    if not context_path or not Path(context_path).is_file():
        raise click.UsageError(f"context_file does not exist: {context_path}")

    state = build_state(flat)
    state["stages"]["reduction"] = {
        "status": "ok",
        "params": {},
        "artifacts": {"partial_file": os.path.abspath(reduced_file)},
        "info": {"externally_reduced": True},
    }
    return state


def _build_state_from_plan(plan_file: str, seed: dict) -> dict:
    """Build a state that starts past planning, from an existing plan (job YAML).

    The given *plan_file* is recorded as ``stages.analysis`` ``job_yaml`` so the
    analyze step (analyze-sample / aure analyze) can run without re-planning.
    The stage stays ``pending`` because the fit itself has not run yet.
    """
    _require(seed, _REQUIRED_FROM_PLAN)
    flat = _common_flat(seed)

    state = build_state(flat)
    state["stages"]["analysis"] = {
        "status": "pending",
        "params": {},
        "artifacts": {"job_yaml": os.path.abspath(plan_file)},
        "info": {"externally_planned": True},
    }
    return state


@click.command()
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--from-reduced", "from_reduced",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Start past reduction: build the state from an already-reduced "
         "dataset (the partial file plan-data consumes). Pass SEED_FILE as the "
         "only positional argument; no event file is read.",
)
@click.option(
    "--from-plan", "from_plan",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Start past planning: build the state from an existing plan (the job "
         "YAML plan-data emits). Pass SEED_FILE as the only positional "
         "argument; the analyze step then runs without re-planning.",
)
@click.option(
    "--output", "-o",
    type=click.Path(dir_okay=False),
    default="config.json",
    show_default=True,
    help="Path to write the state JSON.",
)
@click.option(
    "--facility-root",
    default=DEFAULT_FACILITY_ROOT,
    show_default=True,
    help="Mounted facility data root for the default (event-file) mode; "
         "canonical paths are reconstructed as <root>/<INSTRUMENT>/<IPTS>/"
         "{nexus,shared}/.... Ignored by --from-reduced / --from-plan.",
)
def main(paths, from_reduced, from_plan, output, facility_root):
    """Bootstrap a workflow-state JSON for the reflectivity pipeline.

    \b
    Three modes, picked by how you invoke it:
      seed-config EVENT_FILE SEED_FILE
        From a run's NeXus event file (filename ignored — identity is read from
        the contents). All stages start pending.
      seed-config SEED_FILE --from-reduced REDUCED_FILE
        Start past reduction; stages.reduction is pre-filled (partial_file =
        REDUCED_FILE) so the analyzer runs plan-data directly.
      seed-config SEED_FILE --from-plan PLAN_FILE
        Start past planning; stages.analysis.job_yaml is pre-filled so analyze-
        sample / aure analyze runs without re-planning.

    \b
    SEED_FILE — JSON or YAML with the fields the chosen mode can't derive.
      Default mode:   template_file, output_directory, context_file, sequence_total
      --from-reduced: output_directory, context_file
      --from-plan:    output_directory
      Optional:       prompt, run, instrument, ipts,
                      llm_provider, llm_model, llm_base_url

    \b
    The --from-* modes are local-first: there is no event file, so run identity
    is optional metadata (no canonical /SNS paths are fabricated), relative seed
    paths resolve against the current directory, and absolute paths pass
    through. --facility-root applies to the default (event-file) mode only.

    \b
    Examples:
      seed-config REF_L_226644.nxs.h5 seed.json
      seed-config seed.yaml --from-reduced REFL_226642_3_226644_partial.txt -o 226644.json
      seed-config seed.yaml --from-plan job_Cu-D2O-226642.yaml -o 226644.json
    """
    if from_reduced and from_plan:
        raise click.UsageError("--from-reduced and --from-plan are mutually exclusive.")

    if from_reduced or from_plan:
        if len(paths) != 1:
            raise click.UsageError(
                "with --from-reduced / --from-plan, pass exactly one positional "
                "argument: SEED_FILE."
            )
        seed = _load_seed(paths[0])
        if from_reduced:
            state = _build_state_from_reduced(from_reduced, seed)
        else:
            state = _build_state_from_plan(from_plan, seed)
    else:
        if len(paths) != 2:
            raise click.UsageError(
                "expected two positional arguments: EVENT_FILE SEED_FILE "
                "(or use --from-reduced / --from-plan with just SEED_FILE)."
            )
        event_file, seed_file = paths
        seed = _load_seed(seed_file)
        state = _build_state(event_file, seed, facility_root)

    with open(output, "w") as f:
        json.dump(state, f, indent=2)
    click.echo(f"Wrote {output}")


if __name__ == "__main__":
    main()
