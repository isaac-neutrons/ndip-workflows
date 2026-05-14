# Workflow state schema (v1)

A single JSON document flows through every Galaxy tool in the pipeline:

```
yaml_parser -> reduction -> simple_analyzer -> data_assembler
```

Each tool reads it via `config_json`, runs its underlying CLI, and writes an
updated copy as `updated_config`. The canonical Python implementation lives in
[`src/ndip_state/state.py`](../src/ndip_state/state.py); tool XMLs use the same
module via inlined configfiles.

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
    "data_directory":   "/SNS/REF_L/IPTS-36897/nexus",
    "output_directory": "/SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5",
    "template_file":    "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml",
    "context_file":     "/SNS/REF_L/IPTS-36897/shared/isaac/context.md",
    "event_file":       "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "input_file":       "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "raw_data":         "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "export_path":      "/SNS/REF_L/IPTS-36897/shared/isaac/export.gz"
  },
  "llm": {
    "provider": "local",
    "model":    "gpt-4",
    "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/"
  },
  "reduction": {
    "success":       true,
    "result_file":   "/.../REFL_226642_3_226644_partial.txt",
    "partial_file":  "/.../REFL_226642_3_226644_partial.txt",
    "combined_file": "/.../REFL_226642_combined_data_auto.txt",
    "metadata": {}
  },
  "analysis": {
    "success":          true,
    "model_name":       "Cu-D2O-226642",
    "problem_json":     "/.../results/Cu-D2O-226642/problem.json",
    "perform_assembly": true,
    "metadata": {}
  },
  "assembly": {
    "success":      true,
    "isaac_record": "/.../assembled/isaac_record_226644.json",
    "metadata": {}
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
  paths from here, not from flat top-level keys.
- **Stage blocks** (`reduction`, `analysis`, `assembly`) always carry at
  least `success` (`true`, `false`, or `null` for "not yet attempted") and
  `metadata` (an arbitrary dict for stage-specific extras). Stage-specific
  keys are documented below.
- **`errors`** is append-only. Each entry is
  `{"stage": "...", "message": "...", "exit_code": int|null}`.
- **Unknown top-level keys are preserved** through migration, so the schema
  can grow without losing forward-compatible data.

## Stage-specific keys

| Stage     | Key             | Source                                                      |
|-----------|-----------------|-------------------------------------------------------------|
| reduction | `result_file`   | `simple-reduction --json` summary, `partial_file`           |
| reduction | `partial_file`  | `simple-reduction --json` summary                           |
| reduction | `combined_file` | `simple-reduction --json` summary                           |
| analysis  | `model_name`    | `plan-data` YAML, top-level or `metadata.model_name`         |
| analysis  | `problem_json`  | `$OUTPUT_DIR/results/<model_name>/problem.json`             |
| analysis  | `perform_assembly` | `plan-data` YAML, `metadata.perform_assembly`            |
| assembly  | `isaac_record`  | `$OUTPUT_DIR/assembled/isaac_record_<run>.json`             |

## v0 -> v1 migration

`load_state()` accepts both shapes. The flat v0 form mapped keys directly at
the top level; v1 groups them. The mapping is:

| v0 (flat)          | v1                            |
|--------------------|-------------------------------|
| `data_directory`   | `paths.data_directory`        |
| `output_directory` | `paths.output_directory`      |
| `template_file`    | `paths.template_file`         |
| `context_file`     | `paths.context_file`          |
| `event_file`       | `paths.event_file`            |
| `input_file`       | `paths.input_file`            |
| `raw_data`         | `paths.raw_data`              |
| `export_path`      | `paths.export_path`           |
| `llm_provider`     | `llm.provider`                |
| `llm_model`        | `llm.model`                   |
| `llm_base_url`     | `llm.base_url`                |
| `result_file`      | `reduction.result_file`       |
| `partial_file`     | `reduction.partial_file`      |
| `combined_file`    | `reduction.combined_file`     |
| `model_available`  | `analysis.success`            |
| `final_model`      | `analysis.problem_json`       |

`run`, `sequence_total`, and `prompt` stay at the top level. Any other key is
preserved at the top level so unrecognized data round-trips cleanly.

## Env-var emission

`emit_env(state, path)` writes a `_env.sh` that Galaxy tool XMLs can source.
The variable names are stable across tools so the same `_env.sh` works for
every stage:

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
| `REFLECTIVITY_FILE` | `reduction.result_file`                      |
| `PARTIAL_FILE`      | `reduction.partial_file`                     |
| `COMBINED_FILE`     | `reduction.combined_file`                    |
| `MODEL_NAME`        | `analysis.model_name`                        |
| `FINAL_MODEL`       | `analysis.problem_json`                      |
| `MODEL_AVAILABLE`   | `"1"` if `analysis.success`, else `"0"`      |
| `ISAAC_RECORD`      | `assembly.isaac_record`                      |

Values are quoted with `shlex.quote`.

## CLI subcommands

`python -m ndip_state.state` exposes the four operations Galaxy tools call:

```
parse-config CONFIG_JSON ENV_OUT
merge-reduction CONFIG_JSON SUMMARY_JSON OUT_JSON
merge-analyzer CONFIG_JSON EXIT_CODE MODEL_NAME PROBLEM_JSON OUT_JSON
merge-assembler CONFIG_JSON EXIT_CODE ISAAC_RECORD OUT_JSON
```

When `CONFIG_JSON` is an empty string, an empty v1 state is used as the seed.
