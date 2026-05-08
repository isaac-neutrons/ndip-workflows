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
