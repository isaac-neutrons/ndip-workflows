"""
Bootstrap a workflow-state JSON from an event NeXus file and a minimal seed.

The seed contains only what cannot be read from the event file:

  Required:
    template_file:    relative-to-IPTS-shared (or absolute) Mantid template
    output_directory: where this run's artifacts will land
    context_file:     Markdown context for plan-data
    sequence_total:   number of partials per complete measurement

  Optional (with defaults applied here):
    prompt
    llm_provider, llm_model, llm_base_url

Run identity (run, instrument, ipts) is read from the **contents** of the
event NeXus file with h5py, not from its filename: Galaxy stages uploaded
datasets under opaque names like ``dataset_<uuid>.dat``, so the basename
cannot be trusted. From that metadata the canonical analysis paths
(nexus_file, data_directory, ipts_shared_root) are reconstructed under the
facility root (``/SNS`` by default), and relative seed paths resolve against
the reconstructed IPTS shared root.
"""

import json
import os
from pathlib import Path

import click
import yaml

from ndip_state.state import build_state


_REQUIRED_KEYS = ("template_file", "output_directory", "context_file", "sequence_total")

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


def _build_state(event_file: str, seed: dict, facility_root: str) -> dict:
    """Validate inputs and build the state document."""
    missing = [k for k in _REQUIRED_KEYS if k not in seed]
    if missing:
        raise click.UsageError(
            f"seed is missing required key(s): {', '.join(missing)}"
        )

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


@click.command()
@click.argument(
    "event_file",
    type=click.Path(exists=True, dir_okay=False),
)
@click.argument(
    "seed_file",
    type=click.Path(exists=True, dir_okay=False),
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
    help="Mounted facility data root; canonical paths are reconstructed as "
         "<root>/<INSTRUMENT>/<IPTS>/{nexus,shared}/...",
)
def main(event_file: str, seed_file: str, output: str, facility_root: str) -> None:
    """Bootstrap a workflow-state JSON from an event file + minimal seed.

    \b
    EVENT_FILE — the run's NeXus file. Its run, instrument, and IPTS are
                 read from the file contents with h5py (the filename is
                 ignored — Galaxy renames uploads to dataset_<uuid>.dat).

    \b
    SEED_FILE  — JSON or YAML with the fields this tool can't derive.
                 Required:  template_file, output_directory,
                            context_file, sequence_total.
                 Optional:  prompt, llm_provider, llm_model, llm_base_url.

    \b
    Canonical paths (nexus_file, data_directory, ipts_shared_root) are
    reconstructed under --facility-root (default /SNS) as
    <root>/<INSTRUMENT>/<IPTS>/.... Relative seed paths resolve against the
    reconstructed IPTS shared root; absolute paths pass through unchanged.

    Examples::

        seed-config REF_L_226644.nxs.h5 seed.json
        seed-config dataset_ea91e004.dat seed.yaml -o state_226644.json
    """
    seed = _load_seed(seed_file)
    state = _build_state(event_file, seed, facility_root)
    with open(output, "w") as f:
        json.dump(state, f, indent=2)
    click.echo(f"Wrote {output}")


if __name__ == "__main__":
    main()
