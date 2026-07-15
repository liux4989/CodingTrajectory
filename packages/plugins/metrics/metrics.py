from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "web":
        from metrics_web import main as web_main

        return web_main(raw_args[1:])
    if raw_args and raw_args[0] == "validate":
        return _validate(raw_args[1:])

    parser = argparse.ArgumentParser(
        prog="ct plugin metrics",
        description="Compare canonical token, cost, and execution metrics.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("web", "validate"),
        help="Run `ct plugin metrics web` to open the metrics application.",
    )
    parser.parse_args(raw_args)
    parser.print_help()
    return 0


def _validate(argv: list[str]) -> int:
    from metrics_service import MetricsService

    parser = argparse.ArgumentParser(
        prog="ct plugin metrics validate",
        description="Reconcile metrics read models against direct ct service output.",
    )
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--output", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    result = MetricsService().validate(
        since_days=args.since_days,
        sample_size=args.sample_size,
    )
    if args.output == "json":
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        state = "PASS" if result.passed else "FAIL"
        print(f"metrics plugin validation: {state} ({result.checked_graphs} graphs)")
        for check in result.checks:
            print(
                f"  {check.session_graph_id}: "
                f"tokens={check.processed_tokens} direct={check.direct_processed_tokens} "
                f"cost={check.cost_usd} direct={check.direct_cost_usd}"
            )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
