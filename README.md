# NDIP Workflows for the ISAAC project

Galaxy workflows that take neutron reflectometry event files all the way to
ISAAC AI-Ready Records.

## The pipeline

```
seed_config | yaml_parser  →  reduction  →  simple_analyzer  →  data_assembler
       v1 state seed             reduction.*       analysis.*           assembly.*
```

Every tool threads a single versioned JSON document — the **v1 workflow
state** — through the chain. Each stage reads the state, runs its
underlying CLI, and writes the same state back with its own stage block
populated. The schema is defined in
[`docs/state-schema.md`](docs/state-schema.md); an end-to-end walkthrough
of a real run is in [`docs/state-handling.md`](docs/state-handling.md).

The same CLIs that the Galaxy tools invoke also accept `--state-in PATH`
/ `--state-out PATH` directly, so an agent or script can drive the
pipeline without Galaxy — see
[the "running without Galaxy" snippet](docs/state-handling.md#running-the-chain-without-galaxy).

## Entry points

There are two ways to produce the initial v1 state. Both emit the same
shape; pick whichever matches the situation.

### `seed-config` — single run, on-demand

Give it the event-file path and a small JSON or YAML seed; it parses the
path for `run`, `instrument`, `ipts`, and `data_directory`, resolves
relative seed paths against the IPTS shared root, and emits a complete
v1 state JSON.

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
`common:` and per-run overrides go under `runs:` (or as a bare top-level
list). It writes one v1 JSON per run into a Galaxy `Collection` that
feeds the rest of the workflow.

Galaxy wrapper: [`tools/yaml_parser.xml`](tools/yaml_parser.xml).

## Tools

| Tool                                                       | Container                                                 | Wraps                                                  |
|------------------------------------------------------------|-----------------------------------------------------------|--------------------------------------------------------|
| [`seed_config.xml`](tools/seed_config.xml)                 | `ghcr.io/isaac-neutrons/ndip-workflows`                   | `seed-config` (this repo)                              |
| [`yaml_parser.xml`](tools/yaml_parser.xml)                 | `ghcr.io/isaac-neutrons/ndip-workflows`                   | `yaml-parser` (this repo)                              |
| [`reduction.xml`](tools/reduction.xml)                     | `ghcr.io/mdoucet/analyzer`                                | `simple-reduction` ([mdoucet/analyzer](https://github.com/mdoucet/analyzer)) |
| [`simple_analyzer.xml`](tools/simple_analyzer.xml)         | `ghcr.io/mdoucet/analyzer:*-slim`                         | `plan-data` + `analyze-sample` (same)                  |
| [`data_assembler.xml`](tools/data_assembler.xml)           | `ghcr.io/isaac-neutrons/data-assembler`                   | `data-assembler ingest` + `nr-isaac-format convert-ingest` |

## Layout

```
src/
  ndip_state/        — canonical state module (build_state, load_state, …)
  yaml_parser/       — CLIs: yaml-parser (batched), seed-config (single)
tools/               — Galaxy tool XMLs
tests/               — pytest suite
docs/
  state-schema.md    — full schema reference
  state-handling.md  — end-to-end walkthrough + agent-driven snippet
  experiment-workflows.md
workflows/           — Galaxy workflow definitions (.ga)
```

## Development

```sh
pip install -e '.[test]'
pytest
```

The state module ([`src/ndip_state/state.py`](src/ndip_state/state.py)) is
stdlib-only so it can be vendored verbatim into the analyzer and
data-assembler images, where it backs each CLI's `--state-in /
--state-out` plumbing. The same source is also inlined into each tool
XML as the `state_env` configfile — a slim helper that emits shell
exports the tool's bash needs. The tests under
[`tests/test_state_env_helper.py`](tests/test_state_env_helper.py) guard
the inlined copies against drift across the three XMLs.
