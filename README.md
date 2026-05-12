# NDIP Workflows for the ISAAC project

Workflow to process a list of reflectometry data sets.

## Input
The input is a YAML file with a list of runs with processing parameters:

```yaml
- run: 218386
  event_file: "/SNS/REF_L/IPTS-34347/nexus/REF_L_218386.nxs.h5"
  context_file: "/SNS/REF_L/IPTS-34347/shared/isaac/context/context_218386.txt"
  data_directory: "/SNS/REF_L/IPTS-34347/nexus"
  template_file: "/SNS/REF_L/IPTS-34347/shared/autoreduce/template_down.xml"
  prompt: "45 to 60 nm Cu on 15 to 25 nm Ti on a silicon substrate, in dTHF (ambient medium). The incoming comes from the back (back reflection)."
  export_path: "/SNS/REF_L/IPTS-34347/shared/isaac/218386/export_218386.gz"
  output_directory: "/SNS/REF_L/IPTS-34347/shared/isaac/218386
- run: 218387
  event_file: "/SNS/REF_L/IPTS-34347/nexus/REF_L_218387.nxs.h5"
  context_file: "/SNS/REF_L/IPTS-34347/shared/isaac/context/context_218387.txt"
  data_directory: "/SNS/REF_L/IPTS-34347/nexus"
  template_file: "/SNS/REF_L/IPTS-34347/shared/autoreduce/template_down.xml"
  prompt: "45 to 60 nm Cu on 15 to 25 nm Ti on a silicon substrate, in dTHF (ambient medium). The incoming comes from the back (back reflection)."
  export_path: "/SNS/REF_L/IPTS-34347/shared/isaac/218387/export_218387.gz"
  output_directory: "/SNS/REF_L/IPTS-34347/shared/isaac/218387
```

# Workflow
The YAML parser tool will parse the input file and create two `Collection` objects that will be passed to a sub-workflow. 
One is a `Colleciton` of NeXus files, and the other is a `Collection` of json configurations.

## Reducer JSON output

The `Reduction` tool emits an updated JSON config that carries the
original input fields forward and adds the paths produced by the
reduction step. This config is the bridge between the reducer and the
downstream `Data Assembler` tool. Example:

```json
{
  "run": 218386,
  "event_file": "/SNS/REF_L/IPTS-34347/nexus/REF_L_218386.nxs.h5",
  "context_file": "/SNS/REF_L/IPTS-34347/shared/isaac/context/context_218386.txt",
  "data_directory": "/SNS/REF_L/IPTS-34347/nexus",
  "template_file": "/SNS/REF_L/IPTS-34347/shared/autoreduce/template_down.xml",
  "prompt": "45 to 60 nm Cu on 15 to 25 nm Ti on a silicon substrate, in dTHF (ambient medium). The incoming comes from the back (back reflection).",
  "export_path": "/SNS/REF_L/IPTS-34347/shared/isaac/218386/export_218386.gz",
  "output_directory": "/SNS/REF_L/IPTS-34347/shared/isaac/218386",
  "result_file": "/SNS/REF_L/IPTS-34347/shared/isaac/218386/REFL_218386_partial.txt",
  "partial_file": "/SNS/REF_L/IPTS-34347/shared/isaac/218386/REFL_218386_partial.txt",
  "combined_file": "/SNS/REF_L/IPTS-34347/shared/isaac/218386/REFL_218386_combined.txt",
  "raw_data": "/SNS/REF_L/IPTS-34347/nexus/REF_L_218386.nxs.h5"
}
```

Fields added by the reducer:

| Field | Source | Description |
|-------|--------|-------------|
| `result_file` | reducer summary `partial_file` | Path to the reduced reflectivity file used downstream (alias for `partial_file`). |
| `partial_file` | reducer summary | Path to the per-run reduced reflectivity `.txt`. |
| `combined_file` | reducer summary | Path to the combined reduced reflectivity `.txt`. |
| `raw_data` | input YAML `event_file` | Canonical path to the raw NeXus file, forwarded so `Data Assembler` can record it as a `raw_data_pointer` asset in the ISAAC record. |

# TODO

## Workflow state schema

The JSON config threaded between tools has grown organically and is not
yet a curated state object. It currently carries duplicated fields
(e.g. `result_file` mirrors `partial_file`, `raw_data` mirrors
`event_file`) and a mix of input-only fields, reducer-produced paths,
and forwarded provenance hints — all in one flat namespace.

We should design an explicit state schema for this object: define which
fields are inputs vs. derived, which tool owns writing each field,
which downstream tools consume them, and remove aliases once consumers
are migrated. A versioned schema (e.g. embedded `state_version` key
plus a JSON Schema document) would let tools validate the state they
receive and fail loudly on drift.

## Provenance capture in ISAAC records

The raw NeXus file URI is threaded through the workflow via the
`raw_data` field in the JSON config (set by the reducer from the input
YAML's `event_file`) and passed to `nr-isaac-format convert-ingest` as
`--raw`, so the resulting record carries an `assets[]` entry with
`content_role: "raw_data_pointer"` and a durable `/SNS/REF_L/...` URI.

The reduced `.txt` file is still **not** captured as a
`reduction_product` asset. `--reduced` was intentionally omitted because
Galaxy stages it into a sandbox path (e.g. `/data/dataset_NNNN.dat`)
which would yield a correct SHA-256 paired with a meaningless URI.

To close that gap we need a canonical path for the reduced file at the
point `convert-ingest` runs. Options to explore:

- Have the reducer emit the reduced file at a canonical
  `/SNS/REF_L/...` location and surface that path through the merged
  JSON config (analogous to how `raw_data` is now threaded).
- Have `nr-isaac-format` accept a URI override separate from the
  hashed-file path, so the SHA can be computed against the sandbox copy
  while the recorded URI points to the canonical location.
