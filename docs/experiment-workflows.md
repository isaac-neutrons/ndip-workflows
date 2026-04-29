# Workflow Variations

## Workflow 1: Starting an experiment
The initial hours of an experiment are focused on setting up. The first runs cannot be automatically 
processed because the automated reduction can only be set up with actual data.

Notes are generally written down at the end of the set up process.

**Output from this workflow**:
- An XML template file for data reduction
- Notes about the first data set, which is usually the first of a series for that sample.


## Workflow 2: Daytime experiment
Once the experiment is set up and we have a reduction template, we can start automating the pipeline.
During the day, there is often some amount of troubleshooting and decisions being made about the direction
of the experiment. For this reason, although the data reduction can be automated, notes can still be written 
out-of-sync and after the fact.


## Workflow 3: Nightshift
Night shifts are usually predictable by design. Note taking could be done in advance, but we can assign
text to run numbers.


# Workflow Pipeline

**Input**: YAML that outlines where the data is and the tools to use. Could be json?

```yaml
common:
  data_directory: "/SNS/REF_L/IPTS-36897/nexus"
  runs_per_measurement: 3
  template_file: "/SNS/REF_L/IPTS-36897/shared/autoreduce/template_down.xml"
  context_directory: "/SNS/REF_L/IPTS-36897/shared/isaac/context"
  prompt: "About 50 nm ionomer on 15 nm platinum on 3 nm Ti on a silicon substrate, in either air or D2O (ambient medium). The incoming comes from the back (back reflection)."
  output_directory: "/SNS/REF_L/IPTS-36897/shared/isaac/"
  isaac_format: true
```

### Step 1: Parquet:
Run the `nexus-processor` tool. Return a yaml file with updated state.

**Ouput**: Input plus the following:
```yaml
  nexus_processor_success: true
  nexus_processor_metadata: {}
```

### Step 2: Reduction
Data reduction, using the specified template. Output should be a state file.

**Ouput**: Input plus the following:
```yaml
  reduction_success: true
  reduction_metadata: {}
```

### Step 3: Assess
Look at the reduced data to see if it represents a complete data set, using the runs_per_measurement parameter.  Look at the context file for the set ID. If it exists, and if the measurement is complete, create a YAML file to run `analyze-sample`.

**Ouput**: Input plus the following:
```yaml
  perform_analysis: true
  # Assembly should be performed when a set is complete,
  # but can be done even if we don't have the info to fit.
  perform_assembly: true
  create_model:
    describe: |
    2 nm CuOx / 50 nm Cu / 3 nm Ti on Si in 100 mM LiTFSI/THF.
    Neutrons enter from the silicon side.
    model_name: cu_thf_218281
    out: models/cu_thf_218281.py
    states:
    - name: state_218281
        data:
        - REFL_218281_1_218281_partial.txt
        - REFL_218281_2_218282_partial.txt
        - REFL_218281_3_218283_partial.txt
        theta_offset:      {init: 0.0, min: -0.02, max: 0.02}
        sample_broadening: {init: 0.0, min: 0.0, max: 0.05}

  assess_metadata: {}
```

### Step 4: Analysis
Extract the create-model section of the yaml file into model.yaml and run

`analyze-sample model.yaml --no-reduction-gate. --results-dir /SNS/.../analysis`

Probably need to modify `analyze-sample` to return a json/yaml state?

**Ouput**: Input plus the following:
```yaml
  analysis_success: true
  analysis_metadata: {}
```

### Step 5: Assembler
Assemble AI-ready data and ISAAC record.
May use the final state as metadata for the AI-ready records.