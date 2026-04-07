"""
Command-line interface for yaml-parser.

Parses a master YAML file containing a list of run configurations and
produces two output directories:
  - A directory of NeXus file symlinks/copies
  - A directory of individual JSON configuration files

Both directories use matching identifiers so Galaxy can pair them as
parallel collections for a sub-workflow.
"""

import json
import os
import shutil
import sys

import click
import yaml


@click.command()
@click.argument(
    'input_file',
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
)
@click.option(
    '--nexus-dir', '-n',
    type=click.Path(file_okay=False, resolve_path=True),
    default='nexus_outputs',
    help='Output directory for NeXus file links (default: nexus_outputs)',
)
@click.option(
    '--config-dir', '-c',
    type=click.Path(file_okay=False, resolve_path=True),
    default='config_outputs',
    help='Output directory for JSON config files (default: config_outputs)',
)
def main(input_file: str, nexus_dir: str, config_dir: str) -> None:
    """
    Parse a batch YAML file into NeXus file links and JSON configs.

    INPUT_FILE is a YAML file where the top-level element is a list of
    run configurations, each containing at least an 'event_file' path
    and a 'run' identifier.

    \b
    Examples:
      yaml-parser batch.yaml
      yaml-parser batch.yaml --nexus-dir ./nexus --config-dir ./configs
    """
    os.makedirs(nexus_dir, exist_ok=True)
    os.makedirs(config_dir, exist_ok=True)

    with open(input_file, 'r') as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            click.echo(f"Error parsing YAML: {e}", err=True)
            sys.exit(1)

    if not isinstance(data, list):
        click.echo(
            "Error: Expected the top level of the YAML to be a list of job configurations.",
            err=True,
        )
        sys.exit(1)

    nexus_count = 0
    config_count = 0

    for i, item in enumerate(data):
        identifier = str(item.get('run', item.get('tag', f"run_{i:03d}")))

        # Write JSON config
        config_file = os.path.join(config_dir, f"{identifier}.json")
        with open(config_file, 'w') as out_f:
            json.dump(item, out_f, indent=2)
        config_count += 1

        # Link or copy NeXus file
        event_file = item.get('event_file')
        if event_file:
            nexus_file = os.path.join(nexus_dir, f"{identifier}.nxs.h5")
            if os.path.exists(event_file):
                try:
                    os.symlink(event_file, nexus_file)
                except (OSError, NotImplementedError):
                    shutil.copy2(event_file, nexus_file)
                nexus_count += 1
            else:
                click.echo(f"Warning: Event file not found: {event_file}", err=True)
        else:
            click.echo(f"Warning: No event_file specified for item {identifier}", err=True)

    click.echo(f"Processing complete! Created {config_count} configs and {nexus_count} NeXus links.")
