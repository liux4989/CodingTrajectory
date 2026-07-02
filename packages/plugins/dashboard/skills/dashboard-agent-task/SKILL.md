---
name: dashboard-agent-task
description: Run a general dashboard agent task from a goal and plain-text context, returning plain text.
---

# Dashboard Agent Task

You run a general dashboard agent task.

Input contains:

- `task_goal`: what the dashboard feature wants accomplished;
- `task_context`: the plain-text context supplied by that feature.

Use the supplied context as the evidence boundary. If the goal asks for fixes, include likely causes, concrete next actions, and any missing evidence needed to proceed. Return plain text only.
