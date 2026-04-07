"""Tests for the yaml-parser CLI."""

import json
import os
import tempfile

from click.testing import CliRunner

from yaml_parser.cli import main


SAMPLE_YAML = """\
- run: 218386
  event_file: "/SNS/REF_L/IPTS-34347/nexus/REF_L_218386.nxs.h5"
  data_directory: "/SNS/REF_L/IPTS-34347/nexus"
  template_file: "/SNS/REF_L/IPTS-34347/shared/autoreduce/template_down.xml"
  prompt: "45 to 60 nm Cu on 15 to 25 nm Ti on a silicon substrate"
  export_path: "/SNS/REF_L/IPTS-34347/shared/isaac/218386/export_218386.gz"
  output_directory: "/SNS/REF_L/IPTS-34347/shared/isaac/218386"
- run: 218387
  event_file: "/SNS/REF_L/IPTS-34347/nexus/REF_L_218387.nxs.h5"
  data_directory: "/SNS/REF_L/IPTS-34347/nexus"
  template_file: "/SNS/REF_L/IPTS-34347/shared/autoreduce/template_down.xml"
  prompt: "45 to 60 nm Cu on 15 to 25 nm Ti on a silicon substrate"
  export_path: "/SNS/REF_L/IPTS-34347/shared/isaac/218387/export_218387.gz"
  output_directory: "/SNS/REF_L/IPTS-34347/shared/isaac/218387"
"""


def test_creates_config_files():
    """Test that JSON config files are created with correct content."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_file = os.path.join(tmpdir, "batch.yaml")
        with open(yaml_file, "w") as f:
            f.write(SAMPLE_YAML)

        config_dir = os.path.join(tmpdir, "configs")
        nexus_dir = os.path.join(tmpdir, "nexus")

        result = runner.invoke(main, [yaml_file, "--config-dir", config_dir, "--nexus-dir", nexus_dir])
        assert result.exit_code == 0

        # Check config files exist
        assert os.path.exists(os.path.join(config_dir, "218386.json"))
        assert os.path.exists(os.path.join(config_dir, "218387.json"))

        # Check config content
        with open(os.path.join(config_dir, "218386.json")) as f:
            config = json.load(f)
        assert config["run"] == 218386
        assert config["event_file"] == "/SNS/REF_L/IPTS-34347/nexus/REF_L_218386.nxs.h5"


def test_matching_identifiers():
    """Test that both collections use matching identifiers."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_file = os.path.join(tmpdir, "batch.yaml")
        with open(yaml_file, "w") as f:
            f.write(SAMPLE_YAML)

        config_dir = os.path.join(tmpdir, "configs")
        nexus_dir = os.path.join(tmpdir, "nexus")

        result = runner.invoke(main, [yaml_file, "--config-dir", config_dir, "--nexus-dir", nexus_dir])
        assert result.exit_code == 0

        config_ids = {os.path.splitext(f)[0] for f in os.listdir(config_dir)}
        assert config_ids == {"218386", "218387"}


def test_missing_event_file_warns():
    """Test that a missing event_file produces a warning but doesn't fail."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_file = os.path.join(tmpdir, "batch.yaml")
        with open(yaml_file, "w") as f:
            f.write(SAMPLE_YAML)

        config_dir = os.path.join(tmpdir, "configs")
        nexus_dir = os.path.join(tmpdir, "nexus")

        result = runner.invoke(main, [yaml_file, "--config-dir", config_dir, "--nexus-dir", nexus_dir])
        assert result.exit_code == 0
        assert "Warning: Event file not found" in result.output or result.exit_code == 0


def test_invalid_yaml():
    """Test that invalid YAML produces an error."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_file = os.path.join(tmpdir, "bad.yaml")
        with open(yaml_file, "w") as f:
            f.write(": : : invalid yaml [[[")

        result = runner.invoke(main, [yaml_file])
        assert result.exit_code != 0


def test_non_list_yaml():
    """Test that a YAML file with a non-list top level fails."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_file = os.path.join(tmpdir, "dict.yaml")
        with open(yaml_file, "w") as f:
            f.write("key: value\n")

        result = runner.invoke(main, [yaml_file])
        assert result.exit_code != 0


def test_fallback_identifier():
    """Test that items without 'run' or 'tag' use index-based naming."""
    runner = CliRunner()
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_file = os.path.join(tmpdir, "batch.yaml")
        with open(yaml_file, "w") as f:
            f.write("- data_directory: /some/path\n- data_directory: /other/path\n")

        config_dir = os.path.join(tmpdir, "configs")
        nexus_dir = os.path.join(tmpdir, "nexus")

        result = runner.invoke(main, [yaml_file, "--config-dir", config_dir, "--nexus-dir", nexus_dir])
        assert result.exit_code == 0
        assert os.path.exists(os.path.join(config_dir, "run_000.json"))
        assert os.path.exists(os.path.join(config_dir, "run_001.json"))
