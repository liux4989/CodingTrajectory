from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "web":
        from metrics_web import main as web_main

        return web_main(raw_args[1:])

    parser = argparse.ArgumentParser(
        prog="ct plugin metrics",
        description="Compare canonical token, cost, and execution metrics.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("web",),
        help="Run `ct plugin metrics web` to open the metrics application.",
    )
    parser.parse_args(raw_args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
