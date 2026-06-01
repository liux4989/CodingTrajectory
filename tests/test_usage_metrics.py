from __future__ import annotations

from coding_trajectory.ingestion.vendor_mechanisms.usage_metrics import (
    normalize_amp_usage,
    normalize_claude_usage,
    normalize_gemini_usage,
)


def test_other_agent_usage_normalizers_emit_common_metrics_shape() -> None:
    claude = normalize_claude_usage(
        model="claude-sonnet-4-6",
        usage={
            "input_tokens": 10,
            "output_tokens": 40,
        },
    )
    amp = normalize_amp_usage(
        model="amp-model",
        usage={"inputTokens": 50, "outputTokens": 60},
    )
    gemini = normalize_gemini_usage(
        model="gemini-model",
        tokens={"inputTokens": 70, "outputTokens": 80},
    )

    assert claude["metrics"]["model"] == "claude-sonnet-4-6"
    assert claude["metrics"]["usage"]["output_tokens"] == 40
    assert amp["metrics"]["model"] == "amp-model"
    assert amp["metrics"]["usage"]["input_tokens"] == 50
    assert gemini["metrics"]["model"] == "gemini-model"
    assert gemini["metrics"]["usage"]["outputTokens"] == 80
