"""
Bootstrap a workflow-state JSON from an event file path and a minimal seed.

The seed contains only what cannot be derived from the event file path:

  Required:
    template_file:    relative-to-IPTS-shared (or absolute) Mantid template
    output_directory: where this run's artifacts will land
    context_file:     Markdown context for plan-data
    sequence_total:   number of partials per complete measurement

  Optional (with defaults applied here):
    prompt
    llm_provider, llm_model, llm_base_url

Everything else (data_directory, run, ipts, instrument, nexus_file,
ipts_shared_root) is parsed from the event file path. Relative seed
paths resolve against the IPTS shared root, which is discovered by
walking up from the event file's directory to the IPTS-named segment.
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

import click
import yaml

from ndip_state.state import build_state


_REQUIRED_KEYS = ("template_file", "output_directory", "context_file", "sequence_total")

_DEFAULT_LLM = {
    "provider": "local",
    "model": "gpt-4",
    "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/",
}

# Match e.g. REF_L_226644.nxs.h5 — instrument is an upper-case token with
# optional underscore, run is digits.
_EVENT_RE = re.compile(r"^(?P<instrument>[A-Z][A-Z_]+)_(?P<run>\d+)\.nxs\.h5$")
_IPTS_RE = re.compile(r"^IPTS-\d+$")


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


def _parse_event_file(event_file: str) -> dict:
    """Extract identifiers and paths from an instrument NeXus filename.

    Expects ``/<...>/<IPTS-NNNNN>/<...>/<INSTRUMENT>_<RUN>.nxs.h5``. Returns
    ``run``, ``instrument``, ``ipts``, ``data_directory``, ``event_file``
    (absolutized), and ``ipts_shared_root``. ``ipts`` may be ``None`` when the
    path lacks an ``IPTS-*`` segment; in that case ``ipts_shared_root`` falls
    back to ``<event_file_dir>/../shared``.

    The path is absolutized but symlinks are **not** resolved, so a canonical
    ``/SNS/<INST>/<IPTS>/...`` path is preserved rather than collapsing to its
    ``/gpfs/...`` realpath. This keeps the seeded state consistent with the
    rest of the pipeline, whose ``canonicalize_paths`` step maps realpaths back
    to this canonical form.
    """
    path = Path(os.path.abspath(event_file))
    basename = path.name

    match = _EVENT_RE.match(basename)
    if not match:
        raise click.UsageError(
            f"event_file basename does not match '<INSTRUMENT>_<RUN>.nxs.h5': {basename}"
        )

    run = int(match.group("run"))
    instrument = match.group("instrument")
    data_directory = str(path.parent)

    ipts = None
    ipts_root: Optional[Path] = None
    for parent in path.parents:
        if _IPTS_RE.match(parent.name):
            ipts = parent.name
            ipts_root = parent
            break

    if ipts_root is not None:
        ipts_shared_root = str(ipts_root / "shared")
    else:
        # No IPTS segment found — fall back to a sibling 'shared' next to the
        # data directory. Useful for test fixtures and unusual layouts.
        ipts_shared_root = str(path.parent.parent / "shared")

    return {
        "run": run,
        "instrument": instrument,
        "ipts": ipts,
        "data_directory": data_directory,
        "event_file": str(path),
        "ipts_shared_root": ipts_shared_root,
    }


def _resolve_path(value: str, root: str) -> str:
    """Resolve a seed path: absolute → as-is; relative → joined to *root*."""
    p = Path(value)
    if p.is_absolute():
        return str(p)
    return str(Path(root) / value)


def _build_state(event_file: str, seed: dict) -> dict:
    """Validate inputs and build the state document."""
    missing = [k for k in _REQUIRED_KEYS if k not in seed]
    if missing:
        raise click.UsageError(
            f"seed is missing required key(s): {', '.join(missing)}"
        )

    derived = _parse_event_file(event_file)
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
def main(event_file: str, seed_file: str, output: str) -> None:
    """Bootstrap a workflow-state JSON from an event file + minimal seed.

    \b
    EVENT_FILE — the run's NeXus file (e.g. REF_L_226644.nxs.h5).
                 Path is parsed (not read) to derive run, instrument,
                 IPTS, and data_directory.

    \b
    SEED_FILE  — JSON or YAML with the fields this tool can't derive.
                 Required:  template_file, output_directory,
                            context_file, sequence_total.
                 Optional:  prompt, llm_provider, llm_model, llm_base_url.

    \b
    Relative paths in the seed resolve against the IPTS shared root
    (e.g. /SNS/REF_L/IPTS-36897/shared) discovered from the event file
    path. Absolute paths pass through unchanged.

    Examples::

        seed-config /SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5 seed.json
        seed-config event.h5 seed.yaml -o state_226644.json
    """
    seed = _load_seed(seed_file)
    state = _build_state(event_file, seed)
    with open(output, "w") as f:
        json.dump(state, f, indent=2)
    click.echo(f"Wrote {output}")


if __name__ == "__main__":
    main()
