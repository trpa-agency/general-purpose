"""Convenience wrapper: dump every configured SDE workspace, then render ERDs.

Equivalent to:
    python dump_sde_schema.py
    python build_erd.py
"""
from __future__ import annotations

import sys

import build_erd
import dump_sde_schema


def main() -> None:
    sys.argv = ["dump_sde_schema.py"]
    dump_sde_schema.main()
    sys.argv = ["build_erd.py"]
    build_erd.main()


if __name__ == "__main__":
    main()
