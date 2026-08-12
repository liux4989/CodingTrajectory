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
import json
import platform
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class DashboardApiBenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    generated_at: str
    environment: dict[str, str]
    fixture: BenchmarkFixture
    repeat: int = Field(ge=1)
    thresholds_ms: dict[str, float]
    results: list[ApiBenchmarkResult]
    excluded_apis: list[str]
    notes: list[str]


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
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.since_days < 1:
        parser.error("--since-days must be at least 1")
    if args.warn_ms <= 0 or args.critical_ms <= args.warn_ms:
        parser.error("thresholds must satisfy 0 < --warn-ms < --critical-ms")

    requested = _requested_apis(args)
    fixture = _discover_fixture(
        since_days=args.since_days,
        project_name=args.project_name,
        session_id=args.session_id,
        smallest=args.small,
        require_project=bool(
            requested & {"project-detail", "token-efficiency-project"}
        ),
        require_session=bool(requested & {"context-window", "evaluations"}),
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
                        repeat=args.repeat,
                        warn_ms=args.warn_ms,
                        critical_ms=args.critical_ms,
                    )
                )
    finally:
        service.shutdown()

    excluded = [name for name in ALL_API_NAMES if name not in requested]
    report = DashboardApiBenchmarkReport(
        generated_at=datetime.now(UTC).isoformat(),
        environment={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ct_command": str(web_services_mod.os.environ.get("CT_COMMAND") or "ct"),
        },
        fixture=fixture,
        repeat=args.repeat,
        thresholds_ms={
            "warning": float(args.warn_ms),
            "critical": float(args.critical_ms),
        },
        results=results,
        excluded_apis=excluded,
        notes=[
            "Cold runs clear dashboard projection and shared source caches before each call; warm runs immediately repeat the same call.",
            "nested_ct_median_ms sums dashboard-owned ct subprocess wall time; dashboard_median_ms is the remaining route and JSON serialization time.",
            "Agent, evaluation-start, cleanup-apply, and other state-changing APIs are excluded.",
            "Token-efficiency projections are included only with --include-expensive or an explicit --api selection.",
        ],
    )
    if not args.no_save:
        target = Path(args.save)
        if not target.is_absolute():
            target = _repo_root() / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(_render_report(report, save=None if args.no_save else args.save))
    return 1 if any(row.status == "error" for row in results) else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ct plugin dashboard benchmark",
        description=(
            "Benchmark read-only dashboard API routes and attribute latency to "
            "their nested ct calls."
        ),
    )
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--small",
        action="store_true",
        help="Use the smallest observed session graph instead of the largest.",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--warn-ms", type=float, default=1_000)
    parser.add_argument("--critical-ms", type=float, default=5_000)
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
        default="benchmarks/results/dashboard-api-baseline.json",
        help="JSON report path, relative to the repository root.",
    )
    parser.add_argument("--no-save", action="store_true")
    return parser


def _requested_apis(args: argparse.Namespace) -> set[str]:
    explicit = {
        name.strip() for value in args.api for name in value.split(",") if name.strip()
    }
    unknown = explicit - set(ALL_API_NAMES)
    if unknown:
        raise SystemExit(f"unknown dashboard API name: {sorted(unknown)[0]}")
    if explicit:
        return explicit
    requested = set(STANDARD_API_NAMES)
    if args.include_expensive:
        requested.update(EXPENSIVE_API_NAMES)
    return requested


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
        service.clear_caches()
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
    error = next((sample.error for sample in cold_runs if sample.error), None)
    if error:
        status: BenchmarkStatus = "error"
    elif cold_median is not None and cold_median >= critical_ms:
        status = "critical"
    elif cold_median is not None and cold_median >= warn_ms:
        status = "warning"
    else:
        status = "ok"
    calls = [call for sample in (*cold_runs, *warm_runs) for call in sample.calls]
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
        internal_calls=calls,
        error=error,
    )


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
    error: str | None = None
    try:
        payload = operation()
        response_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (Exception, SystemExit) as exc:
        error = _error_text(exc)
    elapsed_ms = (time.perf_counter() - started) * 1_000
    return _MeasuredRun(
        elapsed_ms=elapsed_ms,
        response_bytes=response_bytes,
        calls=tracer.finish(),
        error=error,
    )


@contextmanager
def _trace_dashboard_calls(tracer: _CallTracer) -> Iterator[None]:
    targets = [
        (web_services_mod, "_ct_json"),
        (web_services_mod, "_ct_json_expensive"),
        (context_window_mod, "_ct_json"),
        (cleanup_mod, "_ct_json"),
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
