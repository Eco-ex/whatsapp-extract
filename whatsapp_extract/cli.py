"""CLI entry point: whatsapp-extract <subcommand> ..."""

from __future__ import annotations

from pathlib import Path

import click

from . import batcher, classifier, consolidator
from . import filter as filter_step
from . import parser as parser_step
from .config_loader import load_config


def _apply_overrides(cfg, batch_size: int | None, model: str | None):
    if batch_size is not None:
        cfg.llm.batch_size = batch_size
    if model is not None:
        cfg.llm.model = model
    return cfg


@click.group()
@click.version_option(package_name="whatsapp-extract")
def main():
    """Extract structured data from WhatsApp chat exports."""


@main.command("parse")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--input", "input_path", required=True, type=click.Path(exists=True, dir_okay=False))
def cmd_parse(config_path: str, input_path: str):
    """Parse a raw WhatsApp .txt export into messages.json."""
    cfg = load_config(config_path)
    parser_step.run(Path(input_path), cfg.parsed_dir)


@main.command("filter")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False))
def cmd_filter(config_path: str):
    """Apply noise filters to parsed messages."""
    cfg = load_config(config_path)
    filter_step.run(cfg)


@main.command("batch")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--batch-size", type=int, default=None, help="Override config batch size")
def cmd_batch(config_path: str, batch_size: int | None):
    """Split filtered messages into batch files."""
    cfg = load_config(config_path)
    cfg = _apply_overrides(cfg, batch_size, None)
    batcher.run(cfg)


@main.command("classify")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--model", default=None, help="Override Claude model")
def cmd_classify(config_path: str, model: str | None):
    """Send batches to the Claude API and save per-batch results."""
    cfg = load_config(config_path)
    cfg = _apply_overrides(cfg, None, model)
    classifier.run(cfg)


@main.command("consolidate")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False))
def cmd_consolidate(config_path: str):
    """Merge per-batch results into deduplicated CSV/JSON output."""
    cfg = load_config(config_path)
    consolidator.run(cfg)


@main.command("run")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--input", "input_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Run parse + filter only (no API calls)")
@click.option("--batch-size", type=int, default=None, help="Override config batch size")
@click.option("--model", default=None, help="Override Claude model")
def cmd_run(config_path: str, input_path: str, dry_run: bool, batch_size: int | None, model: str | None):
    """Run the full pipeline."""
    cfg = load_config(config_path)
    cfg = _apply_overrides(cfg, batch_size, model)

    parser_step.run(Path(input_path), cfg.parsed_dir)
    filter_step.run(cfg)
    if dry_run:
        click.echo("[run] --dry-run: stopping after filter step.")
        return
    batcher.run(cfg)
    classifier.run(cfg)
    consolidator.run(cfg)


if __name__ == "__main__":
    main()
