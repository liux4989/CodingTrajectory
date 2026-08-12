"""Read-only cold/warm benchmark for dashboard API routes.

The benchmark invokes the same ``DashboardDataService`` methods as the HTTP
handler. It also wraps every dashboard-owned ``ct`` subprocess adapter so a
slow route can be split into nested core/CLI time and dashboard projection
time. Agent creation, evaluation execution, cleanup application, and other
state-changing routes are intentionally outside the suite.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import platform
import shlex
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

try:
    from . import cleanup as cleanup_mod
    from . import context_window as context_window_mod
    from . import web_services as web_services_mod
except ImportError:
    import cleanup as cleanup_mod
    import context_window as context_window_mod
    import web_services as web_services_mod


BenchmarkStatus = Literal["ok", "warning", "critical", "error", "skipped"]

STANDARD_API_NAMES = (
    "overview",
    "projects",
    "project-detail",
    "sessions",
    "session-timeline",
    "context-window",
    "model-usage",
    "error-collection",
    "cache-breaks",
    "evaluations",
    "vendors",
    "project-cleanup-preview",
    "session-cleanup-preview",
)
EXPENSIVE_API_NAMES = (
    "token-efficiency",
    "token-efficiency-project",
)
ALL_API_NAMES = (*STANDARD_API_NAMES, *EXPENSIVE_API_NAMES)


class InternalCallTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: int = Field(ge=1)
    phase: Literal["cold", "warm"]
    sequence: int = Field(ge=1)
    label: str
    request_count: int = Field(default=1, ge=1)
    elapsed_ms: float = Field(ge=0)
    response_bytes: int = Field(default=0, ge=0)
    status: Literal["ok", "error"]
    error: str | None = None


class ApiBenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    method: Literal["GET"]
    route: str
    query: dict[str, list[str]]
    status: BenchmarkStatus
    cold_runs_ms: list[float] = Field(default_factory=list)
    warm_runs_ms: list[float] = Field(default_factory=list)
    cold_median_ms: float | None = None
    warm_median_ms: float | None = None
    warm_speedup: float | None = None
    nested_ct_median_ms: float | None = None
    dashboard_median_ms: float | None = None
    response_bytes: int | None = None
    response_sha256: str | None = None
    cold_response_sha256: list[str] = Field(default_factory=list)
    warm_response_sha256: list[str] = Field(default_factory=list)
    response_stable: bool | None = None
    internal_calls: list[InternalCallTiming] = Field(default_factory=list)
    error: str | None = None


class BenchmarkFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since_days: int = Field(ge=1)
    project_name: str | None = None
    session_id: str | None = None
    session_title: str | None = None
    session_graph_size: int | None = Field(default=None, ge=1)
    selection: str

    @field_validator("selection")
    @classmethod
    def _nonempty_selection(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fixture selection must not be blank")
        return value


class DashboardApiBenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    generated_at: str
    environment: dict[str, str]
    provenance: BenchmarkProvenance
    fixture_id: str | None = None
    fixture_sha256: str | None = None
    workload_scope: str
    fixture: BenchmarkFixture
    repeat: int = Field(ge=1)
    thresholds_ms: dict[str, float]
    results: list[ApiBenchmarkResult]
    excluded_apis: list[str]
    gate: BenchmarkGate
    comparison: BenchmarkComparison | None = None
    notes: list[str]


class BenchmarkFixtureFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str
    fixture: BenchmarkFixture
    apis: list[str] = Field(min_length=1)
    minimum_repeat: int = Field(default=5, ge=1)
    deterministic_response_apis: list[str] = Field(default_factory=list)
    expected_response_sha256: dict[str, str] = Field(default_factory=dict)
    thresholds_ms: dict[str, float] = Field(
        default_factory=lambda: {"warning": 1_000.0, "critical": 5_000.0}
    )

    @field_validator("id")
    @classmethod
    def _nonempty_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fixture id must not be blank")
        return value

    @field_validator("apis", "deterministic_response_apis")
    @classmethod
    def _unique_api_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("fixture API names must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("fixture API names must be unique")
        return value

    @model_validator(mode="after")
    def _response_contracts_match(self) -> BenchmarkFixtureFile:
        deterministic = set(self.deterministic_response_apis)
        expected = set(self.expected_response_sha256)
        if expected != deterministic:
            raise ValueError(
                "expected response digests must exactly match deterministic APIs"
            )
        return self


class BenchmarkProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_revision: str
    repository_dirty: bool
    benchmark_harness_sha256: str
    plugin_source_sha256: str
    core_cli_source_sha256: str
    uv_lock_sha256: str
    ct_command: str
    ct_executable: str
    ct_executable_sha256: str | None = None


class BenchmarkFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    route: str | None = None
    category: Literal["measurement", "threshold", "regression"]


class BenchmarkGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "fail"]
    measurement_status: Literal["pass", "fail"]
    threshold_status: Literal["pass", "fail"]
    failures: list[BenchmarkFailure] = Field(default_factory=list)


class RouteComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    route: str
    baseline_cold_median_ms: float
    candidate_cold_median_ms: float
    delta_ms: float
    delta_percent: float
    allowed_regression_ms: float
    status: Literal["pass", "fail"]


class BenchmarkComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_path: str
    baseline_sha256: str
    baseline_schema_version: int | None = None
    status: Literal["pass", "fail", "incompatible"]
    routes: list[RouteComparison] = Field(default_factory=list)
    failures: list[BenchmarkFailure] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ApiSpec:
    name: str
    route: str
    query: dict[str, list[str]]
    invoke: Callable[[], dict[str, Any]]
    missing_fixture: str | None = None


@dataclass(frozen=True, slots=True)
class _MeasuredRun:
    elapsed_ms: float
    response_bytes: int
    response_sha256: str | None
    calls: list[InternalCallTiming]
    error: str | None


class _CallTracer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: tuple[int, Literal["cold", "warm"]] | None = None
        self._calls: list[InternalCallTiming] = []

    def begin(self, run: int, phase: Literal["cold", "warm"]) -> None:
        with self._lock:
            self._active = (run, phase)
            self._calls = []

    def finish(self) -> list[InternalCallTiming]:
        with self._lock:
            calls = list(self._calls)
            self._active = None
            self._calls = []
        return calls

    def wrap(
        self, operation: Callable[[list[str]], dict[str, Any]]
    ) -> Callable[[list[str]], dict[str, Any]]:
        @functools.wraps(operation)
        def traced(args: list[str]) -> dict[str, Any]:
            started = time.perf_counter()
            payload: dict[str, Any] | None = None
            error: str | None = None
            try:
                payload = operation(args)
                return payload
            except BaseException as exc:
                error = _error_text(exc)
                raise
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1_000
                response_bytes = (
                    len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    if payload is not None
                    else 0
                )
                with self._lock:
                    active = self._active
                    if active is not None:
                        run, phase = active
                        self._calls.append(
                            InternalCallTiming(
                                run=run,
                                phase=phase,
                                sequence=len(self._calls) + 1,
                                label=_command_label(args),
                                request_count=_request_count(args),
                                elapsed_ms=round(elapsed_ms, 3),
                                response_bytes=response_bytes,
                                status="error" if error else "ok",
                                error=error,
                            )
                        )

        return traced


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.list_apis:
        print("\n".join(ALL_API_NAMES))
        return 0
    fixture_file, fixture_sha256 = _load_fixture_file(args.fixture_file)
    if args.repeat is not None and args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.since_days is not None and args.since_days < 1:
        parser.error("--since-days must be at least 1")
    if fixture_file is not None and (
        args.project_name
        or args.session_id
        or args.small
        or args.since_days is not None
        or args.warn_ms is not None
        or args.critical_ms is not None
        or args.include_expensive
    ):
        parser.error(
            "--fixture-file cannot be combined with fixture discovery, "
            "threshold, or route-expansion flags"
        )

    repeat = args.repeat or (fixture_file.minimum_repeat if fixture_file else 1)
    since_days = (
        fixture_file.fixture.since_days
        if fixture_file is not None
        else args.since_days or 7
    )
    warn_ms = (
        args.warn_ms
        if args.warn_ms is not None
        else (fixture_file.thresholds_ms["warning"] if fixture_file else 1_000.0)
    )
    critical_ms = (
        args.critical_ms
        if args.critical_ms is not None
        else (fixture_file.thresholds_ms["critical"] if fixture_file else 5_000.0)
    )
    if (
        not math.isfinite(warn_ms)
        or not math.isfinite(critical_ms)
        or warn_ms <= 0
        or critical_ms <= warn_ms
    ):
        parser.error("thresholds must satisfy 0 < --warn-ms < --critical-ms")
    if (
        not math.isfinite(args.max_regression_percent)
        or not math.isfinite(args.max_regression_ms)
        or args.max_regression_percent < 0
        or args.max_regression_ms < 0
    ):
        parser.error("regression allowances must be finite and non-negative")

    requested = _requested_apis(args, fixture_file=fixture_file)
    fixture = (
        fixture_file.fixture
        if fixture_file is not None
        else _discover_fixture(
            since_days=since_days,
            project_name=args.project_name,
            session_id=args.session_id,
            smallest=args.small,
            require_project=bool(
                requested & {"project-detail", "token-efficiency-project"}
            ),
            require_session=bool(requested & {"context-window", "evaluations"}),
        )
    )
    save_target = _resolve_save_target(args.save, no_save=args.no_save)
    if (
        save_target is not None
        and args.compare is not None
        and save_target.resolve() == _resolve_report_path(args.compare).resolve()
    ):
        parser.error("--save and --compare must reference different reports")
    _validate_save_target(
        parser,
        save_target,
        overwrite=args.overwrite,
        overwrite_baseline=args.overwrite_baseline,
        requested=requested,
        repeat=repeat,
    )
    service = web_services_mod.DashboardDataService()
    tracer = _CallTracer()
    try:
        specs = _api_specs(service, fixture)
        results: list[ApiBenchmarkResult] = []
        with _trace_dashboard_calls(tracer):
            for spec in specs:
                if spec.name not in requested:
                    continue
                print(f"benchmarking {spec.route}...", file=sys.stderr, flush=True)
                results.append(
                    _benchmark_api(
                        spec,
                        service=service,
                        tracer=tracer,
                        repeat=repeat,
                        warn_ms=warn_ms,
                        critical_ms=critical_ms,
                    )
                )
    finally:
        service.shutdown()

    excluded = [name for name in ALL_API_NAMES if name not in requested]
    provenance = _provenance()
    gate = _evaluate_gate(
        results,
        repeat=repeat,
        fixture_file=fixture_file,
        requested=requested,
        provenance=provenance,
    )
    report = DashboardApiBenchmarkReport(
        generated_at=datetime.now(UTC).isoformat(),
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        provenance=provenance,
        fixture_id=fixture_file.id if fixture_file else None,
        fixture_sha256=fixture_sha256,
        workload_scope=(
            "host-pinned session identity; response digest detects drift but "
            "the source corpus is not bundled"
            if fixture_file is not None
            else "machine-local discovered fixture"
        ),
        fixture=fixture,
        repeat=repeat,
        thresholds_ms={
            "warning": float(warn_ms),
            "critical": float(critical_ms),
        },
        results=results,
        excluded_apis=excluded,
        gate=gate,
        notes=[
            "Dashboard-cache-cold runs clear projection and shared source caches before each call; warm runs immediately repeat the same call.",
            "nested_ct_median_ms is median summed subprocess work; dashboard_median_ms is a residual estimate and is not valid CPU attribution when subprocesses overlap.",
            "Agent, evaluation-start, cleanup-apply, and other state-changing APIs are excluded.",
            "Token-efficiency projections are included only with --include-expensive or an explicit --api selection.",
        ],
    )
    if args.compare:
        report.comparison = _compare_report(
            report,
            Path(args.compare),
            max_regression_percent=args.max_regression_percent,
            max_regression_ms=args.max_regression_ms,
            fixture_file=fixture_file,
        )
    _validate_baseline_write(save_target, report)
    if save_target is not None:
        save_target.parent.mkdir(parents=True, exist_ok=True)
        _write_report(save_target, report)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(
            _render_report(
                report,
                save=str(save_target.relative_to(_repo_root()))
                if save_target and save_target.is_relative_to(_repo_root())
                else str(save_target)
                if save_target
                else None,
            )
        )
    comparison_failed = (
        report.comparison is not None and report.comparison.status != "pass"
    )
    return 1 if report.gate.status == "fail" or comparison_failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard benchmark",
        description=(
            "Benchmark read-only dashboard API routes and attribute latency to "
            "their nested ct calls."
        ),
    )
    parser.add_argument("--since-days", type=int, default=None)
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--fixture-file",
        default=None,
        help="Pinned benchmark fixture manifest; disables fixture discovery.",
    )
    parser.add_argument(
        "--small",
        action="store_true",
        help="Use the smallest observed session graph instead of the largest.",
    )
    parser.add_argument("--repeat", type=int, default=None)
    parser.add_argument("--warn-ms", type=float, default=None)
    parser.add_argument("--critical-ms", type=float, default=None)
    parser.add_argument(
        "--api",
        action="append",
        default=[],
        help="Benchmark only this API name (repeat or use comma-separated names).",
    )
    parser.add_argument(
        "--include-expensive",
        action="store_true",
        help="Also run the token-efficiency worker projections.",
    )
    parser.add_argument("--list-apis", action="store_true")
    parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table."
    )
    parser.add_argument(
        "--save",
        default=None,
        help="Opt-in JSON report path, relative to the repository root.",
    )
    parser.add_argument(
        "--compare",
        default=None,
        help="Strictly compare against a schema-v2 benchmark report.",
    )
    parser.add_argument("--max-regression-percent", type=float, default=15.0)
    parser.add_argument("--max-regression-ms", type=float, default=500.0)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--overwrite-baseline",
        action="store_true",
        help="Permit replacing the all-route baseline after its safety checks pass.",
    )
    parser.add_argument("--no-save", action="store_true", help=argparse.SUPPRESS)
    return parser


def _requested_apis(
    args: argparse.Namespace,
    *,
    fixture_file: BenchmarkFixtureFile | None,
) -> set[str]:
    explicit = {
        name.strip() for value in args.api for name in value.split(",") if name.strip()
    }
    unknown = explicit - set(ALL_API_NAMES)
    if unknown:
        raise SystemExit(f"unknown dashboard API name: {sorted(unknown)[0]}")
    if fixture_file is not None:
        fixture_apis = set(fixture_file.apis)
        unknown_fixture = fixture_apis - set(ALL_API_NAMES)
        if unknown_fixture:
            raise SystemExit(
                f"unknown dashboard API in fixture: {sorted(unknown_fixture)[0]}"
            )
        if explicit and explicit != fixture_apis:
            raise SystemExit("--api must exactly match the pinned fixture APIs")
        return fixture_apis
    if explicit:
        return explicit
    requested = set(STANDARD_API_NAMES)
    if args.include_expensive:
        requested.update(EXPENSIVE_API_NAMES)
    return requested


def _load_fixture_file(
    value: str | None,
) -> tuple[BenchmarkFixtureFile | None, str | None]:
    if value is None:
        return None, None
    path = Path(value)
    if not path.is_absolute():
        path = _repo_root() / path
    try:
        raw = path.read_bytes()
        fixture = BenchmarkFixtureFile.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid dashboard benchmark fixture {path}: {exc}") from exc
    unknown_deterministic = set(fixture.deterministic_response_apis) - set(fixture.apis)
    unknown_expected = set(fixture.expected_response_sha256) - set(fixture.apis)
    if unknown_deterministic or unknown_expected:
        raise SystemExit("fixture response contracts must reference configured APIs")
    if set(fixture.thresholds_ms) != {"warning", "critical"}:
        raise SystemExit("fixture thresholds_ms must contain warning and critical")
    warning = fixture.thresholds_ms["warning"]
    critical = fixture.thresholds_ms["critical"]
    if not math.isfinite(warning) or not math.isfinite(critical):
        raise SystemExit("fixture thresholds must be finite")
    if warning <= 0 or critical <= warning:
        raise SystemExit("fixture thresholds must satisfy 0 < warning < critical")
    for digest in fixture.expected_response_sha256.values():
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise SystemExit("fixture response digests must be lowercase SHA-256")
    return fixture, hashlib.sha256(raw).hexdigest()


def _resolve_save_target(value: str | None, *, no_save: bool) -> Path | None:
    if value is not None and no_save:
        raise SystemExit("--save and --no-save cannot be combined")
    if value is None or no_save:
        return None
    path = Path(value)
    return path if path.is_absolute() else _repo_root() / path


def _resolve_report_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else _repo_root() / path


def _validate_save_target(
    parser: argparse.ArgumentParser,
    target: Path | None,
    *,
    overwrite: bool,
    overwrite_baseline: bool,
    requested: set[str],
    repeat: int,
) -> None:
    if target is None:
        if overwrite or overwrite_baseline:
            parser.error("--overwrite flags require --save")
        return
    baseline = _repo_root() / "benchmarks/results/dashboard-api-baseline.json"
    is_baseline = target.resolve() == baseline.resolve()
    if overwrite_baseline and not is_baseline:
        parser.error("--overwrite-baseline is valid only for the all-route baseline")
    if is_baseline:
        if not overwrite_baseline:
            parser.error(
                "refusing to replace dashboard-api-baseline.json without "
                "--overwrite-baseline"
            )
        if requested != set(STANDARD_API_NAMES):
            parser.error("the all-route baseline requires every standard API")
        if repeat < 5:
            parser.error("the all-route baseline requires --repeat 5 or greater")
    if target.exists() and not (overwrite_baseline if is_baseline else overwrite):
        parser.error(f"report already exists; pass --overwrite: {target}")


def _validate_baseline_write(
    target: Path | None,
    report: DashboardApiBenchmarkReport,
) -> None:
    if target is None:
        return
    baseline = _repo_root() / "benchmarks/results/dashboard-api-baseline.json"
    if target.resolve() != baseline.resolve():
        return
    if report.gate.status != "pass":
        raise SystemExit(
            "refusing to replace all-route baseline with a failed benchmark gate"
        )
    if report.comparison is not None and report.comparison.status != "pass":
        raise SystemExit(
            "refusing to replace all-route baseline with a failed comparison"
        )


def _write_report(
    target: Path,
    report: DashboardApiBenchmarkReport,
) -> None:
    payload = report.model_dump_json(indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            temporary = Path(handle.name)
        temporary.replace(target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _evaluate_gate(
    results: list[ApiBenchmarkResult],
    *,
    repeat: int,
    fixture_file: BenchmarkFixtureFile | None,
    requested: set[str],
    provenance: BenchmarkProvenance,
) -> BenchmarkGate:
    failures: list[BenchmarkFailure] = []

    def fail(
        code: str,
        message: str,
        route: str | None = None,
        *,
        category: Literal["measurement", "threshold"] = "measurement",
    ) -> None:
        failures.append(
            BenchmarkFailure(
                code=code,
                message=message,
                route=route,
                category=category,
            )
        )

    minimum_repeat = fixture_file.minimum_repeat if fixture_file else 1
    if provenance.repository_dirty:
        fail("dirty_repository", "benchmark source tree has uncommitted changes")
    if repeat < minimum_repeat:
        fail(
            "insufficient_repeats",
            f"repeat {repeat} is below fixture minimum {minimum_repeat}",
        )
    by_name = {row.name: row for row in results}
    for name in sorted(requested - set(by_name)):
        fail("missing_route", f"benchmark omitted requested API: {name}")
    deterministic = (
        set(fixture_file.deterministic_response_apis) if fixture_file else set()
    )
    expected_digests = fixture_file.expected_response_sha256 if fixture_file else {}
    for row in results:
        route = row.route
        if row.status in {"error", "skipped"}:
            code = (
                "timeout_censored"
                if "timed out" in (row.error or "")
                else "invalid_route"
            )
            fail(code, row.error or f"route status is {row.status}", route)
            continue
        if len(row.cold_runs_ms) != repeat or len(row.warm_runs_ms) != repeat:
            fail("incomplete_samples", "cold or warm samples are incomplete", route)
        samples = [*row.cold_runs_ms, *row.warm_runs_ms]
        if any(not math.isfinite(value) or value <= 0 for value in samples):
            fail(
                "invalid_measurement",
                "latency samples must be finite and positive",
                route,
            )
        if row.status in {"warning", "critical"}:
            fail(
                "threshold_breach",
                f"cold median has {row.status} threshold status",
                route,
                category="threshold",
            )
        if row.name in deterministic:
            if row.response_stable is not True or row.response_sha256 is None:
                fail(
                    "unstable_response",
                    "deterministic route responses changed across runs",
                    route,
                )
            expected = expected_digests.get(row.name)
            if expected and row.response_sha256 != expected:
                fail(
                    "response_digest_mismatch",
                    f"expected {expected}, observed {row.response_sha256}",
                    route,
                )
    measurement_failed = any(
        failure.category == "measurement" for failure in failures
    )
    threshold_failed = any(failure.category == "threshold" for failure in failures)
    return BenchmarkGate(
        status="fail" if failures else "pass",
        measurement_status="fail" if measurement_failed else "pass",
        threshold_status="fail" if threshold_failed else "pass",
        failures=failures,
    )


def _compare_report(
    candidate: DashboardApiBenchmarkReport,
    baseline_value: Path,
    *,
    max_regression_percent: float,
    max_regression_ms: float,
    fixture_file: BenchmarkFixtureFile | None,
) -> BenchmarkComparison:
    path = _resolve_report_path(baseline_value)
    failures: list[BenchmarkFailure] = []
    routes: list[RouteComparison] = []

    def fail(code: str, message: str, route: str | None = None) -> None:
        failures.append(
            BenchmarkFailure(
                code=code,
                message=message,
                route=route,
                category="regression",
            )
        )

    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        fail("baseline_unreadable", str(exc))
        return BenchmarkComparison(
            baseline_path=str(path),
            baseline_sha256="",
            status="incompatible",
            failures=failures,
        )
    baseline_sha256 = hashlib.sha256(raw).hexdigest()
    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    if schema_version != 2:
        fail(
            "baseline_schema_incompatible",
            f"strict comparison requires schema 2, got {schema_version}",
        )
        return BenchmarkComparison(
            baseline_path=str(path),
            baseline_sha256=baseline_sha256,
            baseline_schema_version=schema_version,
            status="incompatible",
            failures=failures,
        )
    try:
        baseline = DashboardApiBenchmarkReport.model_validate(data)
    except ValueError as exc:
        fail("baseline_invalid", str(exc))
        return BenchmarkComparison(
            baseline_path=str(path),
            baseline_sha256=baseline_sha256,
            baseline_schema_version=schema_version,
            status="incompatible",
            failures=failures,
        )
    if fixture_file is None:
        fail("fixture_required", "strict comparison requires --fixture-file")
    if candidate.fixture_id != baseline.fixture_id:
        fail("fixture_id_mismatch", "candidate and baseline fixture ids differ")
    if candidate.fixture_sha256 != baseline.fixture_sha256:
        fail("fixture_hash_mismatch", "candidate and baseline fixture hashes differ")
    if candidate.fixture != baseline.fixture:
        fail("fixture_mismatch", "candidate and baseline fixtures differ")
    if candidate.thresholds_ms != baseline.thresholds_ms:
        fail("threshold_mismatch", "candidate and baseline thresholds differ")
    if candidate.environment != baseline.environment:
        fail("environment_mismatch", "Python or platform environment differs")
    for field in (
        "benchmark_harness_sha256",
        "core_cli_source_sha256",
        "uv_lock_sha256",
    ):
        if getattr(candidate.provenance, field) != getattr(baseline.provenance, field):
            fail("provenance_mismatch", f"provenance field differs: {field}")
    minimum_repeat = fixture_file.minimum_repeat if fixture_file else 1
    if baseline.provenance.repository_dirty:
        fail("baseline_dirty_repository", "baseline source tree was dirty")
    if baseline.gate.measurement_status != "pass":
        fail("baseline_invalid_gate", "baseline measurement gate did not pass")
    if baseline.repeat < minimum_repeat:
        fail(
            "baseline_insufficient_repeats",
            f"baseline repeat {baseline.repeat} is below {minimum_repeat}",
        )
    baseline_rows = {row.name: row for row in baseline.results}
    for row in candidate.results:
        previous = baseline_rows.get(row.name)
        if previous is None:
            fail("baseline_missing_route", f"baseline omitted {row.name}", row.route)
            continue
        if row.route != previous.route or row.query != previous.query:
            fail("route_mismatch", "route or query differs from baseline", row.route)
            continue
        if previous.status in {"error", "skipped"}:
            fail("baseline_invalid_route", "baseline route did not succeed", row.route)
            continue
        if (
            len(previous.cold_runs_ms) != baseline.repeat
            or len(previous.warm_runs_ms) != baseline.repeat
            or previous.cold_median_ms is None
            or row.cold_median_ms is None
        ):
            fail(
                "baseline_incomplete_samples",
                "baseline samples are incomplete",
                row.route,
            )
            continue
        baseline_samples = [*previous.cold_runs_ms, *previous.warm_runs_ms]
        if any(not math.isfinite(value) or value <= 0 for value in baseline_samples):
            fail(
                "baseline_invalid_measurement",
                "baseline samples are invalid",
                row.route,
            )
            continue
        recomputed_median = statistics.median(previous.cold_runs_ms)
        if not math.isclose(
            previous.cold_median_ms,
            recomputed_median,
            rel_tol=0,
            abs_tol=0.001,
        ):
            fail(
                "baseline_derived_value_mismatch",
                "baseline cold median does not match its samples",
                row.route,
            )
            continue
        expected_status = _threshold_status(
            recomputed_median,
            warning_ms=baseline.thresholds_ms["warning"],
            critical_ms=baseline.thresholds_ms["critical"],
        )
        if previous.status != expected_status:
            fail(
                "baseline_derived_value_mismatch",
                "baseline route status does not match its median and thresholds",
                row.route,
            )
            continue
        if row.name in (
            set(fixture_file.deterministic_response_apis) if fixture_file else set()
        ):
            baseline_digests = [
                *previous.cold_response_sha256,
                *previous.warm_response_sha256,
            ]
            if (
                previous.response_stable is not True
                or previous.response_sha256 is None
                or len(previous.cold_response_sha256) != baseline.repeat
                or len(previous.warm_response_sha256) != baseline.repeat
                or set(baseline_digests) != {previous.response_sha256}
            ):
                fail(
                    "baseline_unstable_response",
                    "baseline response is unstable",
                    row.route,
                )
                continue
            if row.response_sha256 != previous.response_sha256:
                fail(
                    "response_digest_mismatch",
                    "response changed from baseline",
                    row.route,
                )
                continue
        allowed = min(
            previous.cold_median_ms * max_regression_percent / 100,
            max_regression_ms,
        )
        delta = row.cold_median_ms - previous.cold_median_ms
        route_status: Literal["pass", "fail"] = "fail" if delta > allowed else "pass"
        routes.append(
            RouteComparison(
                name=row.name,
                route=row.route,
                baseline_cold_median_ms=previous.cold_median_ms,
                candidate_cold_median_ms=row.cold_median_ms,
                delta_ms=round(delta, 3),
                delta_percent=round(
                    (delta / previous.cold_median_ms) * 100,
                    3,
                ),
                allowed_regression_ms=round(allowed, 3),
                status=route_status,
            )
        )
        if route_status == "fail":
            fail(
                "latency_regression",
                f"cold median regressed by {delta:.3f}ms; allowance is {allowed:.3f}ms",
                row.route,
            )
    status: Literal["pass", "fail", "incompatible"] = "fail" if failures else "pass"
    return BenchmarkComparison(
        baseline_path=str(path),
        baseline_sha256=baseline_sha256,
        baseline_schema_version=schema_version,
        status=status,
        routes=routes,
        failures=failures,
    )


def _discover_fixture(
    *,
    since_days: int,
    project_name: str | None,
    session_id: str | None,
    smallest: bool,
    require_project: bool,
    require_session: bool,
) -> BenchmarkFixture:
    if (project_name or not require_project) and (session_id or not require_session):
        return BenchmarkFixture(
            since_days=since_days,
            project_name=project_name,
            session_id=session_id,
            selection=(
                "explicit fixture"
                if project_name or session_id
                else "no route fixture required"
            ),
        )
    params: dict[str, Any] = {
        "since_days": since_days,
        "include": ["runtime"],
    }
    if project_name:
        params["project_name"] = project_name
    payload = web_services_mod._run_ct_json(
        [
            "api",
            "call",
            "project.sessions",
            "--global-scope",
            "--params",
            json.dumps(params),
        ],
        timeout_seconds=120,
    )
    result = payload.get("result") if payload.get("ok") else {}
    items = [
        item for item in (result or {}).get("items") or [] if isinstance(item, dict)
    ]
    selected: dict[str, Any] | None = None
    selection = "explicit session id"
    if session_id:
        selected = next(
            (
                item
                for item in items
                if session_id
                in {
                    str(item.get("root_session_id") or ""),
                    str(item.get("id") or ""),
                    *(str(value) for value in item.get("session_ids") or []),
                }
            ),
            None,
        )
    elif items:
        selected = sorted(items, key=_fixture_size, reverse=not smallest)[0]
        session_id = (
            str(selected.get("root_session_id") or selected.get("id") or "") or None
        )
        selection = (
            "smallest observed session graph by runtime.items"
            if smallest
            else "largest observed session graph by runtime.items"
        )
    if selected and not project_name:
        project_name = str(selected.get("project") or "") or None
    if project_name is None and items:
        project_name = str(items[0].get("project") or "") or None
    return BenchmarkFixture(
        since_days=since_days,
        project_name=project_name,
        session_id=session_id,
        session_title=(str(selected.get("title") or "") or None) if selected else None,
        session_graph_size=_fixture_size(selected) if selected else None,
        selection=selection,
    )


def _fixture_size(item: dict[str, Any] | None) -> int:
    if not item:
        return 1
    runtime = item.get("runtime") if isinstance(item.get("runtime"), dict) else {}
    count = runtime.get("items")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count
    return max(1, len(item.get("session_ids") or []))


def _api_specs(
    service: web_services_mod.DashboardDataService,
    fixture: BenchmarkFixture,
) -> list[_ApiSpec]:
    since_query = {"since_days": [str(fixture.since_days)]}
    project_query = {
        **since_query,
        **({"project_name": [fixture.project_name]} if fixture.project_name else {}),
    }
    session_query = {"session_id": [fixture.session_id]} if fixture.session_id else {}
    evaluation_query = {
        "scope_type": ["session"],
        **({"scope_id": [fixture.session_id]} if fixture.session_id else {}),
    }
    project_missing = None if fixture.project_name else "no project fixture discovered"
    session_missing = None if fixture.session_id else "no session fixture discovered"
    return [
        _ApiSpec(
            "overview",
            "/api/overview",
            since_query,
            lambda: service.overview(since_query),
        ),
        _ApiSpec("projects", "/api/projects", {}, lambda: service.projects({})),
        _ApiSpec(
            "project-detail",
            "/api/projects/detail",
            project_query,
            lambda: service.project_detail(project_query),
            project_missing,
        ),
        _ApiSpec(
            "sessions",
            "/api/sessions",
            since_query,
            lambda: service.sessions(since_query),
        ),
        _ApiSpec(
            "session-timeline",
            "/api/sessions/timeline",
            since_query,
            lambda: service.session_timeline(since_query),
        ),
        _ApiSpec(
            "context-window",
            "/api/sessions/context-window",
            session_query,
            lambda: service.context_window(session_query),
            session_missing,
        ),
        _ApiSpec(
            "model-usage",
            "/api/model-usage",
            since_query,
            lambda: service.model_usage(since_query),
        ),
        _ApiSpec(
            "token-efficiency",
            "/api/token-efficiency",
            since_query,
            lambda: service.token_efficiency_index(since_query),
        ),
        _ApiSpec(
            "token-efficiency-project",
            "/api/token-efficiency/project",
            project_query,
            lambda: service.token_efficiency_project(project_query),
            project_missing,
        ),
        _ApiSpec(
            "error-collection",
            "/api/error-collection",
            since_query,
            lambda: service.error_collection(since_query),
        ),
        _ApiSpec(
            "cache-breaks",
            "/api/cache-breaks",
            since_query,
            lambda: service.cache_breaks(since_query),
        ),
        _ApiSpec(
            "evaluations",
            "/api/evaluations",
            evaluation_query,
            lambda: service.evaluation_list(evaluation_query),
            session_missing,
        ),
        _ApiSpec("vendors", "/api/vendors", {}, lambda: service.vendors({})),
        _ApiSpec(
            "project-cleanup-preview",
            "/api/cleanup/project/preview",
            since_query,
            lambda: service.project_cleanup_preview(since_query),
        ),
        _ApiSpec(
            "session-cleanup-preview",
            "/api/cleanup/session/preview",
            {},
            lambda: service.session_cleanup_preview({}),
        ),
    ]


def _benchmark_api(
    spec: _ApiSpec,
    *,
    service: web_services_mod.DashboardDataService,
    tracer: _CallTracer,
    repeat: int,
    warn_ms: float,
    critical_ms: float,
) -> ApiBenchmarkResult:
    if spec.missing_fixture:
        return ApiBenchmarkResult(
            name=spec.name,
            method="GET",
            route=spec.route,
            query=spec.query,
            status="skipped",
            error=spec.missing_fixture,
        )
    cold_runs: list[_MeasuredRun] = []
    warm_runs: list[_MeasuredRun] = []
    for run in range(1, repeat + 1):
        _clear_dashboard_caches(service)
        cold = _measure(spec.invoke, tracer=tracer, run=run, phase="cold")
        cold_runs.append(cold)
        if cold.error is None:
            warm_runs.append(
                _measure(spec.invoke, tracer=tracer, run=run, phase="warm")
            )

    cold_values = [sample.elapsed_ms for sample in cold_runs]
    warm_values = [sample.elapsed_ms for sample in warm_runs]
    cold_median = statistics.median(cold_values) if cold_values else None
    warm_median = statistics.median(warm_values) if warm_values else None
    nested_totals = [
        sum(call.elapsed_ms for call in sample.calls) for sample in cold_runs
    ]
    nested_median = statistics.median(nested_totals) if nested_totals else None
    dashboard_median = (
        max(0.0, cold_median - nested_median)
        if cold_median is not None and nested_median is not None
        else None
    )
    error = next(
        (
            sample.error
            for sample in (*cold_runs, *warm_runs)
            if sample.error is not None
        ),
        None,
    )
    if error:
        status: BenchmarkStatus = "error"
    elif cold_median is not None:
        status = _threshold_status(
            cold_median,
            warning_ms=warn_ms,
            critical_ms=critical_ms,
        )
    else:
        status = "ok"
    calls = [call for sample in (*cold_runs, *warm_runs) for call in sample.calls]
    cold_digests = [
        sample.response_sha256
        for sample in cold_runs
        if sample.response_sha256 is not None
    ]
    warm_digests = [
        sample.response_sha256
        for sample in warm_runs
        if sample.response_sha256 is not None
    ]
    all_digests = [*cold_digests, *warm_digests]
    response_stable = (
        len(cold_digests) == repeat
        and len(warm_digests) == repeat
        and len(set(all_digests)) == 1
    )
    return ApiBenchmarkResult(
        name=spec.name,
        method="GET",
        route=spec.route,
        query=spec.query,
        status=status,
        cold_runs_ms=[round(value, 3) for value in cold_values],
        warm_runs_ms=[round(value, 3) for value in warm_values],
        cold_median_ms=_rounded(cold_median),
        warm_median_ms=_rounded(warm_median),
        warm_speedup=(
            round(cold_median / warm_median, 2)
            if cold_median is not None and warm_median and warm_median > 0
            else None
        ),
        nested_ct_median_ms=_rounded(nested_median),
        dashboard_median_ms=_rounded(dashboard_median),
        response_bytes=next(
            (
                sample.response_bytes
                for sample in reversed(cold_runs)
                if not sample.error
            ),
            None,
        ),
        response_sha256=all_digests[0] if response_stable else None,
        cold_response_sha256=cold_digests,
        warm_response_sha256=warm_digests,
        response_stable=response_stable,
        internal_calls=calls,
        error=error,
    )


def _clear_dashboard_caches(service: web_services_mod.DashboardDataService) -> None:
    """Clear caches across current and legacy dashboard implementations."""
    clear = getattr(service, "clear_caches", None)
    if callable(clear):
        clear()
        return
    service._work.clear_all()


def _measure(
    operation: Callable[[], dict[str, Any]],
    *,
    tracer: _CallTracer,
    run: int,
    phase: Literal["cold", "warm"],
) -> _MeasuredRun:
    tracer.begin(run, phase)
    started = time.perf_counter()
    response_bytes = 0
    response_sha256: str | None = None
    error: str | None = None
    try:
        payload = operation()
        encoded = _canonical_json(payload)
        response_bytes = len(encoded)
        response_sha256 = hashlib.sha256(encoded).hexdigest()
    except (Exception, SystemExit) as exc:
        error = _error_text(exc)
    elapsed_ms = (time.perf_counter() - started) * 1_000
    return _MeasuredRun(
        elapsed_ms=elapsed_ms,
        response_bytes=response_bytes,
        response_sha256=response_sha256,
        calls=tracer.finish(),
        error=error,
    )


@contextmanager
def _trace_dashboard_calls(tracer: _CallTracer) -> Iterator[None]:
    candidates = [
        (web_services_mod, "_ct_json"),
        (web_services_mod, "_ct_json_expensive"),
        (context_window_mod, "_ct_json"),
        (cleanup_mod, "_ct_json"),
    ]
    targets = [
        (module, name)
        for module, name in candidates
        if callable(getattr(module, name, None))
    ]
    originals = [(module, name, getattr(module, name)) for module, name in targets]
    try:
        for module, name, operation in originals:
            setattr(module, name, tracer.wrap(operation))
        yield
    finally:
        for module, name, operation in originals:
            setattr(module, name, operation)


def _command_label(args: list[str]) -> str:
    if len(args) >= 3 and args[:2] == ["api", "call"]:
        return f"ct api call {args[2]}"
    if args[:2] == ["api", "batch"]:
        methods = _batch_methods(args)
        if methods:
            counts: dict[str, int] = {}
            for method in methods:
                counts[method] = counts.get(method, 0) + 1
            detail = ", ".join(
                f"{method} x{count}" if count > 1 else method
                for method, count in counts.items()
            )
            return f"ct api batch [{detail}]"
        return "ct api batch"
    if len(args) >= 2 and args[0] in {"project", "session"}:
        return f"ct {args[0]} {args[1]}"
    return "ct " + " ".join(args[:3])


def _request_count(args: list[str]) -> int:
    methods = _batch_methods(args)
    return max(1, len(methods))


def _batch_methods(args: list[str]) -> list[str]:
    if args[:2] != ["api", "batch"]:
        return []
    try:
        index = args.index("--requests")
        requests = json.loads(args[index + 1])
    except (ValueError, IndexError, json.JSONDecodeError):
        return []
    if not isinstance(requests, list):
        return []
    return [
        str(item.get("method"))
        for item in requests
        if isinstance(item, dict) and item.get("method")
    ]


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    ct = web_services_mod.os.environ.get("CT_COMMAND") or web_services_mod.shutil.which(
        "ct"
    )
    if ct:
        text = text.replace(str(ct), "ct")
    text = text.replace(str(_repo_root()), "<repo>")
    return text if len(text) <= 500 else text[:497] + "..."


def _rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _threshold_status(
    value: float,
    *,
    warning_ms: float,
    critical_ms: float,
) -> Literal["ok", "warning", "critical"]:
    if value >= critical_ms:
        return "critical"
    if value >= warning_ms:
        return "warning"
    return "ok"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _provenance() -> BenchmarkProvenance:
    repo = _repo_root()
    command = str(web_services_mod.os.environ.get("CT_COMMAND") or "ct")
    executable_token = shlex.split(command)[0]
    executable_value = (
        web_services_mod.shutil.which(executable_token) or executable_token
    )
    executable = Path(executable_value)
    if not executable.is_absolute():
        executable = (repo / executable).resolve()
    else:
        executable = executable.resolve()
    repository_revision = _git_output("rev-parse", "HEAD")
    repository_status = _git_output(
        "status", "--porcelain=v1", "--untracked-files=all"
    )
    return BenchmarkProvenance(
        repository_revision=repository_revision,
        repository_dirty=bool(repository_status),
        benchmark_harness_sha256=_file_sha256(Path(__file__)),
        plugin_source_sha256=_source_tree_sha256(
            repo / "packages/plugins/dashboard",
            suffixes={".py", ".toml"},
        ),
        core_cli_source_sha256=_combined_source_sha256(
            [repo / "packages/core", repo / "packages/cli"],
            suffixes={".py", ".toml"},
        ),
        uv_lock_sha256=_file_sha256(repo / "uv.lock"),
        ct_command=command,
        ct_executable=str(executable),
        ct_executable_sha256=(
            _file_sha256(executable) if executable.is_file() else None
        ),
    )


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=_repo_root(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    output = completed.stdout.strip()
    if args[:2] == ("rev-parse", "HEAD") and len(output) != 40:
        raise RuntimeError("git rev-parse HEAD returned an invalid revision")
    return output


def _source_tree_sha256(root: Path, *, suffixes: set[str]) -> str:
    return _hash_paths(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "__pycache__" not in path.parts
    )


def _combined_source_sha256(roots: list[Path], *, suffixes: set[str]) -> str:
    return _hash_paths(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "__pycache__" not in path.parts
    )


def _hash_paths(paths: Iterator[Path]) -> str:
    digest = hashlib.sha256()
    repo = _repo_root()
    for path in sorted(paths):
        try:
            label = str(path.relative_to(repo))
        except ValueError:
            label = str(path)
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_report(report: DashboardApiBenchmarkReport, *, save: str | None) -> str:
    fixture = report.fixture
    lines = [
        "Dashboard API benchmark",
        f"Generated: {report.generated_at}",
        (
            f"Fixture: project={fixture.project_name or '-'} "
            f"session={fixture.session_id or '-'} size={fixture.session_graph_size or '-'} "
            f"({fixture.selection})"
        ),
        (
            "Thresholds: warning >= "
            f"{_format_ms(report.thresholds_ms['warning'])}, critical >= "
            f"{_format_ms(report.thresholds_ms['critical'])}"
        ),
        "",
        f"{'route':38} {'cold':>10} {'warm':>10} {'ct':>10} {'dashboard':>10} {'payload':>10}  status",
        "-" * 112,
    ]
    for row in report.results:
        lines.append(
            f"{row.route:38} "
            f"{_format_ms(row.cold_median_ms):>10} "
            f"{_format_ms(row.warm_median_ms):>10} "
            f"{_format_ms(row.nested_ct_median_ms):>10} "
            f"{_format_ms(row.dashboard_median_ms):>10} "
            f"{_format_bytes(row.response_bytes):>10}  {row.status}"
        )
    lines.extend(["", "Nested ct breakdown (cold run 1)"])
    for row in report.results:
        cold_calls = [
            call
            for call in row.internal_calls
            if call.run == 1 and call.phase == "cold"
        ]
        if not cold_calls and not row.error:
            continue
        lines.append(f"  {row.route}")
        for call in cold_calls:
            suffix = f" [{call.status}]" if call.status == "error" else ""
            lines.append(
                f"    {_format_ms(call.elapsed_ms):>10}  {call.label}"
                f" ({call.request_count} request{'s' if call.request_count != 1 else ''}){suffix}"
            )
        if row.error:
            lines.append(f"    error: {row.error}")
    if report.excluded_apis:
        lines.extend(["", "Excluded: " + ", ".join(report.excluded_apis)])
    lines.extend(["", f"Gate: {report.gate.status}"])
    for failure in report.gate.failures:
        route = f" {failure.route}" if failure.route else ""
        lines.append(f"  {failure.code}{route}: {failure.message}")
    if report.comparison is not None:
        lines.extend(["", f"Comparison: {report.comparison.status}"])
        for row in report.comparison.routes:
            lines.append(
                f"  {row.route}: {row.delta_ms:+.1f}ms "
                f"({row.delta_percent:+.1f}%) {row.status}"
            )
        for failure in report.comparison.failures:
            route = f" {failure.route}" if failure.route else ""
            lines.append(f"  {failure.code}{route}: {failure.message}")
    if save:
        lines.append(f"Report: {save}")
    return "\n".join(lines)


def _format_ms(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000:
        return f"{value / 1_000:.2f}s"
    return f"{value:.1f}ms"


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_048_576:
        return f"{value / 1_048_576:.1f}MB"
    if value >= 1_024:
        return f"{value / 1_024:.1f}KB"
    return f"{value}B"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":
    raise SystemExit(main())
