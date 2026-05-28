# Workflow state schema

A single JSON document threads through every stage in the pipeline. The
canonical Python implementation lives in
[`src/ndip_state/state.py`](../src/ndip_state/state.py); the per-tool inlined
runtime bundle is [`tools/ndip_shim.py`](../tools/ndip_shim.py).

```
seed_config | yaml_parser  →  reduction  →  simple_analyzer  →  data_assembler
```

The orchestrator (this repo) owns the schema. Pipeline tools never read or
write this document directly — they take explicit CLI args and emit a neutral
[`ndip-tool-result/1`](tool-result-schema.md) manifest that an *adapter* folds
back in.

## Top-level shape

```json
{
  "schema_version": "2",
  "workflow": {
    "run": 226644,
    "instrument": "REF_L",
    "ipts": "IPTS-36897"
  },
  "inputs": {
    "operator": {
      "sequence_total": 3,
      "prompt": "Deposited 50 nm Cu on 3 nm Ti on Si, in D2O.",
      "template_file": "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml",
      "context_file": "/SNS/REF_L/IPTS-36897/shared/isaac/context.md",
      "output_directory": "/SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5",
      "export_path": "/SNS/REF_L/IPTS-36897/shared/isaac/export.gz",
      "llm": {
        "provider": "local",
        "model":    "gpt-4",
        "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/"
      }
    },
    "derived": {
      "nexus_file":       "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
      "data_directory":   "/SNS/REF_L/IPTS-36897/nexus",
      "ipts_shared_root": "/SNS/REF_L/IPTS-36897/shared"
    }
  },
  "stages": {
    "reduction": {
      "status": "ok",
      "params":   { "template_file": "...", "template_sha256": "ab12…", "theta_offset": 0.0 },
      "artifacts":{ "partial_file":  "...", "combined_file": "..." },
      "info":     { "first_run_of_set": 226642 }
    },
    "analysis": {
      "status": "ok",
      "params":   { "model_name": "Cu-D2O-226642", "perform_assembly": true,
                    "create_model_ready": true },
      "artifacts":{ "job_yaml":     "/.../plan/job_Cu-D2O-226642.yaml",
                    "problem_json": "/.../results/Cu-D2O-226642/problem.json",
                    "results_dir":  "/.../results",
                    "reports_dir":  "/.../reports",
                    "models_dir":   "/.../models" },
      "info":     { "pipeline_status": "ok",
                    "completed_stages": ["partial", "fit"],
                    "sequence_id": "Cu-D2O-226642",
                    "sequence_number": 3,
                    "sequence_complete": true }
    },
    "assembly": {
      "status": "ok",
      "params":   { "nexus_input":   "/.../REF_L_226644.nxs.h5",
                    "reduced_input": "/.../REFL_226642_3_226644_partial.txt",
                    "model_input":   "/.../problem.json",
                    "ingest_dir":    "/.../assembled" },
      "artifacts":{ "ingest_dir":    "/.../assembled",
                    "parquet_files": { "reflectivity": "...", "sample": "...",
                                       "reflectivity_model": "..." },
                    "isaac_record":  "/.../assembled/isaac_record_226644.json" },
      "info":     { "ingest_status": "completed", "isaac_status": "converted" }
    }
  },
  "errors": []
}
```

## The four-field stage record

Every stage record carries the same shape — *the same shape as a tool's
[result manifest](tool-result-schema.md)*. That symmetry is what makes merging
nearly a straight copy.

| Field | Meaning |
|-------|---------|
| `status` | Enum: `pending` (not attempted), `ok`, `skipped`, `failed`. |
| `params` | The **resolved inputs the stage actually used** — provenance. Captures what the tool reported it consumed (e.g. `theta_offset`, `model_name`) plus what the orchestrator handed it (e.g. `template_file`, `template_sha256`, `nexus_input`). Together they let the document reproduce the run. |
| `artifacts` | The **paths / outputs the stage produced**, keyed by the tool's names (e.g. `partial_file`, `problem_json`, `parquet_files`). |
| `info` | Scalar **diagnostics** that are neither a consumed input nor a produced path (e.g. `first_run_of_set`, `pipeline_status`, `sequence_id`). Never put a path here. |

## Rules

- **`schema_version`** is a string. Currently `"2"`. `load_state` raises if a document has any other value — there is no migration path.
- **Workflow identity** (`workflow.{run, instrument, ipts}`) is metadata only; it has no effect on execution and is recorded for downstream artifacts.
- **`inputs`** are populated before any stage runs. They are immutable in practice — stages do not rewrite them.
- **Stage status invariant**: `status == "failed"` ⇔ a matching entry exists in `state.errors`. The merge layer enforces this.
- **`errors`** is append-only. Each entry is `{stage, message, exit_code}`.
- An `overall_status(state)` helper (computed, not stored) rolls the per-stage statuses up: `failed` if any stage failed, else `pending` if any is still pending, else `ok`.

## Identity (`workflow.*`)

| Key | Type | Set by | Notes |
|-----|------|--------|-------|
| `run` | int | `seed-config` / `yaml-parser` | Parsed from event-file basename. |
| `instrument` | string | `seed-config` | e.g. `"REF_L"`. |
| `ipts` | string | `seed-config` | e.g. `"IPTS-36897"`. Omitted if the path lacks an IPTS segment. |

## Operator inputs (`inputs.operator.*`)

Operator-supplied — the seed YAML/JSON.

| Key | Description |
|-----|-------------|
| `sequence_total` | Partials per complete measurement. |
| `prompt` | Short sample blurb consumed by `plan-data`. |
| `template_file` | Mantid reduction template. |
| `context_file` | Markdown context note for `plan-data`. |
| `output_directory` | Where this run's artifacts land. Also the default canonicalization prefix for `merge-in`. |
| `export_path` | Reserved for batch-export workflows. |
| `llm.{provider, model, base_url}` | LLM endpoint config consumed by `plan-data` (and `analyze-sample` via env). |

## Derived inputs (`inputs.derived.*`)

Populated at seed time by parsing the event-file path; immutable thereafter.

| Key | Description |
|-----|-------------|
| `nexus_file` | The single canonical name for the raw NeXus file. |
| `data_directory` | `dirname(nexus_file)`. |
| `ipts_shared_root` | The IPTS-shared root (`/SNS/<INSTR>/<IPTS>/shared`); used to resolve relative seed paths. |

## Per-stage records

The stage-specific keys recorded under each `stages.<stage>.{params, artifacts, info}` block depend on what the relevant tool emits in its manifest plus what the [merge-in adapter](../src/ndip_state/adapters.py) derives from the state. The shapes that result are documented inline in the JSON example above. The source of truth for what each tool emits is [`docs/tool-result-schema.md`](tool-result-schema.md).

Two notable derived params that the orchestrator (not the tool) adds:

- `reduction.params.template_sha256` — content hash of `inputs.operator.template_file`, computed at merge-in so the record reproduces even if the file is later modified.
- `assembly.params.{nexus_input, reduced_input, model_input}` — the resolved input paths the orchestrator handed to `data-assembler ingest` / `nr-isaac-format convert-ingest`, recorded so each stage's record is self-contained.

## Building a state from a flat dict

`build_state(flat)` is the constructor that `yaml-parser` and `seed-config`
use to translate operator-authored flat YAML/JSON into the structured document.
Known keys land in their target locations; unknown keys are dropped.

| Flat key | Destination |
|----------|-------------|
| `run` / `instrument` / `ipts` | `workflow.*` |
| `sequence_total` / `prompt` | `inputs.operator.*` |
| `template_file` / `context_file` / `output_directory` / `export_path` | `inputs.operator.*` |
| `llm_provider` / `llm_model` / `llm_base_url` | `inputs.operator.llm.{provider,model,base_url}` |
| `event_file` | `inputs.derived.nexus_file` (single canonical name) |
| `data_directory` / `ipts_shared_root` | `inputs.derived.*` |

Stage records always initialise to `{status: "pending", params: {}, artifacts: {}, info: {}}` — they are filled by merge-in as stages complete.

## CLI surface

`python -m ndip_state.state` exposes the two operations the orchestration uses:

```
project-out STAGE STATE_JSON
    Print shell-quoted CLI args for STAGE, built from the state.

merge-in STAGE STATE_IN RESULT_JSON EXIT_CODE STATE_OUT [--output-prefix DIR]
    Fold a tool-result manifest into the state and write STATE_OUT.
    Defaults --output-prefix to inputs.operator.output_directory so paths
    realpath'd through /gpfs symlinks are canonicalized back to /SNS.
```

`STAGE` is one of `reduction` / `plan` / `analyze` / `ingest` / `convert`. The
[`ndip-run`](../src/ndip_state/run.py) console script wraps both around a
foreign tool invocation; see [state-handling.md](state-handling.md).
