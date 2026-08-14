# NDIP Workflows for the ISAAC project

Galaxy workflows that take neutron reflectometry event files all the way to
ISAAC AI-Ready Records.

## The pipeline

```
seed_config | yaml_parser  →  reduction  →  simple_analyzer  →  data_assembler
       state seed             stages.reduction   stages.analysis    stages.assembly
```

A single JSON document — the workflow state — threads through every stage.
Each stage records its outcome under `stages.<name>` (params, artifacts,
info, status). The schema is defined in
[`docs/state-schema.md`](docs/state-schema.md); an end-to-end walkthrough is
in [`docs/state-handling.md`](docs/state-handling.md).

### Decoupled architecture

This repo owns the schema. The pipeline tools are **schema-agnostic**: they
take explicit CLI arguments and emit a neutral
[`ndip-tool-result/1`](docs/tool-result-schema.md) manifest. Around each tool
call the Galaxy wrapper runs two halves of an *adapter* (bundled in
[`tools/ndip_shim.py`](tools/ndip_shim.py)):

```
state ──[project-out]──▶ tool CLI args ──▶ [foreign tool] ──▶ result.json
                                                                  │
state ◀──[merge-in (+ canonicalize)]──────────────────────────────┘
```

The foreign container images (analyzer, data-assembler, nr-isaac-format)
never read or write the workflow state — Galaxy injects the shim at runtime.

The same flow drives an agent without Galaxy via `ndip-run`; see the
[Running without Galaxy](docs/state-handling.md#running-the-chain-without-galaxy)
section.

## Entry points

There are two ways to produce the initial seed. Both emit the same shape;
pick whichever matches the situation.

### `seed-config` — single run, on-demand

Give it the event NeXus file and a small JSON or YAML seed. It reads `run`,
`instrument`, and `ipts` from the file *contents* with h5py (the filename is
ignored — Galaxy renames uploads to `dataset_<uuid>.dat`), reconstructs the
canonical paths under `--facility-root` (default `/SNS`), resolves relative
seed paths against the IPTS shared root, and emits a complete state JSON.

```yaml
# seed.yaml
template_file:     autoreduce/template_down.xml
output_directory:  isaac/reduction/sample5
context_file:      isaac/context.md
sequence_total:    3
```

```sh
seed-config /SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5 seed.yaml \
    -o 226644.json
```

Galaxy wrapper: [`tools/seed_config.xml`](tools/seed_config.xml).

### `yaml-parser` — batched runs

Hand it one YAML file describing many runs. Common defaults go under
`common:` and per-run entries go under `runs:` (a bare top-level list is
also accepted). It writes one JSON per run into a Galaxy `Collection` that
feeds the rest of the workflow. A minimal demo input is at
[`example/batch.yaml`](example/batch.yaml).

Galaxy wrapper: [`tools/yaml_parser.xml`](tools/yaml_parser.xml).

## Tools

| Tool                                                       | Container                                                 | Wraps                                                  |
|------------------------------------------------------------|-----------------------------------------------------------|--------------------------------------------------------|
| [`seed_config.xml`](tools/seed_config.xml)                 | `ghcr.io/isaac-neutrons/ndip-workflows`                   | `seed-config` (this repo)                              |
| [`yaml_parser.xml`](tools/yaml_parser.xml)                 | `ghcr.io/isaac-neutrons/ndip-workflows`                   | `yaml-parser` (this repo)                              |
| [`reduction.xml`](tools/reduction.xml)                     | `ghcr.io/neutrons-ai/nr-analyzer`                         | `simple-reduction` ([neutrons-ai/nr-analyzer](https://github.com/neutrons-ai/nr-analyzer)) |
| [`simple_analyzer.xml`](tools/simple_analyzer.xml)         | `ghcr.io/neutrons-ai/nr-analyzer:*-slim`                  | `plan-data` + `analyze-sample` (same)                  |
| [`data_assembler.xml`](tools/data_assembler.xml)           | `ghcr.io/isaac-neutrons/data-assembler`                   | `data-assembler ingest` + `nr-isaac-format convert-ingest` |

The three downstream tool XMLs are **generated** from `tools/*.xml.in`
templates by [`tools/build_tool_xmls.py`](tools/build_tool_xmls.py), which
inlines [`tools/ndip_shim.py`](tools/ndip_shim.py) at the `@NDIP_SHIM@`
marker. Regenerate after editing either:

```sh
python tools/build_tool_xmls.py
```

`tests/test_ndip_shim.py` fails if the committed XMLs are stale, and asserts
the shim behaves identically to the canonical `ndip_state` modules.

> The bundled `workflows/Galaxy-Workflow-LR_Reduce_Batch.ga` was exported
> against an earlier version of the tool inputs and needs to be re-exported
> from Galaxy after rewiring it against the current XMLs.

## Layout

```
src/
  ndip_state/        — schema, projection, adapters, canonicalize, ndip-run
  yaml_parser/       — CLIs: yaml-parser (batched), seed-config (single)
tools/
  ndip_shim.py       — self-contained orchestration bundle (inlined into XMLs)
  build_tool_xmls.py — generator: ndip_shim + *.xml.in -> *.xml
  *.xml.in / *.xml   — Galaxy tool templates and generated wrappers
tests/               — pytest suite
docs/
  state-schema.md       — workflow-state shape (the orchestrator's contract)
  tool-result-schema.md — neutral manifest the foreign tools emit
  state-handling.md     — end-to-end walkthrough + agent-driven snippet
  experiment-workflows.md
example/             — runnable seed.json + batch.yaml + sample partial files
workflows/           — Galaxy workflow definitions (.ga)
```

## Installing

```sh
pip install -e '.[test]'         # dev: this repo + pytest (stdlib-light)
pip install -e '.[workflow]'     # + the downstream science CLIs (needs Python >=3.11)
```

The `[workflow]` extra installs the tools `ndip-run` shells out to — `plan-data`
and `analyze-sample` (from [`nr-analyzer`](https://github.com/neutrons-ai/nr-analyzer),
**without** Mantid), `aure`, `data-assembler`, and `nr-isaac-format` — so the
whole chain from an already-reduced file to an ISAAC record runs on a plain
Python env, no Galaxy. Granular extras `analyzer` / `assembler` install just one
side; `all` = `workflow` + `test`.

**Reduction is not in any extra.** It needs Mantid, which is conda/pixi-only and
not pip-installable; run it via the `ghcr.io/neutrons-ai/nr-analyzer` (full)
container, or skip it — see below.

### Running the full workflow without Galaxy

When a run's reduced partial file already exists locally, seed *past* reduction
with `seed-config --from-reduced` (it marks `stages.reduction` done and points
at your file), then let `plan-data` find the sister files and drive the rest:

```sh
S=./state.json
seed-config seed.yaml --from-reduced REFL_226642_3_226644_partial.txt -o $S
ndip-run all --state $S           # plan -> analyze -> ingest -> convert
```

`ndip-run all` chains the downstream stages (each with its default `--tool-cmd`)
and stops on the first failure; it skips reduction unless you pass
`--include-reduction` (which needs the full Mantid image and an event file).

The analyze step has two backends, matching the two Galaxy analyzer tools:
`--analyzer simple` (default, `analyze-sample`) or `--analyzer aure` (the
agentic AuRE analyzer). It applies to both `ndip-run analyze` and `ndip-run all`:

```sh
ndip-run all --state $S --analyzer aure
```

`--analyzer aure` also changes the **ingest** step. A REF_L measurement is
several runs — the same state at a few angles, each with its own partial file —
and AuRE co-refines them together, so ingesting only the segment named in the
state would throw the rest of the co-refinement away. The aure path runs
`data-assembler ingest-workflow` over the whole results directory instead: one
reflectivity record per segment, sharing a sample, with the model linked to all
of them and each dataset reported against its own run.

### Provenance package

The analysis artifacts land in scattered folders (`plan/`, `models/`,
`results/<model>/`, `reports/`, `assembled/`). `ndip-package` reads the final
state and gathers them into one organized, git-storable directory — the
reproduction core (inputs, plan, model/checkpoints, compact fit results),
reports, the AI record, plus a `MANIFEST.json` (per-file role + sha256 + tool
versions) and a `REPRODUCE.md` runbook:

```sh
ndip-package --state $S -o path/to/repo/provenance/<model>
```

It works for both analyzer backends (simple's `models/<model>.py` +
`results/<model>/`, or AuRE's top-level `problem.json` + `checkpoints/` trail),
copies the small text artifacts, and **references** large binaries (raw NeXus,
parquet) and bulky regenerable byproducts (MCMC chains, plots) by path + sha256.
Because planning (LLM) and fitting (MCMC) aren't bit-reproducible, the package is
*frozen-artifact-authoritative*: it records inputs + LLM endpoint + tool versions
so a re-run can be compared, not bit-verified.

`MANIFEST.json` also carries `fit.chisq` (and `iterations`), lifted from the
referenced `final_state.json` at package time. That file is most of a megabyte of
arrays so it is referenced rather than copied — but the one number every consumer
wants from it should not require scraping the checkpoint prose, where the
per-segment χ² values sit next to the overall one.

### Handing a measurement to nr-workbench

The pipeline ends with an answer nobody has to accept. `nr-workbench` is where
the analysis continues: it can drive a coding harness unattended
(`nrw agent run`) and then hand the result to a person (`nrw handoff`). But it
will not start without prose under `## Fits to perform` in
`samples/<id>/sample.md` — that is its only task channel. `ndip-workbench` writes
it, plus the data and the register beside it:

```sh
ndip-workbench provenance/230536 --project ~/analysis/ipts-36897          # plan
ndip-workbench provenance/230536 --project ~/analysis/ipts-36897 --write
nrw agent run s230536
```

`SOURCE` is normally a provenance package, which is self-contained and keeps
working after being copied to another machine; a state JSON also works, but its
recorded paths only resolve where the pipeline ran. One handoff is **one
measurement**: its angle segments are co-refined against each other (what the
sample notes ask for and what AuRE did), but a second measurement is never pulled
in — comparing measurements is judgement work for the person at `nrw handoff`.

It writes `sample.md` (the five sections nr-workbench's offline checks read by
name, with the run's own condition from `context.md`), `sample.yaml` (the
`nrw-sample/1` register, byte-identical to what `nrw sample scan` writes), the
reduced segments under `data/steady/`, and `handoff/prior-analysis.{md,json}` —
the pipeline's own fit as **evidence**, never as a starting model, with the
deterministic checks over it: parameters sitting on a bound, bounds the fit
widened past what the notes asked for, SLDs far from their nominal, and a title in
the header that disagrees with `context.md`.

Two things it refuses to do. It will not create the project — `nrw init` also
installs the permission hook that bounds an unattended session, and a workflow
faking that scaffold would produce a project whose safety properties nobody
agreed to. And it will not overwrite a `sample.md` lacking its own marker: that
file is somebody's notes, possibly the declared task of an analysis already under
way, and it is the one thing in an nrw project that cannot be regenerated.

```sh
pytest
```

The `ndip_state` package is stdlib-only by design — no dependencies, fast
imports, and the same logic ports cleanly into the inlined `tools/ndip_shim.py`
that ships into foreign containers via Galaxy's configfile mechanism.

### Console scripts

| Command       | Purpose |
|---------------|---------|
| `seed-config` | Single-run seed: event file + minimal seed YAML/JSON → state JSON. Also `--from-reduced` / `--from-plan` to start mid-pipeline. |
| `yaml-parser` | Batch seed: one YAML of many runs → a directory of state JSONs. |
| `ndip-run`    | Drive one pipeline stage (project-out → tool `--result-out` → merge-in), or `ndip-run all` to chain the downstream stages. `--tool-cmd` defaults per stage; `--analyzer {simple,aure}` picks the analyze backend. Agent-friendly. |
| `ndip-package`| Gather a reproducible **provenance package** (inputs, plan, model, compact results, reports, AI record + a manifest of roles/checksums/tool versions) from a final state. |
| `ndip-workbench`| Hand one finished measurement to an **nr-workbench** project — `sample.md` (incl. the `## Fits to perform` task), `sample.yaml`, the reduced segments, and the pipeline's own fit as evidence — ready for `nrw agent run`. Reads a provenance package or a state. Plans by default; `--write` to commit. |
