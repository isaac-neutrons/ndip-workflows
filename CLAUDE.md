# CLAUDE.md

Guidance for AI assistants (and humans) working in this repo. For the full
architecture, read [`README.md`](README.md) and [`docs/`](docs/); this file
covers how to work here safely and the gotchas we've actually hit.

## What this repo is

Galaxy workflows that take neutron reflectometry event files to ISAAC
AI-Ready Records. A single JSON **workflow state** threads through every
stage:

```
seed_config | yaml_parser  →  reduction  →  simple_analyzer / analyzer  →  data_assembler
       state seed              stages.reduction      stages.analysis            stages.assembly
```

- **`src/ndip_state/`** — the canonical schema + orchestration logic
  (`state`, `projection`, `adapters`, `canonicalize`, `run`).
- **`src/yaml_parser/`** — the `seed-config` and `yaml-parser` CLIs that build
  the initial state.
- **`tools/`** — the Galaxy tool XMLs that wrap foreign science containers.
- **`tests/`** — pytest; the source of truth for "is it still correct".

## Build & test

```sh
pip install -e ".[test]"     # needs h5py, click, pyyaml, pytest
pytest                       # run the whole suite before committing
pytest tests/test_ndip_shim.py   # the generated-XML guard specifically
python tools/build_tool_xmls.py  # regenerate tools/*.xml from *.xml.in
```

**Always run the tests before committing.** Several invariants below are
enforced only by tests — a commit with red tests silently breaks them.

## Critical: generated tool XMLs

`tools/<name>.xml` is **generated** from `tools/<name>.xml.in` by
`tools/build_tool_xmls.py`, which substitutes the `@NDIP_SHIM@` marker with the
contents of `tools/ndip_shim.py`.

- **Edit the `.xml.in` template, never the generated `.xml`.** Then run
  `python tools/build_tool_xmls.py`.
- `tests/test_ndip_shim.py::test_generated_xmls_are_up_to_date` fails if a
  committed `.xml` doesn't match its template + shim — but only catches drift
  **if you actually run the tests**. A hand-edit made directly to a generated
  `.xml` lives on borrowed time: the next `build_tool_xmls.py` run overwrites
  it from the template, and the change is lost. (This is exactly how we lost
  the `cp … results.json` line in `data_assembler.xml` — it had been edited
  into the generated file but never into the template.)
- `seed_config.xml` and `yaml_parser.xml` are **hand-written** (no `.xml.in`,
  no shim) — edit those directly.

## Critical: the shim mirrors `src/ndip_state`

`tools/ndip_shim.py` is a stdlib-only, self-contained bundle of the
`src/ndip_state/{state,projection,canonicalize,adapters}.py` logic, inlined
into every generated tool XML so the foreign containers need only `python`.

- Keep it in **parity** with `src/ndip_state`. Change both sides together;
  `tests/test_ndip_shim.py` asserts they agree.
- **No `<angle brackets>` in `ndip_shim.py` source** (including docstrings).
  The shim is injected via a `<configfile>` as **raw XML text**, not CDATA, so
  `<run>` etc. parse as XML tags and break the generated XML. Use prose like
  "output_directory joined with run" instead. (`test_generated_xmls_are_well_formed_xml`
  guards this.) Angle brackets *inside* a tool's `<command><![CDATA[ … ]]>`
  block are fine — CDATA is literal.

## Tool / container gotchas

- **Slim analyzer image has no pixi.** `analyzer.xml` and `simple_analyzer.xml`
  run in `ghcr.io/mdoucet/analyzer:*-slim`, which has no pixi. Call CLIs
  (`python`, `plan-data`, `analyze-sample`, `aure`) **directly** — never wrap
  in `pixi run`. Non-slim images and the data-assembler image are separate;
  check the actual container before adding/removing a `pixi run` prefix.
- **Galaxy renames uploaded files** to `dataset_<uuid>.dat`. A tool must
  **never derive identity from an input file's basename/path.** `seed-config`
  reads run / instrument / IPTS from the NeXus *contents* with h5py
  (`/entry/run_number`, `/entry/instrument/name`, `/entry/experiment_identifier`).
- **Canonical paths, not realpaths.** Paths in the state are canonical
  (`/SNS/<INST>/<IPTS>/...`), reconstructed under `--facility-root` (default
  `/SNS`); don't `Path.resolve()` an input (it follows the `/SNS → /gpfs`
  symlink). The pipeline's `canonicalize_paths` maps realpaths back to this
  canonical form.
- **Per-run output dirs.** Analysis/assembly artifacts nest under
  `<output_directory>/<run>/{plan,results,reports,assembled}` so concurrent
  runs sharing an `output_directory` don't overwrite each other. The reduction
  stage writes to the bare `output_directory`. The XMLs get this dir from the
  shim's `rundir` command; `project-out` derives the same via `_sub()`. Keep
  the two in step.

## Conventions

- Match the surrounding code's style and comment density.
- The state schema lives in `docs/state-schema.md`; the neutral tool contract
  in `docs/tool-result-schema.md`. Update docs when behavior changes.
