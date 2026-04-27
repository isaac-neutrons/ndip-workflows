"""
Command-line interface for yaml-parser.

Parses a master YAML file containing a list of run configurations and
produces a directory of individual JSON configuration files for use
as a Galaxy collection in a sub-workflow.
"""

import json
import os
import sys

import click
import yaml


@click.command()
@click.argument(
    'input_file',
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
)
@click.option(
    '--config-dir', '-c',
    type=click.Path(file_okay=False, resolve_path=True),
    default='config_outputs',
    help='Output directory for JSON config files (default: config_outputs)',
)
def main(input_file: str, config_dir: str) -> None:
    """
    Parse a batch YAML file into individual JSON configs.

    INPUT_FILE is a YAML file where the top-level element is a list of
    run configurations.

    \b
    Examples:
      yaml-parser batch.yaml
      yaml-parser batch.yaml --config-dir ./configs
    """
    os.makedirs(config_dir, exist_ok=True)

    with open(input_file, 'r') as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            click.echo(f"Error parsing YAML: {e}", err=True)
            sys.exit(1)

    # Support two formats:
    #   1. Plain list of run configs (legacy)
    #   2. Dict with optional 'common' defaults and a 'runs' list
    common: dict = {}
    if isinstance(data, dict):
        if 'runs' not in data:
            click.echo(
                "Error: Expected a 'runs' key when the top level of the YAML is a mapping.",
                err=True,
            )
            sys.exit(1)
        common = data.get('common', {})
        runs = data['runs']
    elif isinstance(data, list):
        runs = data
    else:
        click.echo(
            "Error: Expected the top level of the YAML to be a list or a mapping with 'runs'.",
            err=True,
        )
        sys.exit(1)

    if not isinstance(runs, list):
        click.echo("Error: 'runs' must be a list of job configurations.", err=True)
        sys.exit(1)

    config_count = 0

    for i, item in enumerate(runs):
        identifier = str(item.get('run', item.get('tag', f"run_{i:03d}")))

        # Merge common defaults; run-level keys take precedence
        config_data = {**common, **item}
        event_file = config_data.get('event_file')
        if event_file and 'input_file' not in config_data:
            config_data['input_file'] = event_file

        config_file = os.path.join(config_dir, f"{identifier}.json")
        with open(config_file, 'w') as out_f:
            json.dump(config_data, out_f, indent=2)
        config_count += 1

    click.echo(f"Processing complete! Created {config_count} configs.")
