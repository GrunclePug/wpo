"""Run the installed CLI with python -m wpo."""

from .cli import entrypoint

raise SystemExit(entrypoint())
