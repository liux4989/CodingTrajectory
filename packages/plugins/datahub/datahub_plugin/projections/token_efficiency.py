"""Compatibility facade for the decomposed projection."""

# ruff: noqa: F401
from __future__ import annotations

from datahub_plugin.projections.token_efficiency_assembly import *
from datahub_plugin.projections.token_efficiency_assembly import (
    _batch_methods,
    _compact_project_key,
    _project_display_name,
    _project_options,
    _resolve_project_option,
    _usage_units,
    build_project_projection,
)
from datahub_plugin.projections.token_efficiency_period_analysis import *
from datahub_plugin.projections.token_efficiency_period_analysis import (
    _attribution,
    _comparison,
    _distribution,
    _hotspot_rows,
    _in_period,
    _matches_pattern,
    _optional_int,
    _optional_text,
    _outlier_rows,
    _pattern_metrics,
    _pattern_rows,
    _pct_change,
    _period_summary,
    _period_tuple,
    _prompt_tokens,
    _required_discovery_days,
    _trend_periods,
)
from datahub_plugin.projections.token_efficiency_tool_analysis import *
from datahub_plugin.projections.token_efficiency_tool_analysis import (
    _classify_tool,
    _extract_resources,
    _latest_periods,
    _looks_like_file_operand,
    _search_command_details,
    _tool_records,
)
