"""Compatibility entrypoint for the Datahub plugin host."""
from datahub_plugin.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
