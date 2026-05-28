# Tool-result manifest schema (`ndip-tool-result/1`)

This is the **neutral contract** between a pipeline tool and the NDIP
orchestrator. Each tool emits one small JSON file (conventionally
`result.json`) describing what it did, in **its own vocabulary** — never in
terms of the NDIP workflow-state schema. The orchestrator (`ndip-workflows`)
owns all schema knowledge: it projects a tool's CLI arguments out of the
workflow state ([projection](../src/ndip_state/projection.py)) and merges the
tool's manifest back into the state ([adapters](../src/ndip_state/adapters.py)).

A tool therefore needs to know **nothing** about the workflow state, the other
stages, or the `paths.*` / `stages.*` layout. It reads explicit CLI arguments
and writes a manifest. This is what lets the foreign container images
(`ghcr.io/mdoucet/analyzer`, `ghcr.io/isaac-neutrons/data-assembler`) drop their
vendored copies of `state.py` entirely.

## Shape

```json
{
  "tool": "simple-reduction",
  "tool_version": "0.5.0",
  "schema": "ndip-tool-result/1",
  "status": "ok",
  "exit_code": 0,
  "params": {
    "template_file": "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml",
    "template_sha256": "ab12…",
    "q_min": 0.005,
    "q_max": 0.2,
    "q_step": -0.02
  },
  "artifacts": {
    "partial_file": "/SNS/.../REFL_226642_3_226644_partial.txt",
    "combined_file": "/SNS/.../REFL_226642_combined_data_auto.txt"
  },
  "info": {
    "first_run_of_set": 226642
  },
  "messages": [
    {"level": "info", "text": "Mantid reduction completed"}
  ]
}
```

## Fields

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `schema` | string | yes | Envelope contract version. Currently `"ndip-tool-result/1"`. Bump only on a breaking change to *this* envelope shape — independent of the workflow-state `schema_version`. |
| `tool` | string | yes | The tool's own name (e.g. `simple-reduction`, `plan-data`). Lets the orchestrator dispatch to the right adapter and record exactly what ran. |
| `tool_version` | string | recommended | The tool's version, for provenance. |
| `status` | enum | yes | One of `ok`, `failed`, `skipped`, `dry-run`, `needs-reprocessing`. The adapter maps this to the stage status (see below). |
| `exit_code` | int | recommended | Process exit code. Recorded for provenance; the orchestrator's own captured `$?` is the backstop. |
| `params` | object | yes | The **resolved inputs the tool actually used**, keyed by the tool's own argument names. This is the provenance record — it is what lets the final workflow document reproduce the run. May be `{}`. |
| `artifacts` | object | yes | The **files / structured outputs the tool produced**, keyed by the tool's own names. Values are absolute paths (or nested objects, e.g. a `{table: path}` map). May be `{}`. |
| `info` | object | no | Scalar **diagnostics** that are neither a consumed input nor a produced path (e.g. `first_run_of_set`, `pipeline_status`, `sequence_id`). Never put a path here — paths the tool wrote go in `artifacts`; settings it consumed go in `params`. |
| `messages` | array | no | Human-facing log lines: `{"level": "info"\|"warning"\|"error", "text": "..."}`. The adapter folds `level == "error"` entries into the workflow state's `errors[]`. |

## Status mapping

The adapter maps a manifest `status` (and the captured exit code) onto the
workflow-state stage status:

| manifest `status` | stage status | notes |
|-------------------|--------------|-------|
| `ok`, `dry-run` | `ok` | stage succeeded (a dry-run is a successful no-op). |
| `skipped` | `skipped` | the orchestrator/tool intentionally did not run this stage. |
| `failed`, `needs-reprocessing` | `failed` | records an `errors[]` entry. |
| *(missing manifest)* | `failed` | if a tool exits without writing a manifest, the orchestrator synthesizes a `failed` manifest from the captured exit code — a stage never silently succeeds. |

A non-zero `exit_code` forces `failed` regardless of the reported `status`.

## Rules

- A tool **MUST** write its manifest even on failure (`status: "failed"`, with
  whatever partial `artifacts` it managed to produce). The orchestrator's
  exit-code capture is only a backstop for a missing file.
- Paths in `params` / `artifacts` are written **as the tool resolved them** —
  on SNS hosts `os.path.realpath` may turn `/SNS/<INSTR>` into
  `/gpfs/neutronsfs/instruments/<INSTR>`. The orchestrator canonicalizes them
  back to the operator-supplied prefix on merge (see
  [canonicalize.py](../src/ndip_state/canonicalize.py)); the tool does not need
  to.
- A tool never emits NDIP stage names (`reduction`, `analysis`, `assembly`) or
  the `paths.*` layout. The mapping from a tool's `artifacts`/`params` keys into
  the workflow state is the adapter's job alone.

## Who emits what

| Tool | Key `artifacts` | Key `params` (provenance) | `info` |
|------|-----------------|---------------------------|--------|
| `simple-reduction` | `partial_file`, `combined_file` | `template_file`, `template_sha256`, `q_min`, `q_max`, `q_step` | `first_run_of_set` |
| `plan-data` | `job_yaml` | `model_name`, `perform_assembly`, `create_model_ready` | `sequence_id`, `sequence_number`, `sequence_complete` |
| `analyze-sample` | `problem_json`, `results_dir`, `reports_dir`, `models_dir` | `model_name` | `pipeline_status`, `completed_stages` |
| `data-assembler ingest` | `ingest_dir`, `parquet_files` | `reduced_input`, `model_input`, `nexus_input` | `ingest_status` |
| `nr-isaac-format convert-ingest` | `isaac_record` | `ingest_dir`, `reduced_input`, `nexus_input` | `isaac_status` |
