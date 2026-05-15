# Workflow state schema (v1)

A single JSON document flows through every Galaxy tool in the pipeline:

```
seed_config | yaml_parser  →  reduction  →  simple_analyzer  →  data_assembler
```

Each tool reads it via `config_json`, runs its underlying CLI, and writes an
updated copy as `updated_config`. The canonical Python implementation lives in
[`src/ndip_state/state.py`](../src/ndip_state/state.py); tool XMLs use a slim
inlined env-emitter copy of it ([`state_env`](../tools/reduction.xml) configfile
in each tool).

## Top-level shape

```json
{
  "schema_version": "1",
  "run": 226644,
  "instrument": "REF_L",
  "ipts": "IPTS-36897",
  "sequence_total": 3,
  "prompt": "Deposited 50 nm copper on 3 nm titanium on silicon, in D2O.",
  "paths": {
    "data_directory":     "/SNS/REF_L/IPTS-36897/nexus",
    "output_directory":   "/SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5",
    "template_file":      "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml",
    "context_file":       "/SNS/REF_L/IPTS-36897/shared/isaac/context.md",
    "event_file":         "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "input_file":         "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "raw_data":           "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "export_path":        "/SNS/REF_L/IPTS-36897/shared/isaac/export.gz",
    "assembled_directory":"/SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5/assembled"
  },
  "llm": {
    "provider": "local",
    "model":    "gpt-4",
    "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/"
  },
  "reduction": {
    "success":       true,
    "partial_file":  "/.../REFL_226642_3_226644_partial.txt",
    "combined_file": "/.../REFL_226642_combined_data_auto.txt",
    "metadata": {
      "first_run_of_set": 226642
    }
  },
  "analysis": {
    "success":          true,
    "model_name":       "Cu-D2O-226642",
    "problem_json":     "/.../results/Cu-D2O-226642/problem.json",
    "perform_assembly": true,
    "metadata": {
      "job_yaml":          "/.../plan/job_Cu-D2O-226642.yaml",
      "sequence_id":       "Cu-D2O-226642",
      "sequence_number":   3,
      "sequence_complete": true,
      "create_model_ready":true,
      "pipeline_status":   "ok",
      "completed_stages":  ["partial", "fit"],
      "results_dir":       "/.../results",
      "reports_dir":       "/.../reports",
      "models_dir":        "/.../models"
    }
  },
  "assembly": {
    "success":      true,
    "isaac_record": "/.../assembled/isaac_record_226644.json",
    "metadata": {
      "ingest_dir":    "/.../assembled",
      "ingest_status": "completed",
      "parquet_files": {
        "reflectivity":       "/.../reflectivity/.../226644.parquet",
        "sample":             "/.../sample/<uuid>.parquet",
        "reflectivity_model": "/.../reflectivity_model/<uuid>.parquet"
      },
      "isaac_status":  "converted"
    }
  },
  "errors": []
}
```

## Rules

- **`schema_version`** is a string. The current value is `"1"`. Bump only on
  breaking changes; additive fields stay within v1.
- **`run` / `instrument` / `ipts`** are optional top-level identifiers
  populated by `seed-config` (or `yaml-parser`) from the event file path.
  They have no effect on workflow execution; downstream tools use them as
  metadata in records like the ISAAC report.
- **`paths.*`** holds every filesystem path the workflow refers to. Tools read
  paths from here, never from flat top-level keys.
- **Stage blocks** (`reduction`, `analysis`, `assembly`) always carry at
  least `success` (`true`, `false`, or `null` for "not yet attempted") and
  `metadata` (an arbitrary dict for stage-specific extras). Stage-specific
  keys are documented below.
- **`errors`** is append-only. Each entry is
  `{"stage": "...", "message": "...", "exit_code": int|null}`.
- **Unknown top-level keys are preserved** so the schema can grow without
  losing forward-compatible data.

## Top-level fields

| Key              | Type     | Set by                       | Notes                                              |
|------------------|----------|------------------------------|----------------------------------------------------|
| `schema_version` | string   | `empty_state`                | Always `"1"`.                                      |
| `run`            | int      | `seed-config` / `yaml-parser`| Parsed from event-file basename.                   |
| `instrument`     | string   | `seed-config`                | e.g. `"REF_L"`.                                    |
| `ipts`           | string   | `seed-config`                | e.g. `"IPTS-36897"`. Omitted if path lacks one.    |
| `sequence_total` | int      | operator seed                | Partials per complete measurement.                 |
| `prompt`         | string   | operator seed (optional)     | Short sample blurb.                                |

## Path keys (`paths.*`)

| Key                    | Set by                         | Description                                                   |
|------------------------|--------------------------------|---------------------------------------------------------------|
| `data_directory`       | `seed-config`                  | `dirname(event_file)`.                                        |
| `output_directory`     | operator seed                  | Where this run's artifacts land.                              |
| `template_file`        | operator seed                  | Mantid reduction template.                                    |
| `context_file`         | operator seed                  | Markdown context consumed by `plan-data`.                     |
| `event_file`           | `seed-config`                  | Absolute NeXus path.                                          |
| `input_file`           | `seed-config`                  | Alias of `event_file` (some tools still read this name).      |
| `raw_data`             | `simple-reduction`             | Same as `event_file`, but written by the reducer for the assembler. |
| `export_path`          | operator seed (optional)       | Reserved for batch-export workflows.                          |
| `assembled_directory`  | `data-assembler ingest`        | The `<output_directory>/assembled` parquet bundle root.       |

## Stage-specific keys

### `reduction.*`
| Key              | Set by                          | Description                                                   |
|------------------|---------------------------------|---------------------------------------------------------------|
| `success`        | `simple-reduction`              | `true` once Mantid reduction completes.                       |
| `partial_file`   | `simple-reduction`              | The reduced reflectivity `.txt` for this run.                 |
| `combined_file`  | `simple-reduction`              | The cross-set combined reflectivity `.txt`.                   |
| `metadata.first_run_of_set` | `simple-reduction`   | Run number that anchors the partial-file naming scheme.       |

### `analysis.*`
| Key                              | Set by              | Description                                                 |
|----------------------------------|---------------------|-------------------------------------------------------------|
| `success`                        | `analyze-sample`    | `true` if pipeline status was `ok` / `dry-run`.             |
| `model_name`                     | `plan-data`         | Sample/sequence tag; drives results-dir naming.             |
| `problem_json`                   | `analyze-sample`    | `<results_dir>/<model_name>/problem.json` when fit ran.     |
| `perform_assembly`               | `plan-data`         | Whether the planner says assembly should run.               |
| `metadata.job_yaml`              | `plan-data`         | Path of the generated job YAML.                             |
| `metadata.sequence_id`           | `plan-data`         | LLM's verdict on which measurement series this run is part of. |
| `metadata.sequence_number`       | `plan-data`         | Position within that series.                                |
| `metadata.sequence_complete`     | `plan-data`         | Whether the set has all parts.                              |
| `metadata.create_model_ready`    | `plan-data`         | Whether the job YAML has fittable model info.               |
| `metadata.pipeline_status`       | `analyze-sample`    | One of `ok`, `dry-run`, `needs-reprocessing`, `failed`.     |
| `metadata.completed_stages`      | `analyze-sample`    | List of stages that ran (`partial`, `fit`, …).              |
| `metadata.results_dir`           | `analyze-sample`    | Absolute results root for the run.                          |
| `metadata.reports_dir`           | `analyze-sample`    | Absolute reports root for the run.                          |
| `metadata.models_dir`            | `analyze-sample`    | Absolute models-script root for the run.                    |

### `assembly.*`
| Key                          | Set by                          | Description                                              |
|------------------------------|---------------------------------|----------------------------------------------------------|
| `success`                    | `nr-isaac-format convert-ingest`| `true` once an ISAAC record is written.                  |
| `isaac_record`               | `nr-isaac-format convert-ingest`| Path to the `isaac_record_<run>.json` produced.          |
| `metadata.ingest_dir`        | `data-assembler ingest`         | Same as `paths.assembled_directory`.                     |
| `metadata.ingest_status`     | `data-assembler ingest`         | `"completed"` once the parquet bundle is written.        |
| `metadata.parquet_files`     | `data-assembler ingest`         | `{table_name: path}` map for every parquet output.       |
| `metadata.isaac_status`      | `nr-isaac-format convert-ingest`| `"converted"` once the ISAAC record is written.          |

## Env-var emission

`emit_env(state, path)` writes a `_env.sh` that Galaxy tool XMLs source. The
variable names are stable across tools so the same `_env.sh` works for every
stage:

| Variable            | Source                                       |
|---------------------|----------------------------------------------|
| `EVENT_FILE`        | `paths.event_file`                           |
| `INPUT_FILE`        | `paths.input_file` or `paths.event_file`     |
| `TEMPLATE`          | `paths.template_file`                        |
| `DATA_DIR`          | `paths.data_directory`                       |
| `OUTPUT_DIR`        | `paths.output_directory`                     |
| `CONTEXT_FILE`      | `paths.context_file`                         |
| `RAW_DATA`          | `paths.raw_data` or `paths.event_file`       |
| `EXPORT_PATH`       | `paths.export_path`                          |
| `PROMPT`            | `prompt`                                     |
| `SEQUENCE_TOTAL`    | `sequence_total`                             |
| `LLM_PROVIDER`      | `llm.provider`                               |
| `LLM_MODEL`         | `llm.model`                                  |
| `LLM_BASE_URL`      | `llm.base_url`                               |
| `REFLECTIVITY_FILE` | `reduction.partial_file`                     |
| `PARTIAL_FILE`      | `reduction.partial_file`                     |
| `COMBINED_FILE`     | `reduction.combined_file`                    |
| `MODEL_NAME`        | `analysis.model_name`                        |
| `FINAL_MODEL`       | `analysis.problem_json`                      |
| `MODEL_AVAILABLE`   | `"1"` if `analysis.success`, else `"0"`      |
| `ISAAC_RECORD`      | `assembly.isaac_record`                      |

Values are quoted with `shlex.quote`.

## Building a state from a flat dict

`build_state(flat)` is the constructor that `yaml-parser` and `seed-config`
use to translate operator-authored flat YAML / JSON into a v1 state. Known
keys land in their target blocks (`paths.*`, `llm.*`); unknown top-level keys
are preserved verbatim:

| Flat key           | v1 location                |
|--------------------|----------------------------|
| `data_directory`   | `paths.data_directory`     |
| `output_directory` | `paths.output_directory`   |
| `template_file`    | `paths.template_file`      |
| `context_file`     | `paths.context_file`       |
| `event_file`       | `paths.event_file`         |
| `input_file`       | `paths.input_file`         |
| `raw_data`         | `paths.raw_data`           |
| `export_path`      | `paths.export_path`        |
| `llm_provider`     | `llm.provider`             |
| `llm_model`        | `llm.model`                |
| `llm_base_url`     | `llm.base_url`             |

`run`, `sequence_total`, `prompt`, `instrument`, `ipts`, and any other
operator-supplied scalar stay at the top level. Stage-output keys
(`partial_file`, `problem_json`, `isaac_record`, …) are written by the CLIs
themselves and should not appear in seeds.

## CLI subcommands

`python -m ndip_state.state` exposes the four operations Galaxy tools call:

```
parse-config CONFIG_JSON ENV_OUT
merge-reduction CONFIG_JSON SUMMARY_JSON OUT_JSON
merge-analyzer CONFIG_JSON EXIT_CODE MODEL_NAME PROBLEM_JSON OUT_JSON
merge-assembler CONFIG_JSON EXIT_CODE ISAAC_RECORD OUT_JSON
```

When `CONFIG_JSON` is an empty string, an empty v1 state is used as the seed.
