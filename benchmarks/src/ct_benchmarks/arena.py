"""Human arena: present two variant outputs for side-by-side comparison."""
from __future__ import annotations
import random
from .models import AgentOutput, ArenaComparison, TaskType


def build_arena_pair(
    task_type: TaskType,
    outputs: list[AgentOutput],
) -> ArenaComparison | None:
    """Pick two outputs with different tool variants for the same task type, randomize order."""
    by_variant: dict[str, list[AgentOutput]] = {}
    for out in outputs:
        if out.task_type == task_type:
            by_variant.setdefault(out.tool_variant.value, []).append(out)

    variants = list(by_variant.keys())
    if len(variants) < 2:
        return None

    a = random.choice(by_variant[variants[0]])
    b = random.choice(by_variant[variants[1]])

    # Randomize which is shown as A vs B to reduce position bias
    if random.random() > 0.5:
        a, b = b, a

    return ArenaComparison(
        case_id=f"{a.case_id}_vs_{b.case_id}",
        task_type=task_type,
        output_a=a,
        output_b=b,
    )


def render_arena_text(comparison: ArenaComparison) -> str:
    """Render a comparison as plain text for human evaluation."""
    lines = [
        f"=== Arena Comparison: {comparison.task_type.value} ===",
        "",
        "--- Output A ---",
        comparison.output_a.output,
        "",
        "--- Output B ---",
        comparison.output_b.output,
        "",
        "Which output is better? Enter: a / b / tie",
    ]
    return "\n".join(lines)
