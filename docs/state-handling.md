# State handling, end to end

The pipeline threads a single JSON document — the workflow state — through
every Galaxy tool. This page walks one real-world run from the IPTS-36897
sample-5 experiment to show exactly what each tool reads from state and what
the orchestrator writes back.

For the schema itself, see [state-schema.md](state-schema.md). For the neutral
contract the tools emit, see [tool-result-schema.md](tool-result-schema.md).
The canonical Python implementation is in
[`src/ndip_state/state.py`](../src/ndip_state/state.py) and the per-tool
runtime bundle is [`tools/ndip_shim.py`](../tools/ndip_shim.py).

## The chain

```
seed_config | yaml_parser  ──▶ reduction.xml ──▶ simple_analyzer.xml ──▶ data_assembler.xml
       state seed              stages.reduction    stages.analysis        stages.assembly
```

Each downstream tool takes `config_json` (the state) as input and writes an
`updated_config` with its stage record populated. The science tools
themselves know nothing about the schema — they take explicit CLI flags and
emit an [`ndip-tool-result/1`](tool-result-schema.md) manifest. The Galaxy
tool wrapper does the two halves around the call:

```
state ──[project-out]──▶ tool CLI args ──▶ [foreign tool] ──▶ result.json
                                                                  │
state ◀──[merge-in (+ canonicalize)]──────────────────────────────┘
```

Both halves live in [`tools/ndip_shim.py`](../tools/ndip_shim.py), inlined
into each XML via the generator [`tools/build_tool_xmls.py`](../tools/build_tool_xmls.py).

## Step 0 — produce the seed

There are two main entry points (plus two mid-pipeline `seed-config` modes,
below); pick whichever fits the situation.

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

Relative paths in the seed resolve against the IPTS shared root parsed from
the event-file path (`/SNS/<INSTRUMENT>/<IPTS>/shared`). Absolute paths pass
through.

**`yaml-parser`** (batched runs): one YAML file describing multiple runs,
common defaults under `common:`. The
[yaml_parser.xml](../tools/yaml_parser.xml) wrapper splits it into a Galaxy
`Collection` of per-run JSON states. [`example/batch.yaml`](../example/batch.yaml)
is a minimal demo input.

### Starting mid-pipeline

When the reduction (or planning) has already happened — e.g. local
re-analysis of an existing reflectivity file — `seed-config` has two
local-first modes that skip the earlier stages instead of reading a NeXus
event file:

```sh
# Already reduced: pre-fill stages.reduction so the analyzer runs plan-data directly.
seed-config seed.yaml --from-reduced REFL_226642_3_226644_partial.txt -o 226644.json

# Already planned: pre-fill stages.analysis.artifacts.job_yaml so the analyze
# step runs without re-planning.
seed-config seed.yaml --from-plan job_Cu-D2O-226642.yaml -o 226644.json
```

The seed needs only what that mode can't supply: `--from-reduced` wants
`output_directory` + `context_file` (plan-data still needs the context);
`--from-plan` wants just `output_directory`. There is no event file, so
`run` / `instrument` / `ipts` are optional metadata you can add for a complete
ISAAC record (no canonical `/SNS` paths are fabricated from them). Relative
paths resolve against the current directory; absolute paths pass through.

`--from-reduced` lands a complete `stages.reduction` (status `ok`,
`artifacts.partial_file` = your file), so it feeds
[simple_analyzer.xml](../tools/simple_analyzer.xml) or `ndip-run plan` /
`ndip-run analyze` unchanged. `--from-plan` lands `job_yaml` with the analysis
stage left `pending` (the fit hasn't run yet); consume it with
`ndip-run analyze` — the analyzer XMLs always run plan-data first, so they
would overwrite the supplied plan.

Either way the result is the same shape. Per-run output (`226644.json`):

```json
{
  "schema_version": "2",
  "workflow": {"run": 226644, "instrument": "REF_L", "ipts": "IPTS-36897"},
  "inputs": {
    "operator": {
      "sequence_total": 3,
      "prompt": "Deposited 50 nm Cu on 3 nm Ti on Si, in D2O. Back reflection.",
      "template_file":    "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml",
      "context_file":     "/SNS/REF_L/IPTS-36897/shared/isaac/context.md",
      "output_directory": "/SNS/REF_L/IPTS-36897/shared/isaac/reduction/sample5",
      "llm": {"provider": "local", "model": "gpt-4",
              "base_url": "https://aoai-eastus-bead.openai.azure.com/openai/v1/"}
    },
    "derived": {
      "nexus_file":       "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
      "data_directory":   "/SNS/REF_L/IPTS-36897/nexus",
      "ipts_shared_root": "/SNS/REF_L/IPTS-36897/shared"
    }
  },
  "stages": {
    "reduction": {"status": "pending", "params": {}, "artifacts": {}, "info": {}},
    "analysis":  {"status": "pending", "params": {}, "artifacts": {}, "info": {}},
    "assembly":  {"status": "pending", "params": {}, "artifacts": {}, "info": {}}
  },
  "errors": []
}
```

`yaml-parser` leaves `instrument`/`ipts` off (operators can add them
explicitly in the batch YAML if needed). Both seeds populate the same
v2 shape so downstream tools see one contract regardless of entry point.

## Step 1 — reduction.xml

```
ARGS=$(ndip_shim project-out reduction config_json)   # --event-file ... --template ... --output-dir ...
simple-reduction $ARGS --result-out result.json
ndip_shim merge-in reduction config_json result.json $? updated_config
```

`simple-reduction` (in the analyzer image) is schema-agnostic: it takes the
explicit flags, runs the Mantid reduction, and writes a result manifest of its
inputs and outputs. The `merge-in` adapter then folds that into the
`reduction` stage of state, blending in orchestrator-derived provenance (the
template content hash):

```diff
   "stages": {
     "reduction": {
-      "status": "pending", "params": {}, "artifacts": {}, "info": {}
+      "status": "ok",
+      "params": {
+        "template_file":   "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml",
+        "template_sha256": "ab12…",
+        "theta_offset":    0.0
+      },
+      "artifacts": {
+        "partial_file":  "/.../sample5/REFL_226642_3_226644_partial.txt",
+        "combined_file": "/.../sample5/REFL_226642_combined_data_auto.txt"
+      },
+      "info": {"first_run_of_set": 226642}
     }
   }
```

The tool also emits the combined-reflectivity file as a separate Galaxy
dataset; everything else lives in `updated_config`.

## Step 2 — simple_analyzer.xml

Two foreign CLIs in sequence, each wrapped in its own project-out / merge-in:

### 2a · plan-data

```
PLAN_ARGS=$(ndip_shim project-out plan state_in)
# positional partial_file context_file --output-dir <out>/plan --sequence-total 3 --llm-provider ... --llm-model ... --llm-base-url ...
plan-data $PLAN_ARGS --result-out plan_result.json
ndip_shim merge-in plan state_in plan_result.json $? plan_state.json
```

`plan-data` calls the LLM, drops a `job_<sequence_id>.yaml` in
`$OUTPUT_DIR/plan/`, and emits its outcome as a manifest. The adapter routes
that into `stages.analysis`:

```diff
   "stages": {
     "analysis": {
-      "status": "pending", "params": {}, "artifacts": {}, "info": {}
+      "status": "ok",
+      "params": {
+        "model_name":         "Cu-D2O-226642",
+        "perform_assembly":   true,
+        "create_model_ready": true
+      },
+      "artifacts": {"job_yaml": "/.../sample5/plan/job_Cu-D2O-226642.yaml"},
+      "info": {
+        "sequence_id":       "Cu-D2O-226642",
+        "sequence_number":   3,
+        "sequence_complete": true
+      }
     }
   }
```

The XML bash reads `stages.analysis.params.perform_assembly` via
`ndip_shim get` and gates the next step on it.

### 2b · analyze-sample

```
ANALYZE_ARGS=$(ndip_shim project-out analyze plan_state.json)
# positional job_yaml --results-dir <out>/results --reports-dir <out>/reports
analyze-sample --no-reduction-gate $ANALYZE_ARGS --result-out analyze_result.json
ndip_shim merge-in analyze plan_state.json analyze_result.json $? updated_config
```

`analyze-sample` runs the analyzer pipeline; its manifest maps `status` enum
values (`ok` / `dry-run` / `needs-reprocessing` / `failed`) which the adapter
translates to the stage status. After merging:

```diff
   "stages": {
     "analysis": {
       "status": "ok",
       "params": { "model_name": "Cu-D2O-226642", "perform_assembly": true, ... },
       "artifacts": {
         "job_yaml": "...",
+        "problem_json": "/.../sample5/results/Cu-D2O-226642/problem.json",
+        "results_dir":  "/.../sample5/results",
+        "reports_dir":  "/.../sample5/reports",
+        "models_dir":   "/.../sample5/models"
       },
       "info": {
         ...,
+        "pipeline_status":  "ok",
+        "completed_stages": ["partial", "fit"]
       }
     }
   }
```

If `perform_assembly` was false, the XML skips `analyze-sample` and copies the
plan state to `updated_config`; `stages.analysis.status` stays `pending` and
`analysis.artifacts.problem_json` is absent — downstream reads that as "not
attempted".

## Step 3 — data_assembler.xml

Also two foreign CLIs chained through an interim state file:

### 3a · data-assembler ingest

```
INGEST_ARGS=$(ndip_shim project-out ingest config_json)
# -o <out>/assembled --reduced ... --nexus-file ... [--model ... when analysis.status==ok]
data-assembler ingest $INGEST_ARGS --result-out ingest_result.json
ndip_shim merge-in ingest config_json ingest_result.json $? ingest_state.json
```

The adapter writes the parquet bundle's outputs into `stages.assembly` and
records the resolved inputs as orchestrator-derived params:

```diff
   "stages": {
     "assembly": {
-      "status": "pending", "params": {}, "artifacts": {}, "info": {}
+      "status": "ok",
+      "params": {
+        "nexus_input":   "/SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5",
+        "reduced_input": "/.../sample5/REFL_226642_3_226644_partial.txt",
+        "model_input":   "/.../sample5/results/Cu-D2O-226642/problem.json"
+      },
+      "artifacts": {
+        "ingest_dir": "/.../sample5/assembled",
+        "parquet_files": {
+          "reflectivity":       "/.../assembled/reflectivity/.../226644.parquet",
+          "sample":             "/.../assembled/sample/<uuid>.parquet",
+          "reflectivity_model": "/.../assembled/reflectivity_model/<uuid>.parquet"
+        }
+      },
+      "info": {"ingest_status": "completed"}
     }
   }
```

### 3b · nr-isaac-format convert-ingest

```
CONVERT_ARGS=$(ndip_shim project-out convert ingest_state.json)
# positional ingest_dir --raw <nexus> --reduced <partial>
nr-isaac-format convert-ingest $CONVERT_ARGS --result-out convert_result.json
ndip_shim merge-in convert ingest_state.json convert_result.json $? updated_config
```

`convert-ingest` produces one `isaac_record_<run>.json`; the adapter finalises
the assembly stage:

```diff
   "stages": {
     "assembly": {
       "status": "ok",
       "params":    { ... },
       "artifacts": {
         "ingest_dir": "...",
         "parquet_files": {...},
+        "isaac_record": "/.../sample5/assembled/isaac_record_226644.json"
       },
       "info": {
         "ingest_status": "completed",
+        "isaac_status":  "converted"
       }
     }
   }
```

The Galaxy tool also tars `$ASSEMBLED_DIR` into `out_gz` for downstream
consumers that prefer the bundle to chasing paths.

## The final document

After the chain, the same JSON that started with just identity and inputs now
carries an `ok` status for every stage, plus the params and artifacts each
recorded. A representative shape lives at [`example/seed.json`](../example/seed.json);
the canonical schema is at [state-schema.md](state-schema.md).

Failure is recorded explicitly. A failing stage's `status` flips to `"failed"`
and an entry is appended to `state.errors` — no need to sift through exit codes.
A computed `overall_status(state)` helper rolls everything up: `failed` if any
stage failed, else `pending` if any is still pending, else `ok`.

## Running the chain without Galaxy

The `ndip-run` console script wraps project-out → tool → merge-in for any
single host where the tool binaries are on `$PATH` (install them with
`pip install '.[workflow]'`). Each stage has a default `--tool-cmd`, so the
agent never needs to know any tool's name or argument surface:

```bash
S=/tmp/state.json

seed-config /SNS/REF_L/IPTS-36897/nexus/REF_L_226644.nxs.h5 seed.yaml -o $S

ndip-run reduction --state $S      # default --tool-cmd 'simple-reduction'
ndip-run plan      --state $S      # 'plan-data'
ndip-run analyze   --state $S      # 'analyze-sample --no-reduction-gate'
ndip-run ingest    --state $S      # 'data-assembler ingest'
ndip-run convert   --state $S      # 'nr-isaac-format convert-ingest'
```

Pass `--tool-cmd` to override any default.

**Two analyzer backends.** The analyze stage matches the two Galaxy analyzer
tools. `--analyzer simple` (default) runs `analyze-sample`
([simple_analyzer.xml](../tools/simple_analyzer.xml)); `--analyzer aure` runs
the agentic AuRE analyzer ([analyzer.xml](../tools/analyzer.xml)):

```bash
ndip-run analyze --state $S --analyzer aure     # 'aure analyze -c JOB -o RESULTS'
ndip-run all     --state $S --analyzer aure     # aure for the analyze step of the chain
```

AuRE has a different CLI (`-c`/`-o`), reads the LLM endpoint from the
environment (`ndip-run` exports it from `inputs.operator.llm`), and emits no
result manifest — `ndip-run` stages the plan next to the reduced data and
synthesizes the analyze manifest from the `problem.json` AuRE drops, matching
what `analyzer.xml` does under Galaxy.

**Mantid-free local run.** Reduction needs Mantid; the other stages don't. Seed
past reduction from an existing partial file and chain the rest in one shot:

```bash
seed-config seed.yaml --from-reduced REFL_226642_3_226644_partial.txt -o $S
ndip-run all --state $S            # plan -> analyze -> ingest -> convert
```

`ndip-run all` runs the downstream stages in order and stops on the first
failure; it excludes reduction unless you pass `--include-reduction` (which
needs the full Mantid image and an event file).

After each step `$S` is the latest snapshot. For manual control, the
individual halves are also callable: `python -m ndip_state.state project-out
STAGE STATE` and `python -m ndip_state.state merge-in STAGE STATE_IN RESULT
EXIT_CODE STATE_OUT [--output-prefix DIR]`.

## Packaging a reproducible record

Once the chain is done, `ndip-package --state $S -o <dir>` reads the final state
and gathers the scattered analysis artifacts into one git-storable
**provenance package** — inputs, plan, model (or AuRE `checkpoints/` trail),
compact fit results, reports, the AI record, plus a `MANIFEST.json` (per-file
role + sha256 + tool versions, now including each stage's
`info.tool_versions`) and a `REPRODUCE.md` runbook. Large binaries (raw NeXus,
parquet) and bulky regenerable byproducts are recorded by reference. It handles
both `--analyzer` backends.

## Where things live

| Concern | File |
|---------|------|
| Schema definition | [docs/state-schema.md](state-schema.md) |
| Tool-result envelope spec | [docs/tool-result-schema.md](tool-result-schema.md) |
| Canonical Python module | [src/ndip_state/state.py](../src/ndip_state/state.py) |
| project-out (state → tool args) | [src/ndip_state/projection.py](../src/ndip_state/projection.py) |
| merge-in (manifest → state) | [src/ndip_state/adapters.py](../src/ndip_state/adapters.py) |
| Path canonicalisation | [src/ndip_state/canonicalize.py](../src/ndip_state/canonicalize.py) |
| Agent-driven runner | [src/ndip_state/run.py](../src/ndip_state/run.py) (`ndip-run`) |
| Provenance packager | [src/ndip_state/package.py](../src/ndip_state/package.py) (`ndip-package`) |
| Inlined runtime shim | [tools/ndip_shim.py](../tools/ndip_shim.py) + [tools/build_tool_xmls.py](../tools/build_tool_xmls.py) |
| Tool XML templates | `tools/*.xml.in` (generator inlines the shim → `tools/*.xml`) |
| `yaml-parser` → batched seeds | [src/yaml_parser/cli.py](../src/yaml_parser/cli.py) |
| `seed-config` → single-run seed | [src/yaml_parser/seed.py](../src/yaml_parser/seed.py) |
