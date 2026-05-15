# State handling, end to end

The pipeline threads a single versioned JSON document — the v1 workflow
state — through every Galaxy tool. This page walks one real-world run
from the IPTS-36897 sample-5 experiment to show exactly what each tool
reads from state and what it writes back.

For the schema itself, see [state-schema.md](state-schema.md). The
canonical Python implementation is in
[`src/ndip_state/state.py`](../src/ndip_state/state.py).

## The chain

```
seed_config | yaml_parser  ──▶ reduction.xml ──▶ simple_analyzer.xml ──▶ data_assembler.xml
       v1 seed                  reduction.*        analysis.*               assembly.*
```

Each downstream tool accepts `config_json` (a v1 state) and emits
`updated_config` (the same state plus its own stage block). Underlying
CLIs (`simple-reduction`, `plan-data`, `analyze-sample`,
`data-assembler ingest`, `nr-isaac-format convert-ingest`) speak the
same contract via `--state-in PATH` / `--state-out PATH`, so an agent
can drive the pipeline directly without Galaxy when it needs to.

## Step 0 — produce the v1 seed

There are two entry points; pick whichever fits the situation.

**`seed-config`** (single run, on-demand): give it the run's event-file
path and a tiny seed (JSON or YAML) carrying just the things this tool
can't derive from the path. The new
[seed_config.xml](../tools/seed_config.xml) wraps it for Galaxy.

```yaml
# seed.yaml — required: template_file, output_directory, context_file, sequence_total
template_file:     autoreduce/template_down.xml
output_directory:  isaac/reduction/sample5
context_file:      isaac/context.md
sequence_total:    3
prompt:            "Deposited 50 nm Cu on 3 nm Ti on Si, in D2O. Back reflection."
```

```
seed-config /SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5 seed.yaml -o 226644.json
```

Relative paths in the seed resolve against the IPTS shared root parsed
from the event-file path
(`/SNS/<INSTRUMENT>/<IPTS>/shared`). Absolute paths pass through.

**`yaml-parser`** (batched runs): one YAML file describing multiple
runs, common defaults under `common:`. The
[yaml_parser.xml](../tools/yaml_parser.xml) wrapper splits it into a
Galaxy `Collection` of per-run JSON states.

```yaml
- run: 226644
  event_file:        /SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5
  data_directory:    /SNS/REF_L/IPTS-36897/nexus
  template_file:     /SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml
  context_file:      /SNS/REF_L/IPTS-36897/shared/isaac/context.md
  output_directory:  /SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5
  prompt:            "Deposited 50 nm Cu on 3 nm Ti on Si, in D2O. Back reflection."
  sequence_total:    3
  llm_provider:      local
  llm_model:         gpt-4
  llm_base_url:      https://aoai-eastus-bead.openai.azure.com/openai/v1/
```

Either way the result is the same shape — a v1 state. Per-run output
(`226644.json`):

```json
{
  "schema_version": "1",
  "run": 226644,
  "instrument": "REF_L",
  "ipts": "IPTS-36897",
  "sequence_total": 3,
  "prompt": "Deposited 50 nm Cu on 3 nm Ti on Si, in D2O. Back reflection.",
  "paths": {
    "data_directory": "/SNS/REF_L/IPTS-36897/nexus",
    "output_directory": "/SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5",
    "template_file": "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml",
    "context_file": "/SNS/REF_L/IPTS-36897/shared/isaac/context.md",
    "event_file": "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "input_file": "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
    "raw_data": "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5"
  },
  "llm": {
    "provider": "local",
    "model": "gpt-4",
    "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/"
  },
  "reduction": {"success": null, "metadata": {}},
  "analysis":  {"success": null, "metadata": {}},
  "assembly":  {"success": null, "metadata": {}},
  "errors": []
}
```

`instrument` and `ipts` come from `seed-config` parsing the event-file
path (`yaml-parser` leaves them off — operators can add them
explicitly in the batch YAML if needed). `paths.raw_data` is also
populated by `seed-config` ahead of the reducer, which `yaml-parser`
defers — both tools then converge after `reduction.xml` runs.

## Step 1 — reduction.xml

The tool inlines a slim
[`state_env`](../tools/reduction.xml) helper, sources its `_env.sh`
to get `OUTPUT_DIR` / `DATA_DIR` / `EVENT_FILE` / `TEMPLATE`, then runs:

```sh
simple-reduction \
    --output-dir "$OUTPUT_DIR" \
    --state-in   $config_json \
    --state-out  $updated_config
```

`simple-reduction` (in the analyzer image) reads `paths.event_file` /
`paths.template_file` / `paths.output_directory` from `--state-in`, runs
the Mantid reduction, then writes the same state back with a populated
`reduction` block:

```diff
   "reduction": {
-    "success": null, "metadata": {}
+    "success": true,
+    "partial_file":  "/.../sample5/REFL_226642_3_226644_partial.txt",
+    "combined_file": "/.../sample5/REFL_226642_combined_data_auto.txt",
+    "metadata": {"first_run_of_set": 226642}
   },
+  "paths": {
+    ...,
+    "raw_data": "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5"
+  }
```

The tool also emits the combined-reflectivity file as a separate Galaxy
dataset, but everything else (paths, success, run, `first_run_of_set`, etc.)
lives in `updated_config`. The `first_run_of_set` field used to surface
as a separate `identifier` Galaxy output; downstream tools that need it
should now read it from `state.reduction.metadata.first_run_of_set`.

## Step 2 — simple_analyzer.xml

Two CLI invocations, both speaking state:

### 2a · plan-data

```sh
plan-data "$REFLECTIVITY_FILE" "$CONTEXT_FILE" \
    --output-dir "$OUTPUT_DIR/plan" \
    --sequence-total "$SEQUENCE_TOTAL" \
    --state-in  $config_json \
    --state-out $PLAN_STATE
```

`plan-data` calls the LLM, drops a `job_<sequence_id>.yaml` in
`$OUTPUT_DIR/plan/`, and writes the planner outcome into state:

```diff
   "analysis": {
-    "success": null, "metadata": {}
+    "success": null,
+    "model_name":       "Cu-D2O-226642",
+    "perform_assembly": true,
+    "metadata": {
+      "job_yaml": "/.../sample5/plan/job_Cu-D2O-226642.yaml",
+      "sequence_id": "Cu-D2O-226642",
+      "sequence_number": 3,
+      "sequence_complete": true,
+      "create_model_ready": true
+    }
   },
```

The tool's bash extracts `analysis.perform_assembly` from `$PLAN_STATE`
with a one-line python and uses it as the gate for step 2b.

### 2b · analyze-sample

```sh
analyze-sample --no-reduction-gate \
    --state-in  $PLAN_STATE \
    --state-out $updated_config
```

`analyze-sample` reads `analysis.metadata.job_yaml` from state (so the
positional `CONFIG` arg is unneeded), derives `--results-dir` /
`--reports-dir` from `paths.output_directory`, runs the analyzer
pipeline, then writes:

```diff
   "analysis": {
-    "success": null,
+    "success": true,
+    "problem_json": "/.../sample5/results/Cu-D2O-226642/problem.json",
     "model_name": "Cu-D2O-226642",
     "perform_assembly": true,
     "metadata": {
       ...,
+      "pipeline_status": "ok",
+      "completed_stages": ["partial", "fit"],
+      "results_dir": "/.../sample5/results",
+      "reports_dir": "/.../sample5/reports",
+      "models_dir":  "/.../sample5/models"
     }
   },
```

If `perform_assembly` was false the tool skips `analyze-sample` and just
copies `$PLAN_STATE` into `updated_config` — `analysis.success` stays
`null`, which downstream reads as "not attempted".

## Step 3 — data_assembler.xml

Also two CLIs in a chain through an interim state file:

```sh
data-assembler ingest \
    -o "$ASSEMBLED_DIR" \
    --state-in  $config_json \
    --state-out $INGEST_STATE \
    [ -r $reflectivity ] [ -p parquet ] [ -m $model ]
```

`data-assembler ingest` fills `--reduced` from `reduction.partial_file`,
`--model` from `analysis.problem_json` (only when `analysis.success` is
`true`), and `--nexus-file` from `paths.raw_data`. After writing the
parquet bundle it records:

```diff
+  "paths": {
+    ...,
+    "assembled_directory": "/.../sample5/assembled"
+  },
   "assembly": {
-    "success": null, "metadata": {}
+    "success": null,
+    "metadata": {
+      "ingest_dir":      "/.../sample5/assembled",
+      "ingest_status":   "completed",
+      "parquet_files": {
+        "reflectivity":       "/.../assembled/reflectivity/.../226644.parquet",
+        "sample":             "/.../assembled/sample/<uuid>.parquet",
+        "reflectivity_model": "/.../assembled/reflectivity_model/<uuid>.parquet"
+      }
+    }
   }
```

Then:

```sh
nr-isaac-format convert-ingest "$ASSEMBLED_DIR" \
    --state-in  $INGEST_STATE \
    --state-out $updated_config
```

`convert-ingest` reads `assembly.metadata.ingest_dir`, `paths.raw_data`,
and `reduction.partial_file` from state, writes one
`isaac_record_<run>.json`, and finalizes:

```diff
   "assembly": {
-    "success": null,
+    "success": true,
+    "isaac_record": "/.../sample5/assembled/isaac_record_226644.json",
     "metadata": {
       "ingest_dir":   "...",
       "ingest_status": "completed",
+      "isaac_status":  "converted",
       "parquet_files": {...}
     }
   }
```

That's the terminal `updated_config`. The Galaxy tool also tars
`$ASSEMBLED_DIR` into `out_gz` for downstream consumers that prefer the
bundle to chasing paths.

## The final document

After all four tools, the same JSON that started with just `paths`,
`run`, and an LLM block now carries `reduction.success`,
`analysis.success`, `analysis.problem_json`, `assembly.success`, and
`assembly.isaac_record` — a complete provenance record of the run.

```json
{
  "schema_version": "1",
  "run": 226644,
  "instrument": "REF_L",
  "ipts": "IPTS-36897",
  "sequence_total": 3,
  "prompt": "...",
  "paths": {
    "data_directory":     "...",
    "output_directory":   "...",
    "template_file":      "...",
    "context_file":       "...",
    "event_file":         "...",
    "input_file":         "...",
    "raw_data":           "...",
    "assembled_directory": "..."
  },
  "llm": {"provider": "...", "model": "...", "base_url": "..."},
  "reduction": {
    "success": true, "partial_file": "...",
    "combined_file": "...", "metadata": {}
  },
  "analysis": {
    "success": true, "model_name": "Cu-D2O-226642",
    "problem_json": "/.../problem.json", "perform_assembly": true,
    "metadata": {"job_yaml": "...", "pipeline_status": "ok", ...}
  },
  "assembly": {
    "success": true,
    "isaac_record": "/.../isaac_record_226644.json",
    "metadata": {"ingest_dir": "...", "isaac_status": "converted", ...}
  },
  "errors": []
}
```

## Running the chain without Galaxy

Because every CLI accepts `--state-in / --state-out`, an agent (or a
shell script, or a notebook) can drive the pipeline directly. Starting
from just the event-file path and a small seed:

```bash
S=/tmp/state.json

seed-config /SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5 seed.yaml -o $S

simple-reduction              --state-in $S --state-out $S
plan-data                     --state-in $S --state-out $S
analyze-sample                --state-in $S --state-out $S --no-reduction-gate
data-assembler ingest         --state-in $S --state-out $S
nr-isaac-format convert-ingest --state-in $S --state-out $S
```

Every required path the CLIs need is in `$S` after `seed-config` runs,
so no extra positional arguments are needed. After each step `$S` is the
latest snapshot of the run. Failures are recorded as entries in
`state.errors[]` with a `stage` and `message`, and the corresponding
stage's `success` flips to `false` — no need to sift through exit
codes.

## Where things live

| Concern                              | File                                                  |
|--------------------------------------|-------------------------------------------------------|
| Schema definition                    | [docs/state-schema.md](state-schema.md)               |
| Canonical Python module              | [src/ndip_state/state.py](../src/ndip_state/state.py) |
| Slim env-emitter inlined in tool XMLs| `<configfile name="state_env">` in each `tools/*.xml` |
| `yaml-parser` → v1 seed (batched)    | [src/yaml_parser/cli.py](../src/yaml_parser/cli.py)   |
| `seed-config` → v1 seed (single run) | [src/yaml_parser/seed.py](../src/yaml_parser/seed.py) |
| CLI state IO (analyzer image)        | `analyzer_tools/state.py` in mdoucet/analyzer         |
| CLI state IO (assembler image)       | `assembler/state.py` in isaac-neutrons/data-assembler |
