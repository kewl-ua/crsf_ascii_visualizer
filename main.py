#!/usr/bin/env python3
"""Compatibility entry point; prefer the installed crsf-viz command."""

from crsf_ascii_visualizer.cli import *  # Preserve imports from the old script.
from crsf_ascii_visualizer.cli import run


if __name__ == "__main__":
    raise SystemExit(run())
