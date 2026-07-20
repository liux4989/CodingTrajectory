"""Tool-specific output optimization profiles for analysis views."""

from __future__ import annotations

from pydantic import BaseModel

from coding_trajectory.analysis.tool_summary_shared import (
    EDIT_FILE,
    LIST_FILES,
    READ_FILE,
    RUN_COMMAND,
    SEARCH_TEXT,
    SESSION_HANDOFF,
    SUBAGENT_TASK,
    TODO_LIST,
    WEB_FETCH,
    WEB_SEARCH,
    WRITE_FILE,
)


class ToolOptimizationProfile(BaseModel):
    concept: str
    profile: str | None = None
    group_repeated: bool = False
    detail_key: str = "target"
    detail_list_key: str = "targets"
    detail_counts_key: str | None = None
    dedupe_repeated_details: bool = False


TOOL_OPTIMIZATION_PROFILES: tuple[ToolOptimizationProfile, ...] = (
    ToolOptimizationProfile(
        concept=READ_FILE,
        group_repeated=True,
        detail_key="path",
        detail_list_key="paths",
        detail_counts_key="path_counts",
        dedupe_repeated_details=True,
    ),
    ToolOptimizationProfile(
        concept=READ_FILE,
        profile="shell:read",
        group_repeated=True,
        detail_key="path",
        detail_list_key="paths",
        detail_counts_key="path_counts",
        dedupe_repeated_details=True,
    ),
    ToolOptimizationProfile(
        concept=SEARCH_TEXT,
        group_repeated=True,
        detail_key="query",
        detail_list_key="queries",
    ),
    ToolOptimizationProfile(
        concept=SEARCH_TEXT,
        profile="shell:search",
        group_repeated=True,
        detail_key="query",
        detail_list_key="queries",
    ),
    ToolOptimizationProfile(
        concept=LIST_FILES,
        group_repeated=True,
        detail_key="path",
        detail_list_key="paths",
    ),
    ToolOptimizationProfile(
        concept=LIST_FILES,
        profile="shell:list",
        group_repeated=True,
        detail_key="path",
        detail_list_key="paths",
    ),
    ToolOptimizationProfile(
        concept=WEB_FETCH,
        group_repeated=True,
        detail_key="url",
        detail_list_key="urls",
    ),
    ToolOptimizationProfile(
        concept=WEB_SEARCH,
        group_repeated=True,
        detail_key="query",
        detail_list_key="queries",
    ),
    ToolOptimizationProfile(concept=EDIT_FILE, detail_key="path"),
    ToolOptimizationProfile(concept=WRITE_FILE, detail_key="path"),
    ToolOptimizationProfile(concept=RUN_COMMAND, detail_key="cmd"),
    ToolOptimizationProfile(
        concept=RUN_COMMAND, profile="shell:command", detail_key="cmd"
    ),
    ToolOptimizationProfile(concept=TODO_LIST, detail_key="items"),
    ToolOptimizationProfile(concept=SUBAGENT_TASK, detail_key="task"),
    ToolOptimizationProfile(concept=SESSION_HANDOFF, detail_key="session"),
)

_PROFILE_BY_CONCEPT = {
    profile.concept: profile
    for profile in TOOL_OPTIMIZATION_PROFILES
    if profile.profile is None
}
_PROFILE_BY_KEY = {
    (profile.concept, profile.profile): profile
    for profile in TOOL_OPTIMIZATION_PROFILES
    if profile.profile is not None
}
_DEFAULT_PROFILE = ToolOptimizationProfile(concept="*", detail_key="target")


def tool_optimization_profile(
    concept: str | None, profile: str | None = None
) -> ToolOptimizationProfile:
    if not concept:
        return _DEFAULT_PROFILE
    if profile is not None:
        matched = _PROFILE_BY_KEY.get((concept, profile))
        if matched is not None:
            return matched
    return _PROFILE_BY_CONCEPT.get(concept, _DEFAULT_PROFILE)
