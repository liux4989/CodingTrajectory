import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  CircleCheck,
  CircleSlash,
  Flag,
  FlaskConical,
  Play,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { ProjectListResponse } from "./generated/project.list";
import type {
  Watch,
  Evaluation,
  Finding,
  DryRunResult,
  RefreshResult,
} from "./monitor-api";
import { monitorApi, type StrategyManifest, type WatchFormValue } from "./monitor-api";
import {
  Blank,
  ErrorNotice,
  EvidenceLink,
  Json,
  Loading,
  date,
  short,
} from "./shared";
import { useCore } from "./api";

function useMonitorData<T>(
  load: (signal?: AbortSignal) => Promise<T>,
  deps: unknown[] = [],
) {
  const [state, setState] = useState<{
    data?: T;
    error?: string;
    loading: boolean;
  }>({ loading: true });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true });
    load(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ data, loading: false });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setState({ error: String(error.message ?? error), loading: false });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);
  return { ...state, reload: () => setNonce((n) => n + 1) };
}

function ResultBadge({ evaluation }: { evaluation: Evaluation }) {
  if (evaluation.state === "completed" && evaluation.result === "pass")
    return <Badge variant="secondary">pass</Badge>;
  if (evaluation.state === "completed" && evaluation.result === "breach")
    return <Badge variant="destructive">breach</Badge>;
  if (evaluation.state === "unavailable")
    return <Badge variant="outline">unavailable</Badge>;
  if (evaluation.state === "pending")
    return <Badge variant="outline">pending</Badge>;
  return <Badge variant="destructive">errored</Badge>;
}

function SeverityBadge({ severity }: { severity: string }) {
  if (severity === "critical") return <Badge variant="destructive">critical</Badge>;
  if (severity === "warning") return <Badge>warning</Badge>;
  return <Badge variant="secondary">info</Badge>;
}

function scopeLabel(watch: Watch) {
  return watch.scope.session_id
    ? `Session ${short(watch.scope.session_id)}`
    : `Project ${watch.scope.project_name}`;
}

function EvaluationCard({
  evaluation,
  watches,
}: {
  evaluation: Evaluation;
  watches: Watch[];
}) {
  const watch = watches.find((item) => item.watch_id === evaluation.watch_id);
  const reference = {
    session_id: evaluation.reference.session_id,
    turn_id: evaluation.reference.turn_id ?? undefined,
  };
  return (
    <article className="evaluation-row">
      <div className="flex flex-wrap items-center gap-2">
        <ResultBadge evaluation={evaluation} />
        <Badge variant="outline">
          {evaluation.trigger === "historical_dry_run" ? "dry-run" : "refresh"}
        </Badge>
        <span className="small muted">revision {evaluation.config_revision}</span>
        {watch ? (
          <a className="evidence-link" href={`#/monitor/watches/${watch.watch_id}`}>
            {watch.name}
          </a>
        ) : (
          <span className="small muted">watch {short(evaluation.watch_id)}</span>
        )}
      </div>
      <p className="evaluation-summary">{evaluation.condition.summary}</p>
      {evaluation.reason && evaluation.state !== "completed" && (
        <p className="muted small">{evaluation.reason}</p>
      )}
      <p className="muted small">
        <code>{short(evaluation.reference.session_id)}</code> · turn{" "}
        <code>{short(evaluation.reference.turn_id ?? "")}</code> ·{" "}
        {date(evaluation.completed_at)} · ledger observations{" "}
        {String(
          (evaluation.coverage as Record<string, unknown> | undefined)?.[
            "request_ledger_observations"
          ] ?? "unavailable",
        )}
      </p>
      <div className="flex flex-wrap gap-3 items-center">
        <EvidenceLink reference={reference}>Open turn evidence</EvidenceLink>
        {evaluation.finding_id && (
          <a className="evidence-link" href="#/monitor/findings">
            <Flag size={13} /> Linked finding
          </a>
        )}
      </div>
      <details>
        <summary>Condition, coverage, and evaluator provenance</summary>
        <Json
          value={{
            condition: evaluation.condition,
            coverage: evaluation.coverage,
            evaluator: evaluation.evaluator,
            strategy: {
              strategy_id: evaluation.strategy_id,
              version: evaluation.strategy_version,
              config_revision: evaluation.config_revision,
            },
            evidence_fingerprint: evaluation.evidence_fingerprint,
            evaluation_id: evaluation.evaluation_id,
            superseded_by: evaluation.superseded_by,
          }}
        />
      </details>
    </article>
  );
}

function EvaluationList({
  evaluations,
  watches,
}: {
  evaluations: Evaluation[];
  watches: Watch[];
}) {
  if (!evaluations.length)
    return (
      <Blank title="No evaluations yet">
        Evaluations appear after a dry-run preview or an enabled refresh.
      </Blank>
    );
  return (
    <div>
      {evaluations.map((evaluation) => (
        <EvaluationCard
          key={evaluation.evaluation_id}
          evaluation={evaluation}
          watches={watches}
        />
      ))}
    </div>
  );
}

function RunSummary({ run }: { run: DryRunResult | RefreshResult }) {
  const summary = run.summary;
  const counts = {
    sessions: summary.sessions_examined ?? 0,
    evaluated: summary.turns_evaluated ?? 0,
    passed: summary.passed ?? 0,
    breached: summary.breached ?? 0,
    unavailable: summary.unavailable ?? 0,
    pending: summary.pending ?? 0,
    errored: summary.errored ?? 0,
    unchanged: summary.skipped_unchanged ?? 0,
  };
  return (
    <div className="run-summary">
      <div className="flex flex-wrap gap-2">
        <Badge variant="secondary">{counts.sessions} sessions</Badge>
        <Badge variant="secondary">{counts.evaluated} evaluated</Badge>
        <Badge variant="secondary">{counts.passed} pass</Badge>
        {counts.breached > 0 && (
          <Badge variant="destructive">{counts.breached} breach</Badge>
        )}
        {counts.unavailable > 0 && (
          <Badge variant="outline">{counts.unavailable} unavailable</Badge>
        )}
        {counts.pending > 0 && (
          <Badge variant="outline">{counts.pending} pending</Badge>
        )}
        {counts.errored > 0 && (
          <Badge variant="destructive">{counts.errored} errored</Badge>
        )}
        {counts.unchanged > 0 && (
          <Badge variant="outline">{counts.unchanged} unchanged</Badge>
        )}
      </div>
      {(run.notes ?? []).map((note) => (
        <p className="coverage-note" key={note}>
          {note}
        </p>
      ))}
    </div>
  );
}

function WatchForm({
  manifest,
  initial,
  onSaved,
}: {
  manifest: StrategyManifest;
  initial?: Watch;
  onSaved: (watch: Watch) => void;
}) {
  const inventory = useCore<ProjectListResponse>("project.list", {});
  const [name, setName] = useState(initial?.name ?? "");
  const [scopeType, setScopeType] = useState<"project" | "session">(
    initial?.scope.session_id ? "session" : "project",
  );
  const [projectName, setProjectName] = useState(
    initial?.scope.project_name ?? "",
  );
  const [sessionId, setSessionId] = useState(initial?.scope.session_id ?? "");
  const [measure, setMeasure] = useState<string>(
    initial?.config.measure ?? "processed_tokens",
  );
  const [threshold, setThreshold] = useState(
    String(initial?.config.threshold_tokens ?? 50000),
  );
  const [severity, setSeverity] = useState<string>(
    initial?.config.severity ?? "warning",
  );
  const [emitFindings, setEmitFindings] = useState(
    initial?.config.emit_findings ?? true,
  );
  const [state, setState] = useState<"idle" | "saving">("idle");
  const [error, setError] = useState<string>();
  const projects = Object.keys(inventory.data?.result.items ?? {});
  const thresholdValue = Number(threshold);
  const valid =
    name.trim().length > 0 &&
    (scopeType === "project" ? projectName.trim() : sessionId.trim()).length >
      0 &&
    Number.isInteger(thresholdValue) &&
    thresholdValue > 0;

  async function save() {
    setState("saving");
    setError(undefined);
    const value: WatchFormValue = {
      strategy_id: manifest.strategy_id,
      name: name.trim(),
      scope:
        scopeType === "project"
          ? { project_name: projectName.trim() }
          : { session_id: sessionId.trim() },
      config: {
        measure,
        threshold_tokens: thresholdValue,
        severity,
        emit_findings: emitFindings,
      },
    };
    try {
      const watch = initial
        ? await monitorApi.updateWatch(initial.watch_id, {
            name: value.name,
            scope: value.scope,
            config: value.config,
          })
        : await monitorApi.createWatch(value);
      onSaved(watch);
    } catch (cause) {
      setError(String((cause as Error).message ?? cause));
      setState("idle");
    }
  }

  return (
    <FieldGroup>
      <Field>
        <FieldLabel htmlFor="watch-name">Watch name</FieldLabel>
        <Input
          id="watch-name"
          maxLength={160}
          placeholder="e.g. Codex turn budget"
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </Field>
      <Field>
        <FieldLabel>Scope</FieldLabel>
        <ToggleGroup
          type="single"
          variant="outline"
          value={scopeType}
          onValueChange={(value) => {
            if (value === "project" || value === "session") setScopeType(value);
          }}
        >
          <ToggleGroupItem value="project">Project</ToggleGroupItem>
          <ToggleGroupItem value="session">Session</ToggleGroupItem>
        </ToggleGroup>
      </Field>
      {scopeType === "project" ? (
        <Field>
          <FieldLabel htmlFor="watch-project">Project name</FieldLabel>
          <Input
            id="watch-project"
            list="monitor-projects"
            placeholder="Project as recorded by Core"
            value={projectName}
            onChange={(event) => setProjectName(event.target.value)}
          />
          <datalist id="monitor-projects">
            {projects.map((project) => (
              <option key={project} value={project} />
            ))}
          </datalist>
          <p className="muted small">
            Core project inventory is unpaginated. A project whose logs exist
            but is not classified as a project still matches by recorded
            working directory.
          </p>
        </Field>
      ) : (
        <Field>
          <FieldLabel htmlFor="watch-session">Session ID</FieldLabel>
          <Input
            id="watch-session"
            placeholder="Canonical session or graph ID"
            value={sessionId}
            onChange={(event) => setSessionId(event.target.value)}
          />
        </Field>
      )}
      <Field>
        <FieldLabel htmlFor="watch-measure">Native token measure</FieldLabel>
        <Select value={measure} onValueChange={setMeasure}>
          <SelectTrigger id="watch-measure" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {Object.entries(manifest.measures).map(([key, description]) => (
                <SelectItem key={key} value={key}>
                  {key} — {description}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        <p className="muted small">
          Token count is not currency cost. Core owns the measurement and its
          formula; the watch owns only this threshold.
        </p>
      </Field>
      <Field>
        <FieldLabel htmlFor="watch-threshold">Budget threshold (tokens)</FieldLabel>
        <Input
          id="watch-threshold"
          inputMode="numeric"
          value={threshold}
          onChange={(event) => setThreshold(event.target.value)}
        />
        <p className="muted small">
          A turn breaches when the observed measure is greater than this
          threshold.
        </p>
      </Field>
      <Field>
        <FieldLabel htmlFor="watch-severity">Finding severity</FieldLabel>
        <Select value={severity} onValueChange={setSeverity}>
          <SelectTrigger id="watch-severity" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {manifest.severities.map((level) => (
                <SelectItem key={level} value={level}>
                  {level}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </Field>
      <Field orientation="horizontal">
        <Switch
          id="watch-emit"
          checked={emitFindings}
          onCheckedChange={setEmitFindings}
        />
        <FieldLabel htmlFor="watch-emit" className="font-normal">
          Emit findings for breaches when the watch is enabled
        </FieldLabel>
      </Field>
      {error && (
        <Alert variant="destructive">
          <AlertTitle>Watch not saved</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div className="flex gap-2 items-center">
        <Button disabled={!valid || state === "saving"} onClick={save}>
          {state === "saving"
            ? "Saving…"
            : initial
              ? "Save as new revision"
              : "Save watch"}
        </Button>
        {initial && (
          <span className="small muted">
            Configuration changes create revision{" "}
            {(initial.config_revision ?? 1) + 1}; earlier evaluations stay
            pinned to their revision.
          </span>
        )}
        {!initial && (
          <span className="small muted">
            Saving stores configuration only. Nothing runs until you dry-run or
            enable the watch.
          </span>
        )}
      </div>
    </FieldGroup>
  );
}

function PermissionPanel({ manifest }: { manifest: StrategyManifest }) {
  return (
    <div className="permission-panel">
      <h3>Permissions and evidence boundary</h3>
      <ul>
        {manifest.permissions.map((permission) => (
          <li key={permission.id}>
            <div className="flex gap-2 items-center">
              {permission.detail.startsWith("Not requested") ? (
                <CircleSlash size={14} aria-hidden="true" />
              ) : (
                <CircleCheck size={14} aria-hidden="true" />
              )}
              <strong>{permission.label}</strong>
            </div>
            <p className="muted small">{permission.detail}</p>
          </li>
        ))}
      </ul>
      <p className="muted small">
        Evaluations reference canonical evidence by ID and store measured values
        and thresholds only. Transcript and event bodies never leave Core.
      </p>
    </div>
  );
}

export function MonitorStrategies() {
  const strategies = useMonitorData(monitorApi.strategies);
  const watches = useMonitorData(monitorApi.watches);
  const manifest = strategies.data?.[0];
  return (
    <section className="explore monitor-page">
      <h1>Monitor · Strategies</h1>
      <p className="lede">
        Human-approved monitoring policy over local Core evidence.
      </p>
      <ErrorNotice message={strategies.error ?? watches.error} />
      {strategies.loading && <Loading />}
      {manifest && (
        <Card className="strategy-card">
          <CardHeader>
            <CardTitle>
              {manifest.name}{" "}
              <Badge variant="outline">v{manifest.version}</Badge>
            </CardTitle>
            <CardDescription>{manifest.description}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              <Badge variant="secondary">
                {manifest.evaluator.type} · {manifest.evaluator.rule} v
                {manifest.evaluator.rule_version}
              </Badge>
              <Badge variant="outline">{manifest.input_window}</Badge>
            </div>
            <p className="small">{manifest.finding_policy}</p>
            <dl className="provenance strategy-meta">
              <div>
                <dt>Triggers</dt>
                <dd>
                  {Object.entries(manifest.trigger).map(([key, text]) => (
                    <p key={key} className="small">
                      <code>{key}</code> — {text}
                    </p>
                  ))}
                </dd>
              </div>
              <div>
                <dt>Core methods</dt>
                <dd>
                  <code>
                    {Object.entries(manifest.required_core_methods)
                      .map(([method, version]) => `${method} v${version}`)
                      .join(" · ")}
                  </code>
                </dd>
              </div>
            </dl>
            <PermissionPanel manifest={manifest} />
          </CardContent>
        </Card>
      )}
      <div className="section-heading">
        <h2>Watches</h2>
        <span className="muted small">{watches.data?.length ?? 0} configured</span>
      </div>
      {watches.data?.map((watch) => (
        <article className="session-row watch-row" key={watch.watch_id}>
          <div>
            <div className="flex flex-wrap gap-2">
              {watch.enabled ? (
                <Badge>enabled</Badge>
              ) : (
                <Badge variant="outline">disabled</Badge>
              )}
              <Badge variant="secondary">
                revision {watch.config_revision ?? 1}
              </Badge>
              <SeverityBadge severity={watch.config.severity ?? "warning"} />
            </div>
            <h3>
              <a href={`#/monitor/watches/${watch.watch_id}`}>{watch.name}</a>
            </h3>
            <p className="muted small">
              {scopeLabel(watch)} · {watch.config.measure} ≤{" "}
              {watch.config.threshold_tokens.toLocaleString()} · findings{" "}
              {watch.config.emit_findings ? "on breach" : "suppressed"}
            </p>
          </div>
          <Button asChild variant="outline">
            <a href={`#/monitor/watches/${watch.watch_id}`}>Open</a>
          </Button>
        </article>
      ))}
      {watches.data && !watches.data.length && (
        <p className="muted small">
          No watches yet. Configure one below, then dry-run it before enabling.
        </p>
      )}
      {manifest && (
        <Card className="strategy-card">
          <CardHeader>
            <CardTitle>Configure a watch</CardTitle>
            <CardDescription>
              A watch binds the strategy to one scope, measure, threshold, and
              finding policy. You approve the semantics; agents only execute
              them.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <WatchForm
              manifest={manifest}
              onSaved={(watch) => {
                location.hash = `#/monitor/watches/${watch.watch_id}`;
              }}
            />
          </CardContent>
        </Card>
      )}
    </section>
  );
}

export function MonitorWatchDetail({ watchId }: { watchId: string }) {
  const detail = useMonitorData((signal) => monitorApi.watch(watchId, signal));
  const strategies = useMonitorData(monitorApi.strategies);
  const evaluations = useMonitorData((signal) =>
    monitorApi.evaluations({ watch_id: watchId, limit: 50 }, signal),
  );
  const findings = useMonitorData((signal) =>
    monitorApi.findings({ watch_id: watchId }, signal),
  );
  const watches = useMonitorData(monitorApi.watches);
  const [editing, setEditing] = useState(false);
  const [runState, setRunState] = useState<"idle" | "running">("idle");
  const [dryRunResult, setDryRunResult] = useState<DryRunResult>();
  const [refreshResult, setRefreshResult] = useState<RefreshResult>();
  const [actionError, setActionError] = useState<string>();
  const watch = detail.data;
  const manifest = strategies.data?.find(
    (item) => item.strategy_id === watch?.strategy_id,
  );

  async function act(kind: "dry-run" | "refresh" | "toggle") {
    if (!watch) return;
    setRunState("running");
    setActionError(undefined);
    try {
      if (kind === "dry-run") {
        setDryRunResult(await monitorApi.dryRun(watch.watch_id));
      } else if (kind === "refresh") {
        setRefreshResult(await monitorApi.refresh(watch.watch_id));
      } else {
        await monitorApi.updateWatch(watch.watch_id, {
          enabled: !watch.enabled,
        });
      }
      detail.reload();
      evaluations.reload();
      findings.reload();
      watches.reload();
    } catch (cause) {
      setActionError(String((cause as Error).message ?? cause));
    } finally {
      setRunState("idle");
    }
  }

  if (detail.error)
    return (
      <section className="explore monitor-page">
        <ErrorNotice message={detail.error} />
      </section>
    );
  if (!watch) return <section className="explore monitor-page"><Loading /></section>;
  return (
    <section className="explore monitor-page">
      <a className="back-link" href="#/monitor/strategies">
        <ArrowLeft size={15} />
        Strategies
      </a>
      <div className="section-heading">
        <h1>{watch.name}</h1>
        <div className="flex gap-2">
          {watch.enabled ? (
            <Badge>enabled</Badge>
          ) : (
            <Badge variant="outline">disabled</Badge>
          )}
          <Badge variant="secondary">
            revision {watch.config_revision ?? 1}
          </Badge>
        </div>
      </div>
      <p className="muted">
        {watch.strategy_id} v{watch.strategy_version} · {scopeLabel(watch)} ·{" "}
        {watch.config.measure} ≤ {watch.config.threshold_tokens.toLocaleString()}{" "}
        tokens · severity {watch.config.severity ?? "warning"} · findings{" "}
        {watch.config.emit_findings ? "on breach" : "suppressed"}
      </p>
      {watch.refresh?.last_run_at && (
        <p className="muted small">
          Last refresh {date(watch.refresh.last_run_at)} ·{" "}
          {watch.refresh.caught_up
            ? "caught up with the local change feed"
            : "more changed sessions remain"}
        </p>
      )}
      <div className="flex flex-wrap gap-2 watch-actions">
        <Button
          variant="outline"
          disabled={runState === "running"}
          onClick={() => act("dry-run")}
        >
          <FlaskConical data-icon="inline-start" />
          Dry-run history
        </Button>
        <Button
          variant="outline"
          disabled={runState === "running" || !watch.enabled}
          onClick={() => act("refresh")}
        >
          <Play data-icon="inline-start" />
          Refresh now
        </Button>
        <Button
          variant={watch.enabled ? "outline" : "default"}
          disabled={runState === "running"}
          onClick={() => act("toggle")}
        >
          <ShieldCheck data-icon="inline-start" />
          {watch.enabled ? "Disable" : "Enable"}
        </Button>
        <Button variant="ghost" onClick={() => setEditing((value) => !value)}>
          {editing ? "Close editor" : "Edit configuration"}
        </Button>
      </div>
      {!watch.enabled && (
        <p className="coverage-note">
          Refresh stays off until you explicitly enable this watch. Dry-run is a
          labeled historical preview and never emits live findings.
        </p>
      )}
      {actionError && (
        <Alert variant="destructive">
          <AlertTitle>Action failed</AlertTitle>
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      )}
      {editing && manifest && (
        <Card className="strategy-card">
          <CardHeader>
            <CardTitle>Edit configuration</CardTitle>
            <CardDescription>
              Saving creates revision {(watch.config_revision ?? 1) + 1} and
              resets the refresh position so the new policy re-observes the
              scope.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <WatchForm
              manifest={manifest}
              initial={watch}
              onSaved={() => {
                setEditing(false);
                detail.reload();
                evaluations.reload();
                findings.reload();
              }}
            />
          </CardContent>
        </Card>
      )}
      {dryRunResult && (
        <section>
          <h2>Dry-run preview</h2>
          <RunSummary run={dryRunResult} />
          {dryRunResult.prospective_findings.length > 0 && (
            <Alert>
              <TriangleAlert />
              <AlertTitle>
                {dryRunResult.prospective_findings.length} prospective finding(s)
              </AlertTitle>
              <AlertDescription>
                Enabling this watch would emit findings for the breach
                evaluations below. Dry-run did not create them.
              </AlertDescription>
            </Alert>
          )}
          <EvaluationList
            evaluations={dryRunResult.evaluations}
            watches={watches.data ?? []}
          />
        </section>
      )}
      {refreshResult && (
        <section>
          <h2>Latest refresh</h2>
          <RunSummary run={refreshResult} />
          {refreshResult.findings.length > 0 && (
            <Alert>
              <TriangleAlert />
              <AlertTitle>
                {refreshResult.findings.length} finding(s) emitted
              </AlertTitle>
              <AlertDescription>
                Breaches against the enabled watch created findings, listed
                under Findings below.
              </AlertDescription>
            </Alert>
          )}
          <EvaluationList
            evaluations={refreshResult.evaluations}
            watches={watches.data ?? []}
          />
        </section>
      )}
      <section>
        <div className="section-heading">
          <h2>Evaluations</h2>
          <span className="muted small">
            {evaluations.data?.length ?? 0} latest records
          </span>
        </div>
        <ErrorNotice message={evaluations.error} />
        {evaluations.loading && <Loading />}
        {evaluations.data && (
          <EvaluationList
            evaluations={evaluations.data}
            watches={watches.data ?? []}
          />
        )}
      </section>
      <section>
        <div className="section-heading">
          <h2>Findings</h2>
          <span className="muted small">{findings.data?.length ?? 0}</span>
        </div>
        <ErrorNotice message={findings.error} />
        {findings.data && (
          <FindingList
            findings={findings.data}
            watches={watches.data ?? []}
            onChanged={() => {
              findings.reload();
              evaluations.reload();
            }}
          />
        )}
      </section>
      <details>
        <summary>Revision history</summary>
        <Json value={watch.revisions} />
      </details>
    </section>
  );
}

const FINDING_STATUSES = ["open", "acknowledged", "resolved", "dismissed"];

function FindingList({
  findings,
  watches,
  onChanged,
}: {
  findings: Finding[];
  watches: Watch[];
  onChanged: () => void;
}) {
  const [error, setError] = useState<string>();
  async function transition(finding: Finding, status: string) {
    setError(undefined);
    try {
      await monitorApi.setFindingStatus(finding.finding_id, status);
      onChanged();
    } catch (cause) {
      setError(String((cause as Error).message ?? cause));
    }
  }
  if (!findings.length)
    return (
      <Blank title="No findings in this state">
        Findings appear when an enabled watch observes a configured breach.
      </Blank>
    );
  return (
    <div>
      {error && (
        <Alert variant="destructive">
          <AlertTitle>Status not changed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {findings.map((finding) => {
        const watch = watches.find(
          (item) => item.watch_id === finding.watch_id,
        );
        return (
          <article className="finding-row" key={finding.finding_id}>
            <div className="flex flex-wrap items-center gap-2">
              <SeverityBadge severity={finding.severity} />
              <Badge variant="outline">{finding.status}</Badge>
              <span className="small muted">
                revision {finding.config_revision}
              </span>
              {watch && (
                <a
                  className="evidence-link"
                  href={`#/monitor/watches/${watch.watch_id}`}
                >
                  {watch.name}
                </a>
              )}
            </div>
            <p className="evaluation-summary">{finding.title}</p>
            <p className="muted small">
              <code>{short(finding.reference.session_id)}</code> · turn{" "}
              <code>{short(finding.reference.turn_id ?? "")}</code> · opened{" "}
              {date(finding.created_at)} · updated {date(finding.updated_at)}
            </p>
            <div className="flex flex-wrap gap-2 items-center">
              <EvidenceLink
                reference={{
                  session_id: finding.reference.session_id,
                  turn_id: finding.reference.turn_id ?? undefined,
                }}
              >
                Open exact turn evidence
              </EvidenceLink>
              {finding.status === "open" && (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => transition(finding, "acknowledged")}
                  >
                    Acknowledge
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => transition(finding, "resolved")}
                  >
                    Resolve
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => transition(finding, "dismissed")}
                  >
                    Dismiss
                  </Button>
                </>
              )}
              {finding.status === "acknowledged" && (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => transition(finding, "resolved")}
                  >
                    Resolve
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => transition(finding, "dismissed")}
                  >
                    Dismiss
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => transition(finding, "open")}
                  >
                    Reopen
                  </Button>
                </>
              )}
              {(finding.status === "resolved" ||
                finding.status === "dismissed") && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => transition(finding, "open")}
                >
                  Reopen
                </Button>
              )}
            </div>
            <details>
              <summary>Condition and lifecycle history</summary>
              <Json
                value={{
                  condition: finding.condition,
                  status_history: finding.status_history,
                  evaluation_id: finding.evaluation_id,
                  finding_id: finding.finding_id,
                }}
              />
            </details>
          </article>
        );
      })}
    </div>
  );
}

export function MonitorFindings() {
  const [status, setStatus] = useState("open");
  const findings = useMonitorData(
    (signal) =>
      monitorApi.findings(status === "all" ? {} : { status }, signal),
    [status],
  );
  const watches = useMonitorData(monitorApi.watches);
  return (
    <section className="explore monitor-page">
      <h1>Monitor · Findings</h1>
      <p className="lede">
        Actionable work items emitted by enabled watches. Triage changes state,
        never the underlying evaluation or evidence.
      </p>
      <Tabs value={status} onValueChange={setStatus}>
        <TabsList>
          {[...FINDING_STATUSES, "all"].map((value) => (
            <TabsTrigger key={value} value={value}>
              {value}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
      <ErrorNotice message={findings.error} />
      {findings.loading && <Loading />}
      {findings.data && (
        <FindingList
          findings={findings.data}
          watches={watches.data ?? []}
          onChanged={findings.reload}
        />
      )}
    </section>
  );
}

export function MonitorActivity() {
  const watches = useMonitorData(monitorApi.watches);
  const [watchId, setWatchId] = useState("");
  const [trigger, setTrigger] = useState("");
  const [state, setState] = useState("");
  const evaluations = useMonitorData(
    (signal) =>
      monitorApi.evaluations(
        {
          watch_id: watchId || undefined,
          trigger: trigger || undefined,
          state: state || undefined,
          limit: 100,
        },
        signal,
      ),
    [watchId, trigger, state],
  );
  const summary = useMemo(() => {
    const counts = { pass: 0, breach: 0, unavailable: 0, pending: 0, errored: 0 };
    for (const evaluation of evaluations.data ?? []) {
      if (evaluation.state === "completed" && evaluation.result === "pass")
        counts.pass += 1;
      else if (evaluation.state === "completed" && evaluation.result === "breach")
        counts.breach += 1;
      else if (evaluation.state === "unavailable") counts.unavailable += 1;
      else if (evaluation.state === "pending") counts.pending += 1;
      else counts.errored += 1;
    }
    return counts;
  }, [evaluations.data]);
  return (
    <section className="explore monitor-page">
      <h1>Monitor · Activity</h1>
      <p className="lede">
        Every evaluation a watch has attempted — passes, breaches, unavailable
        evidence, and errors — so rates and coverage stay honest.
      </p>
      <div className="monitor-filters">
        <Field>
          <FieldLabel htmlFor="filter-watch">Watch</FieldLabel>
          <Select value={watchId} onValueChange={setWatchId}>
            <SelectTrigger id="filter-watch">
              <SelectValue placeholder="All watches" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {(watches.data ?? []).map((watch) => (
                  <SelectItem key={watch.watch_id} value={watch.watch_id}>
                    {watch.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor="filter-trigger">Trigger</FieldLabel>
          <Select value={trigger} onValueChange={setTrigger}>
            <SelectTrigger id="filter-trigger">
              <SelectValue placeholder="All triggers" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="historical_dry_run">dry-run</SelectItem>
                <SelectItem value="manual_refresh">refresh</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel htmlFor="filter-state">State</FieldLabel>
          <Select value={state} onValueChange={setState}>
            <SelectTrigger id="filter-state">
              <SelectValue placeholder="All states" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="completed">completed</SelectItem>
                <SelectItem value="unavailable">unavailable</SelectItem>
                <SelectItem value="pending">pending</SelectItem>
                <SelectItem value="errored">errored</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        {(watchId || trigger || state) && (
          <Button
            variant="ghost"
            onClick={() => {
              setWatchId("");
              setTrigger("");
              setState("");
            }}
          >
            Clear filters
          </Button>
        )}
      </div>
      <div className="flex flex-wrap gap-2">
        <Badge variant="secondary">{summary.pass} pass</Badge>
        {summary.breach > 0 && (
          <Badge variant="destructive">{summary.breach} breach</Badge>
        )}
        {summary.unavailable > 0 && (
          <Badge variant="outline">{summary.unavailable} unavailable</Badge>
        )}
        {summary.pending > 0 && (
          <Badge variant="outline">{summary.pending} pending</Badge>
        )}
        {summary.errored > 0 && (
          <Badge variant="destructive">{summary.errored} errored</Badge>
        )}
      </div>
      <ErrorNotice message={evaluations.error} />
      {evaluations.loading && <Loading />}
      {evaluations.data && (
        <EvaluationList
          evaluations={evaluations.data}
          watches={watches.data ?? []}
        />
      )}
    </section>
  );
}

export function MonitorHome({ route }: { route: { view: string; watchId?: string } }) {
  if (route.view === "strategies") return <MonitorStrategies />;
  if (route.view === "findings") return <MonitorFindings />;
  if (route.view === "watch" && route.watchId)
    return <MonitorWatchDetail key={route.watchId} watchId={route.watchId} />;
  return <MonitorActivity />;
}
