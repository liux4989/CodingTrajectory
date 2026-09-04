"""Direct script entry point for the Datahub web server."""
from datahub_plugin.serving.server import main

if __name__ == "__main__":
    raise SystemExit(main())
