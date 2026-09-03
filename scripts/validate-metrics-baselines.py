#!/usr/bin/env python3
"""Validate audited metric baselines against committed provider evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coding_trajectory.contracts import service_contract
from coding_trajectory.ingestion import ClaudeCodeAdapter, CodexAdapter, PiAdapter
from coding_trajectory.ingestion.graph import assemble_project_session_graphs
from coding_trajectory.discovery import stabilize_session
from coding_trajectory.ingestion.models import Vendor
from coding_trajectory.metrics import pricing
from coding_trajectory.query import DocumentStore
from coding_trajectory.service import IndexCache, dispatch
from coding_trajectory_cli._shared import compact_payload


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_ROOT = REPO_ROOT / "validation" / "metrics"
DEFAULT_MANIFEST = VALIDATION_ROOT / "manifest.toml"
DEFAULT_REPORT = REPO_ROOT / ".artifacts" / "metrics-quality-gate-report.json"
SURFACES = (
    "session.overview",
    "session.stats",
    "session.usage",
    "session.model_usage",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaselineCase(StrictModel):
    id: str
    status: Literal["candidate", "audited", "active", "superseded", "retired"]
    provider: Literal["codex_cli", "claude_code", "pi"]
    entrypoint_id: str
    source_files: list[str] = Field(min_length=1)
    expected_files: list[str] = Field(min_length=1)
    coverage: list[str] = Field(min_length=1)


class Manifest(StrictModel):
    schema_version: Literal[1]
    pricing_artifact: str
    cases: list[BaselineCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> Manifest:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


class PriceRuleArtifact(StrictModel):
    provider: str
    model: str
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cached_input_per_mtok: float | None = Field(default=None, ge=0)
    cache_creation_input_per_mtok: float | None = Field(default=None, ge=0)
    reasoning_output_per_mtok: float | None = Field(default=None, ge=0)


class PricingArtifact(StrictModel):
    schema_version: Literal[1]
    version: str
    rules: list[PriceRuleArtifact]


class Provenance(StrictModel):
    schema_version: Literal[1]
    case_id: str
    status: Literal["candidate", "audited", "active", "superseded", "retired"]
    provider: str
    adapter_family: str
    original_source_sha256: list[str]
    committed_source_sha256: dict[str, str]
    sanitization_version: str
    entrypoint_id: str
    coverage: list[str]
    expected_schema_versions: dict[str, int]
    pricing_artifact_version: str | None = None
    audit_date: str
    auditor: str
    reviewer: str
    last_contract_migration: str | None = None


class Assertion(StrictModel):
    path: str
    expected: Any
    source_refs: list[str] = Field(min_length=1)
    audit_ref: str


class ExpectedArtifact(StrictModel):
    schema_version: Literal[1]
    method: Literal[
        "session.overview",
        "session.stats",
        "session.usage",
        "session.model_usage",
    ]
    assertions: list[Assertion] = Field(min_length=1)


class Difference(StrictModel):
    case: str
    surface: str
    scope: str
    field: str
    expected: Any = None
    actual: Any = None
    source_refs: list[str] = Field(default_factory=list)
    audit_ref: str | None = None
    kind: Literal["schema", "value", "missing", "invariant", "artifact"]
    message: str


class CaseReport(StrictModel):
    case: str
    provider: str
    status: Literal["pass", "fail"]
    assertions: int = 0
    invariants: int = 0
    differences: list[Difference] = Field(default_factory=list)


class GateReport(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["pass", "fail"]
    pricing_artifact_version: str
    cases: list[CaseReport]


ADAPTERS = {
    "codex_cli": (CodexAdapter, Vendor.CODEX_CLI),
    "claude_code": (ClaudeCodeAdapter, Vendor.CLAUDE_CODE),
    "pi": (PiAdapter, Vendor.PI),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_model(path: Path, model: type[BaseModel]) -> BaseModel:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except ValidationError as exc:
        raise ValueError(f"invalid {path}: {exc}") from exc


def load_manifest(path: Path) -> Manifest:
    try:
        return Manifest.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid manifest {path}: {exc}") from exc


@contextmanager
def pinned_pricing(artifact: PricingArtifact) -> Iterator[None]:
    rules: dict[str, pricing.PriceRule] = {}
    for item in artifact.rules:
        rule = pricing.PriceRule(
            model=item.model,
            input_per_mtok=item.input_per_mtok,
            output_per_mtok=item.output_per_mtok,
            cached_input_per_mtok=item.cached_input_per_mtok,
            cache_creation_input_per_mtok=item.cache_creation_input_per_mtok,
            reasoning_output_per_mtok=item.reasoning_output_per_mtok,
            pricing_source=f"validation/metrics/pricing/{artifact.version}",
            pricing_effective_date=artifact.version,
        )
        rules[item.model] = rule
        rules[f"{item.provider}:{item.model}"] = rule

    original_rules = pricing._load_live_price_rules
    original_catalog = pricing._load_models_dev_cache
    original_preindexed = pricing._preindexed_price_rules
    pricing._load_live_price_rules = lambda *, now: rules
    pricing._load_models_dev_cache = lambda *, now, refresh: None
    pricing._preindexed_price_rules = lambda: {}
    try:
        yield
    finally:
        pricing._load_live_price_rules = original_rules
        pricing._load_models_dev_cache = original_catalog
        pricing._preindexed_price_rules = original_preindexed


def build_store(case: BaselineCase) -> DocumentStore:
    adapter_class, vendor = ADAPTERS[case.provider]
    sessions = []
    for relative in case.source_files:
        path = VALIDATION_ROOT / relative
        if not path.is_file():
            raise ValueError(f"missing source file: {path}")
        session = adapter_class().ingest_file(path)
        sessions.append(stabilize_session(session, vendor=vendor, source=path))
    graphs = assemble_project_session_graphs(f"metrics-baseline-{case.id}", sessions)
    return DocumentStore.from_session_graphs(graphs)


def project_surface(store: DocumentStore, case: BaselineCase, method: str) -> Any:
    payload = dispatch(
        method,
        {"session_id": case.entrypoint_id},
        store=store,
        global_scope=True,
        current_dir=REPO_ROOT,
        discovery_note=f"committed baseline {case.id}",
        cache=IndexCache(),
    )
    if method == "session.model_usage":
        # This surface is public through `ct api call`, whose result is the
        # versioned service payload rather than a CLI compact projection.
        return payload
    compact = compact_payload(method, payload)
    return service_contract(method).validate_cli_response(compact)


def resolve_path(payload: Any, path: str) -> tuple[bool, Any]:
    if not path.startswith("$"):
        raise ValueError(f"assertion path must start with $: {path}")
    current = payload
    cursor = 1
    while cursor < len(path):
        if path[cursor] == ".":
            cursor += 1
            end = cursor
            while end < len(path) and path[end] not in ".[":
                end += 1
            key = path[cursor:end]
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
            cursor = end
        elif path[cursor] == "[":
            end = path.find("]", cursor)
            if end < 0:
                raise ValueError(f"unterminated list index: {path}")
            try:
                index = int(path[cursor + 1 : end])
            except ValueError as exc:
                raise ValueError(f"invalid list index: {path}") from exc
            if not isinstance(current, list) or not 0 <= index < len(current):
                return False, None
            current = current[index]
            cursor = end + 1
        else:
            raise ValueError(f"invalid assertion path syntax: {path}")
    return True, current


def walk_dicts(value: Any, path: str = "$") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            yield from walk_dicts(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_dicts(item, f"{path}[{index}]")


def invariant_differences(case: BaselineCase, surfaces: dict[str, Any]) -> tuple[int, list[Difference]]:
    checked = 0
    differences: list[Difference] = []
    usage_payload = surfaces["session.usage"]
    for path, value in walk_dicts(usage_payload):
        if not {"processed", "prompt", "cached_prompt", "completion"}.issubset(value):
            continue
        checked += 1
        calculated = (
            value.get("uncached_prompt", value.get("prompt", 0))
            + value.get("cached_prompt", 0)
            + value.get("cache_write", 0)
            + value.get("completion", 0)
            + value.get("reasoning", 0)
        )
        if value["processed"] != calculated:
            differences.append(
                Difference(
                    case=case.id,
                    surface="session.usage",
                    scope=path,
                    field="processed",
                    expected=calculated,
                    actual=value["processed"],
                    kind="invariant",
                    message="processed tokens do not reconcile with prompt, cache, and completion buckets",
                )
            )
        if "reported_total" in value:
            checked += 1
            accepted_reported_totals = {
                value["processed"],
                value["processed"] - value.get("reasoning", 0),
            }
            if value["reported_total"] not in accepted_reported_totals:
                differences.append(
                    Difference(
                        case=case.id,
                        surface="session.usage",
                        scope=path,
                        field="reported_total",
                        expected=sorted(accepted_reported_totals),
                        actual=value["reported_total"],
                        kind="invariant",
                        message="reported total does not reconcile with processed tokens",
                    )
                )

    runtime = usage_payload.get("runtime") or {}
    if runtime:
        checked += 1
        if runtime.get("execution_seconds", 0) < 0 or runtime.get("wait_seconds", 0) < 0:
            differences.append(
                Difference(
                    case=case.id,
                    surface="session.usage",
                    scope="$.runtime",
                    field="execution_seconds",
                    actual=runtime,
                    kind="invariant",
                    message="runtime execution and wait durations must be non-negative",
                )
            )
        if runtime.get("start") and runtime.get("end"):
            checked += 1
            started_at = datetime.fromisoformat(runtime["start"].replace("Z", "+00:00"))
            ended_at = datetime.fromisoformat(runtime["end"].replace("Z", "+00:00"))
            elapsed_seconds = max((ended_at - started_at).total_seconds(), 0)
            if runtime.get("execution_seconds", 0) > round(elapsed_seconds) + 1:
                differences.append(
                    Difference(
                        case=case.id,
                        surface="session.usage",
                        scope="$.runtime",
                        field="execution_seconds",
                        expected=f"<= {round(elapsed_seconds) + 1}",
                        actual=runtime.get("execution_seconds"),
                        kind="invariant",
                        message="execution time exceeds the elapsed timestamp span",
                    )
                )

    graph_usage = usage_payload.get("graph_usage") or {}
    session_rows = usage_payload.get("sessions") or []
    if graph_usage and session_rows:
        for field in (
            "uncached_prompt",
            "cached_prompt",
            "cache_write",
            "completion",
            "reasoning",
            "processed",
        ):
            checked += 1
            session_total = sum((row.get("usage") or {}).get(field, 0) for row in session_rows)
            if graph_usage.get(field, 0) != session_total:
                differences.append(
                    Difference(
                        case=case.id,
                        surface="session.usage",
                        scope="$.graph_usage",
                        field=field,
                        expected=session_total,
                        actual=graph_usage.get(field),
                        kind="invariant",
                        message="graph usage does not reconcile with its distinct session sections",
                    )
                )

    for path, value in walk_dicts(usage_payload):
        if "cost" not in value:
            continue
        checked += 1
        if not isinstance(value.get("pricing"), dict):
            differences.append(
                Difference(
                    case=case.id,
                    surface="session.usage",
                    scope=path,
                    field="pricing",
                    actual=value.get("pricing"),
                    kind="invariant",
                    message="cost is present without pricing evidence",
                )
            )

    models = usage_payload.get("models") or []
    if models:
        checked += 1
        unpriced_models = [
            f"{model.get('provider')}:{model.get('model')}"
            for model in models
            if model.get("cost") is None
        ]
        if unpriced_models and usage_payload.get("cost") is not None:
            differences.append(
                Difference(
                    case=case.id,
                    surface="session.usage",
                    scope="$",
                    field="cost",
                    actual=usage_payload.get("cost"),
                    kind="invariant",
                    message=f"graph cost is present with unpriced models: {unpriced_models}",
                )
            )

    # Graph structure is intentionally no longer part of the default session
    # projection. Validate the multi-session invariant against the explicit
    # graph surface instead.
    overview_sessions = surfaces["graph.overview"].get("sessions") or []
    if "graph:multi-session" in case.coverage:
        checked += 1
        if len(overview_sessions) < 2:
            differences.append(
                Difference(
                    case=case.id,
                    surface="session.overview",
                    scope="$.sessions",
                    field="length",
                    expected=2,
                    actual=len(overview_sessions),
                    kind="invariant",
                    message="multi-session baseline collapsed to a single session",
                )
            )
    return checked, differences


def validate_case(case: BaselineCase, pricing_version: str) -> CaseReport:
    report = CaseReport(case=case.id, provider=case.provider, status="pass")
    case_root = VALIDATION_ROOT / "cases" / case.id
    provenance_path = case_root / "provenance.json"
    try:
        provenance = load_json_model(provenance_path, Provenance)
        assert isinstance(provenance, Provenance)
        if provenance.case_id != case.id or provenance.status != case.status:
            raise ValueError("provenance identity/status does not match manifest")
        if provenance.pricing_artifact_version not in {None, pricing_version}:
            raise ValueError("provenance references a different pricing artifact")
        actual_hashes = {
            relative: sha256(VALIDATION_ROOT / relative) for relative in case.source_files
        }
        if provenance.committed_source_sha256 != actual_hashes:
            raise ValueError("committed source hashes do not match provenance")
        store = build_store(case)
        surfaces = {method: project_surface(store, case, method) for method in SURFACES}
        surfaces["graph.overview"] = project_surface(store, case, "graph.overview")
        expected_methods: set[str] = set()
        for relative in case.expected_files:
            artifact = load_json_model(VALIDATION_ROOT / relative, ExpectedArtifact)
            assert isinstance(artifact, ExpectedArtifact)
            expected_methods.add(artifact.method)
            payload = surfaces[artifact.method]
            for assertion in artifact.assertions:
                report.assertions += 1
                found, actual = resolve_path(payload, assertion.path)
                if not found:
                    report.differences.append(
                        Difference(
                            case=case.id,
                            surface=artifact.method,
                            scope=assertion.path.rpartition(".")[0] or "$",
                            field=assertion.path,
                            expected=assertion.expected,
                            source_refs=assertion.source_refs,
                            audit_ref=assertion.audit_ref,
                            kind="missing",
                            message="expected public field is absent",
                        )
                    )
                elif actual != assertion.expected:
                    report.differences.append(
                        Difference(
                            case=case.id,
                            surface=artifact.method,
                            scope=assertion.path.rpartition(".")[0] or "$",
                            field=assertion.path,
                            expected=assertion.expected,
                            actual=actual,
                            source_refs=assertion.source_refs,
                            audit_ref=assertion.audit_ref,
                            kind="value",
                            message="public metric differs from audited expectation",
                        )
                    )
        missing_methods = set(SURFACES) - expected_methods
        if missing_methods:
            raise ValueError(f"missing expected artifacts for: {sorted(missing_methods)}")
        report.invariants, invariant_failures = invariant_differences(case, surfaces)
        report.differences.extend(invariant_failures)
    except (AssertionError, ValueError, ValidationError) as exc:
        report.differences.append(
            Difference(
                case=case.id,
                surface="artifacts",
                scope="$",
                field="bundle",
                kind="artifact",
                message=str(exc),
            )
        )
    report.status = "fail" if report.differences else "pass"
    return report


def print_report(report: GateReport) -> None:
    for case in report.cases:
        marker = "PASS" if case.status == "pass" else "FAIL"
        print(
            f"{marker} {case.case} ({case.provider}): "
            f"{case.assertions} assertions, {case.invariants} invariants"
        )
        for difference in case.differences:
            print(f"  surface: {difference.surface}")
            print(f"  scope: {difference.scope}")
            print(f"  field: {difference.field}")
            if difference.expected is not None:
                print(f"  expected: {json.dumps(difference.expected, sort_keys=True)}")
            if difference.actual is not None:
                print(f"  actual: {json.dumps(difference.actual, sort_keys=True)}")
            if difference.source_refs:
                print(f"  source refs: {', '.join(difference.source_refs)}")
            if difference.audit_ref:
                print(f"  audit ref: {difference.audit_ref}")
            print(f"  error: {difference.message}")
    print(f"metrics quality gate: {report.status.upper()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = load_manifest(args.manifest)
        pricing_path = VALIDATION_ROOT / manifest.pricing_artifact
        artifact = load_json_model(pricing_path, PricingArtifact)
        assert isinstance(artifact, PricingArtifact)
        selected = [case for case in manifest.cases if case.status == "active"]
        if args.case_ids:
            requested = set(args.case_ids)
            selected = [case for case in selected if case.id in requested]
            missing = requested - {case.id for case in selected}
            if missing:
                raise ValueError(f"unknown or inactive cases: {sorted(missing)}")
        with pinned_pricing(artifact):
            cases = [validate_case(case, artifact.version) for case in selected]
        report = GateReport(
            status="fail" if any(case.status == "fail" for case in cases) else "pass",
            pricing_artifact_version=artifact.version,
            cases=cases,
        )
    except (AssertionError, ValueError, ValidationError) as exc:
        print(f"metrics quality gate configuration error: {exc}", file=sys.stderr)
        return 2

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if not args.quiet or report.status == "fail":
        print_report(report)
        print(f"machine report: {args.report}")
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
