"""The current command entry delegates to the revisioned local backend client.

The legacy direct-database CLI remains in the untouched v0.3 development tree.
"""
from .automation.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
