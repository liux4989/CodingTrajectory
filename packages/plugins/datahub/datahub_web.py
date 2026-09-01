"""Compatibility facade for Datahub's web server."""
from datahub_plugin.serving.server import *
from datahub_plugin.serving.server import main

if __name__ == "__main__":
    raise SystemExit(main())
