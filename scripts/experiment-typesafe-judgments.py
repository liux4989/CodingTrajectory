"""Run TypeSafe (Jev) judgment experiments over local coding-session evidence.

Experiments:
  1. session-intent   Choice: classify opening user messages by work type
  2. finding-triage   Score+Noul: severity and interruption-worthiness of
                      Monitor-style findings
  3. read-routing     Choice: map natural-language asks to core read methods
  4. session-rerank   Choice-over-candidates vs per-candidate Noul relevance

Usage:
  uv run --with typesafe-sdk python scripts/experiment-typesafe-judgments.py

Reads TYPESAFE_API_KEY from the environment, falling back to the gitignored
project .env. Prints a JSON results document to stdout.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

ROOT = pathlib.Path(__file__).resolve().parent.parent

if not os.environ.get("TYPESAFE_API_KEY"):
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("TYPESAFE_API_KEY="):
            os.environ["TYPESAFE_API_KEY"] = line.split("=", 1)[1].strip()

client = TypeSafeClient()

# --- Experiment 1: session intent classification -----------------------------
# Titles are opening user messages of real local sessions (coding-trajectory
# project), with hand-assigned ground-truth labels.
INTENT_OPTIONS = {
    "bugfix": "Fix a defect, error, or regression in existing behavior",
    "feature": "Add a new capability or user-facing functionality",
    "refactor": "Restructure code internals without changing observable behavior",
    "ui_change": "Change the visual design, layout, or interaction of a user interface",
    "removal": "Delete code, features, config, or dependencies",
    "investigation": "Understand, diagnose, analyze, or answer a question; may not change code",
    "docs": "Write or update documentation only",
    "ops": "Repository or environment chores: commit, merge, publish, CI, tooling setup",
}

INTENT_CASES: list[tuple[str, str]] = [
    # (opening user message, expected label)
    ("@packages/core/ for core session datas, we should remove the 'price' since it involving too many heuristic logic. The current token cost is enought", "removal"),
    ("@packages/plugins/dashboard/dashboard_tui.py relet us remove the tui and it's dependcies", "removal"),
    ("why there are so many 'no token usage metrics found for session', for 'coding-trajectory 019f12ed-e1fd-79f3-a258-dcdd5dcc107e'", "investigation"),
    ("@packages/plugins/dashboard/ we want to change the current 'navigation/routing' to a better experience - the dashboard will query daily sessions instead 30 days", "ui_change"),
    ("@packages/plugins/dashboard/web/src/routes/context-window.tsx let us refactor the ux - we have two progress bar : a progress bar combine the differnt category and a context timeline to to visualized", "ui_change"),
    ("fix bug : ct session items 9893af50-fa7c-5f15-a2bb-eb8fd5f15dd1 [] Note: Session id : 019f13c4-90ec-79e6-8920-c69b0cca2bba", "bugfix"),
    ("remove the ci actions from this project", "removal"),
    ("@packages/plugins/dashboard/web/src/routes/context-window.tsx 1. let us combine the two context window timeline. Instead create a timeline sequence, let us combine the catgory segments. 2. also add", "ui_change"),
    ("commit", "ops"),
    ("let us import motion for our @packages/plugins/dashboard/ web ui. And enrich the ux with fluent motion when need", "feature"),
    ("@packages/plugins/dashboard/ currently 'largely a custom design system' - we want to build a 'design token' to unfified the ui style - and we should follow the shadercn and taildwind css best practice", "feature"),
    ("why 'dist/assets/index-DO4QQnUX.js 588.76 kB' is so big in 'bun run build'", "investigation"),
    ("why @.gitignore don't include node_module", "investigation"),
    ("merge this barnch to main. and do a cleanup", "ops"),
    ("why ct session stats 019f8358-347a-70f2-b69b-96bcbe1ddd91 reports prompt 2.2m but codex itself reports : Token usage: total=147,135", "investigation"),
    ("help me fix error : claude update Current version: 2.1.201 Checking for updates to latest version... Updating", "bugfix"),
]

# --- Experiment 2: Monitor finding triage ------------------------------------
TRIAGE_LEVELS = [
    "Everything is normal; no human attention needed",
    "Noteworthy but harmless; log it, no action needed soon",
    "Needs human attention soon; task quality or budget is degrading",
    "Needs immediate human attention; work is blocked, runaway, or sensitive data is at risk",
]

TRIAGE_CASES: list[tuple[str, dict[str, Any], int]] = [
    # (name, finding state, expected severity band 0-3)
    ("normal_progress", {
        "finding": {"watch": "turn_token_budget", "observed": "turn tokens 12,400 of 200,000 budget", "expected": "turns stay under budget"},
        "session_evidence": {"turns_completed": 8, "last_turn_outcome": "tests passing, task progressing"},
    }, 0),
    ("idle_after_completion", {
        "finding": {"watch": "session_idle", "observed": "no activity for 45 minutes", "expected": "active sessions show activity"},
        "session_evidence": {"last_turn_outcome": "task completed, summary delivered to user"},
    }, 0),
    ("budget_92pct_midtask", {
        "finding": {"watch": "turn_token_budget", "observed": "turn at 92% of 200,000 token budget", "expected": "turns stay under budget"},
        "session_evidence": {"turns_completed": 3, "last_turn_outcome": "mid-task, agent editing files"},
    }, 1),
    ("cost_spike_single_request", {
        "finding": {"watch": "request_cost", "observed": "one provider request cost 4.2x the session median", "expected": "request costs stay near the median"},
        "session_evidence": {"last_turn_outcome": "task progressing", "median_request_tokens": 38000, "spike_request_tokens": 161000},
    }, 1),
    ("repeated_failing_test_loop", {
        "finding": {"watch": "tool_loop", "observed": "agent re-ran the same failing test 6 times", "expected": "agent changes approach after failures"},
        "session_evidence": {"code_changes_between_runs": 0, "last_turn_outcome": "same assertion failing identically"},
    }, 2),
    ("budget_exceeded_no_progress", {
        "finding": {"watch": "turn_token_budget", "observed": "3 consecutive turns exceeded the token budget", "expected": "turns stay under budget"},
        "session_evidence": {"user_visible_progress_in_those_turns": "none", "files_changed": 0},
    }, 2),
    ("secret_file_leaked", {
        "finding": {"watch": "sensitive_path", "observed": "agent edited .env and printed its full contents into the transcript", "expected": "secrets are never read into the transcript"},
        "session_evidence": {"file": ".env", "contained": "API tokens"},
    }, 3),
]

# --- Experiment 3: natural-language read routing -----------------------------
ROUTE_OPTIONS = {
    "session.tree": "Show ordinary human conversation forks and their agent-run counts",
    "session.overview": "Show a compact session hierarchy",
    "session.summary": "Show a bounded, evidence-backed session brief",
    "session.search": "Search canonical session evidence with deterministic structural ranking",
    "session.stats": "Show compact context/token usage composition",
    "session.usage": "Show turn-level token usage and request-summed cost",
    "session.request_usage": "Show exact provider-request usage and cost",
    "session.events": "Lazily load local events within a published session or turn",
    "session.items": "Query items within a session by explicit IDs or full session scope",
    "session.graph": "Inspect the selected branch's internal multi-agent graph",
    "project.list": "List projects",
    "project.sessions": "List sessions within a project",
    "none_of_these": "None of these reads can answer the request",
}

ROUTE_CASES: list[tuple[str, str]] = [
    ("Give me a short brief of what happened in this session", "session.summary"),
    ("I want a compact outline of the parts of this session", "session.overview"),
    ("Did this conversation fork into branches? How many agent runs are on each?", "session.tree"),
    ("Find every mention of prepared-graphs.sqlite in this session", "session.search"),
    ("How is the context window split between system prompt, tools, and messages?", "session.stats"),
    ("What was the token usage of each turn and the summed cost?", "session.usage"),
    ("Show me the exact per-request usage reported by the provider, including cache writes", "session.request_usage"),
    ("Load the raw events for turn 12 of this session", "session.events"),
    ("Get all items in this session so I can filter them myself", "session.items"),
    ("Which subagents did this orchestration run spawn and how are they connected?", "session.graph"),
    ("What projects exist on this machine?", "project.list"),
    ("List the sessions I have in the langfuse project", "project.sessions"),
    ("What's the weather in Lisbon?", "none_of_these"),
    ("Rewrite this function to be async", "none_of_these"),
]

# --- Experiment 4: session search rerank -------------------------------------
# Candidate pools are real session titles; relevance labels are hand-assigned.
RERANK_QUERIES: list[dict[str, Any]] = [
    {
        "query": "redesigning the dashboard web UI",
        "candidates": [
            ("c01", "@packages/plugins/dashboard/ we want to change the current 'navigation/routing' to a better experience - the dashboard will query daily sessions instead 30 days", True),
            ("c02", "@packages/core/ for core session datas, we should remove the 'price' since it involving too many heuristic logic", False),
            ("c03", "commit", False),
            ("c04", "@packages/plugins/dashboard/ currently 'largely a custom design system' - we want to build a 'design token' to unfified the ui style", True),
            ("c05", "why @.gitignore don't include node_module", False),
            ("c06", "let us import motion for our @packages/plugins/dashboard/ web ui. And enrich the ux with fluent motion when need", True),
            ("c07", "fix bug : ct session items 9893af50-fa7c-5f15-a2bb-eb8fd5f15dd1 []", False),
            ("c08", "about the @packages/plugins/dashboard/web/ , current table is manual ui elments, what's your propsaol ui kit or uielements", True),
            ("c09", "breakdown the '/Users/cr7sund/Documents/labs' disk space, why it cost more than 8gb", False),
            ("c10", "@packages/plugins/dashboard/ currently each page has a 'date ' as filter params, can we unified into one ui toggle. Also we want to optimzie current 'navigation ux'", True),
            ("c11", "why 'dist/assets/index-DO4QQnUX.js 588.76 kB' is so big in 'bun run build'", False),
            ("c12", "Analyze TrailTrading token costs", False),
        ],
    },
    {
        "query": "diagnosing token usage measurement problems",
        "candidates": [
            ("c01", "why there are so many 'no token usage metrics found for session', for 'coding-trajectory 019f12ed...'", True),
            ("c02", "commit", False),
            ("c03", "when we run 'ct session usage b1582044...' we found many 'prompt 0 uncached 0 cached 0 cache write 0 completion 0 reasoning 0'", True),
            ("c04", "why @.gitignore don't include node_module", False),
            ("c05", "why ct session stats 019f8358... reports prompt 2.2m but codex itself reports : Token usage: total=147,135", True),
            ("c06", "let us import motion for our @packages/plugins/dashboard/ web ui", False),
            ("c07", "breakdown codex memory and skills token cost, test case '019f3789-54e1-79b3-a642-70f55a4629cc'", False),  # near-miss: token cost analysis, not measurement defects
            ("c08", "merge this barnch to main. and do a cleanup", False),
            ("c09", "Explain Minimum Simulation Core", False),
            ("c10", "why 'dist/assets/index-DO4QQnUX.js 588.76 kB' is so big in 'bun run build'", False),
            ("c11", "remove the ci actions from this project", False),
            ("c12", "@packages/plugins/dashboard/ currently each page has a 'date ' as filter params, can we unified into one ui toggle", False),
        ],
    },
]


def ask(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.system_one(state=state, questions=questions)
    latency_ms = round((time.perf_counter() - started) * 1000)
    answers: dict[str, Any] = {}
    for key, answer in response.answers.items():
        entry = {"type": answer.type}
        for attr in ("choice", "probabilities", "confidence", "score", "legend", "noul"):
            value = getattr(answer, attr, None)
            if value is not None:
                entry[attr] = value
        answers[key] = entry
    return {
        "model": response.model,
        "latency_ms": latency_ms,
        "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
        "answers": answers,
    }


def experiment_intent() -> dict[str, Any]:
    records = []
    for text, expected in INTENT_CASES:
        result = ask(
            {"opening_user_message": text},
            {"intent": Choice(
                instructions="What kind of work is the user asking the coding agent to do? Judge the dominant intent.",
                criteria=INTENT_OPTIONS,
            )},
        )
        answer = result["answers"]["intent"]
        records.append({
            "input": text[:80], "expected": expected,
            "predicted": answer["choice"], "confidence": answer["confidence"],
            "correct": answer["choice"] == expected,
            "probabilities": answer["probabilities"],
            "latency_ms": result["latency_ms"], "usage": result["usage"],
        })
    return {"records": records}


def experiment_triage() -> dict[str, Any]:
    records = []
    for name, state, expected_band in TRIAGE_CASES:
        result = ask(state, {
            "severity": Score(
                instructions="How severe is this monitoring finding for the human supervising the coding agent?",
                criteria=TRIAGE_LEVELS,
            ),
            "interrupt": Noul(
                instructions="Should this finding interrupt the human right now?",
                criteria={"true": "Waiting risks wasted work, runaway cost, or exposure of sensitive data",
                          "false": "It can wait until the human next checks in"},
            ),
        })
        records.append({
            "case": name, "expected_band": expected_band,
            "severity": result["answers"]["severity"]["score"],
            "severity_confidence": result["answers"]["severity"]["confidence"],
            "interrupt_p": result["answers"]["interrupt"]["noul"],
            "latency_ms": result["latency_ms"], "usage": result["usage"],
        })
    return {"records": records}


def experiment_routing() -> dict[str, Any]:
    records = []
    for query, expected in ROUTE_CASES:
        result = ask(
            {"user_request": query},
            {"route": Choice(
                instructions="Which read method should serve this request about coding-agent session data?",
                criteria=ROUTE_OPTIONS,
            )},
        )
        answer = result["answers"]["route"]
        records.append({
            "query": query, "expected": expected,
            "predicted": answer["choice"], "confidence": answer["confidence"],
            "correct": answer["choice"] == expected,
            "runner_up": sorted(answer["probabilities"].items(), key=lambda kv: -kv[1])[1],
            "latency_ms": result["latency_ms"], "usage": result["usage"],
        })
    return {"records": records}


def experiment_rerank() -> dict[str, Any]:
    records = []
    for spec in RERANK_QUERIES:
        query = spec["query"]
        candidates = spec["candidates"]
        state = {"query": query, "candidates": [{"id": cid, "session_title": title} for cid, title, _ in candidates]}

        # Method A: one Choice over candidate IDs; probabilities give a ranking.
        choice_run = ask(state, {"pick": Choice(
            instructions="Which candidate session is most relevant to the query?",
            criteria={cid: None for cid, _, _ in candidates},
        )})
        choice_ranking = sorted(choice_run["answers"]["pick"]["probabilities"].items(), key=lambda kv: -kv[1])

        # Method B: per-candidate Noul relevance.
        noul_scores = []
        for cid, title, _ in candidates:
            run = ask({"query": query, "session_title": title}, {"relevant": Noul(
                instructions="Is this session relevant to the query?",
                criteria={"true": "The session is about the query topic",
                          "false": "Unrelated, or only superficially shares words"},
            )})
            noul_scores.append((cid, run["answers"]["relevant"]["noul"], run["latency_ms"], run["usage"]))
        noul_ranking = sorted(((cid, p) for cid, p, _, _ in noul_scores), key=lambda kv: -kv[1])

        truth = {cid: rel for cid, _, rel in candidates}
        records.append({
            "query": query,
            "ground_truth_relevant": [cid for cid, rel in truth.items() if rel],
            "baseline_order": [cid for cid, _, _ in candidates],
            "choice_ranking": choice_ranking,
            "choice_confidence": choice_run["answers"]["pick"]["confidence"],
            "choice_latency_ms": choice_run["latency_ms"], "choice_usage": choice_run["usage"],
            "noul_ranking": noul_ranking,
            "noul_calls": len(noul_scores),
            "noul_total_tokens": sum(u["input_tokens"] + u["output_tokens"] for _, _, _, u in noul_scores),
            "noul_total_latency_ms": sum(lat for _, _, lat, _ in noul_scores),
        })
    return {"records": records}


def main() -> None:
    results = {
        "experiments": {
            "session_intent": experiment_intent(),
            "finding_triage": experiment_triage(),
            "read_routing": experiment_routing(),
            "session_rerank": experiment_rerank(),
        }
    }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
