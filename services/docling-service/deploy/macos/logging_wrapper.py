#!/usr/bin/env python3
"""Compatibility entry point for the pre-1.2 logging wrapper CLI."""

from __future__ import annotations

import sys

from lifecycle import main


if __name__ == "__main__":
    sys.exit(main(["legacy", *sys.argv[1:]]))
