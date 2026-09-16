# CodingTrajectory Loop

Loop is a local-first reading and improvement product over frozen Core.
This release serves developers investigating their own coding-agent logs.
The primary tasks are Explore → Investigation → exact canonical evidence
(Analytics) and Strategies → Watch → dry-run/refresh → Evaluation → Finding
triage (Monitor) with the deterministic turn-token-budget strategy.
Core owns all resource and metric semantics. Loop saves references, view state,
watch configuration, evaluation results, and finding triage — never copied
transcript or event bodies. No publication, remote writes, LLM strategies, or
Improve execution ships in this slice.

Monitor's first strategy is deterministic and declares its permission boundary
in-product: it reads local Core usage measurements and session inventory only,
with transcript access, external egress, notifications, and enforcement all
"Not requested". Dry-run is a labeled historical preview; refresh runs only on
explicit human request after enable. Unavailable evidence is recorded honestly
and never treated as zero.

Assumptions from the explicit implementation brief: desktop is the primary reading
surface, mobile must remain usable, and familiar React/shadcn controls take priority
over expressive visual effects. Local evidence may be incomplete and must say so.
