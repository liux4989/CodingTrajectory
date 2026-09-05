# Tool use
- uv for python
- pydantic 

# Orb resources
- Keep build and test fan-out to 2 jobs on the default a1.medium Orb.
- For unusually heavy native, frontend, or multi-package builds, explicitly request a larger Orb when creating the thread. Setup and resume scripts must not select or resize the Orb.

# Atomic rule
- After completing a task that changes files, the agent MUST create a git commit with a descriptive message summarizing the changes.
- Do not create empty or analysis-only commits just to satisfy the atomic rule.
- When continuing work in the same session or on a continuous task, prefer amending the latest relevant commit instead of creating a new commit.
- Do not write unit tests. 

# Metrics quality gate
- Before committing changes to metric-sensitive paths, run `scripts/check-metrics-quality-gate.sh`.
- Run the full baseline workflow directly with `uv run python scripts/validate-metrics-baselines.py`.
- Never update expected metric values from current command output alone; reconstruct and document intentional changes from the committed source evidence first.
