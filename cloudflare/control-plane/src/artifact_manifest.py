"""Compact stored artifact-manifest representation."""

from __future__ import annotations

from typing import Any

from shared import require_that, stable

Json = dict[str, Any]


def compact_graph(graph: Json) -> Json:
    objects = graph["api_objects"]
    descriptors = graph["api_methods"]
    metadata = {
        key: value
        for key, value in graph.items()
        if key not in {"api_objects", "api_methods"}
    }
    positions = {reference["sha256"]: index for index, reference in enumerate(objects)}
    methods: list[list[Any]] = []
    scopes: list[str] = []
    entries: list[list[Any]] = []
    method_positions: dict[str, int] = {}
    scope_positions: dict[str, int] = {}
    for descriptor in descriptors:
        method = [descriptor["method"], descriptor["method_version"]]
        key = stable(method)
        if key not in method_positions:
            method_positions[key] = len(methods)
            methods.append(method)
        scope = descriptor["scope"]
        if scope not in scope_positions:
            scope_positions[scope] = len(scopes)
            scopes.append(scope)
        index = (
            positions.get(descriptor["index"]["sha256"])
            if descriptor.get("index")
            else None
        )
        if descriptor.get("index"):
            require_that(
                index is not None
                and objects[index]["bytes"] == descriptor["index"]["bytes"],
                "invalid_prepared_reference",
            )
        entries.append(
            [
                method_positions[key],
                scope_positions[scope],
                descriptor.get("turn_id"),
                index,
            ]
        )
    return {
        **metadata,
        "api": {
            "objects": [
                [reference["sha256"], reference["bytes"]] for reference in objects
            ],
            "methods": methods,
            "scopes": scopes,
            "entries": entries,
        },
    }


def expand_graph(graph: Json) -> Json:
    api = graph["api"]
    metadata = {key: value for key, value in graph.items() if key != "api"}
    objects = [
        {"kind": "api", "sha256": sha256, "bytes": byte_count}
        for sha256, byte_count in api["objects"]
    ]
    require_that(
        len({reference["sha256"] for reference in objects}) == len(objects),
        "invalid_prepared_reference",
    )

    def at(values: list[Any], position: Any) -> Any:
        require_that(
            isinstance(position, int)
            and not isinstance(position, bool)
            and 0 <= position < len(values),
            "invalid_prepared_reference",
        )
        return values[position]

    methods = []
    for method_position, scope_position, turn_id, index in api["entries"]:
        name, version = at(api["methods"], method_position)
        methods.append(
            {
                "method": name,
                "method_version": version,
                "scope": at(api["scopes"], scope_position),
                "turn_id": turn_id,
                "index": None if index is None else at(objects, index),
                "error": "remote_result_too_large" if index is None else None,
            }
        )
    return {**metadata, "api_objects": objects, "api_methods": methods}


def expand_manifest(manifest: Json) -> Json:
    if manifest.get("schema_version") != "ct.artifact-manifest.v3":
        return manifest
    return {
        **manifest,
        "schema_version": "ct.artifact-manifest.v2",
        "graphs": [expand_graph(graph) for graph in manifest["graphs"]],
    }
