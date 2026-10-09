"""Partition adapter-owned source topology without constructing transcripts."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from coding_trajectory.ingestion.adapters.base import SourceTopology
from coding_trajectory.ingestion.common import normalize_project_key
from coding_trajectory.ingestion.models import Vendor


@dataclass(frozen=True)
class TopologyRun:
    root_session_id: UUID
    lineage_root_session_id: UUID
    session_ids: tuple[UUID, ...]
    source_paths: tuple[str, ...]
    ancestor_paths: tuple[str, ...]
    header: SourceTopology
    modified: datetime
    vendors: tuple[Vendor, ...]


def orchestration_topology(sources: dict[str, SourceTopology]) -> list[TopologyRun]:
    """Use explicit parents and unique Amp claims; missing parents stay roots.

    Ordinary forks start a new run; spawn/sidechain relationships join a run.
    Project partitioning matches canonical discovery's graph assembly.
    """
    projects: dict[str, dict[str, SourceTopology]] = defaultdict(dict)
    for path, header in sources.items():
        projects[normalize_project_key(header.project or header.session_id.hex)][
            path
        ] = header
    runs = []
    for rows in projects.values():
        paths: dict[UUID, list[str]] = defaultdict(list)
        headers: dict[UUID, SourceTopology] = {}
        for path, header in sorted(rows.items()):
            paths[header.session_id].append(path)
            previous = headers.get(header.session_id)
            if previous is None or header.modified >= previous.modified:
                headers[header.session_id] = header
        parents = {
            sid: (header.parent_session_id, header.parent_kind or "spawn")
            for sid, header in headers.items()
            if header.parent_session_id in headers and header.parent_session_id != sid
        }
        claims: dict[UUID, set[UUID]] = defaultdict(set)
        for header in rows.values():
            if header.vendor == Vendor.AMP:
                for child in header.children:
                    claims[child].add(header.session_id)
        for child, owners in sorted(claims.items(), key=lambda item: str(item[0])):
            if (
                len(owners) != 1
                or child not in headers
                or headers[child].vendor != Vendor.AMP
            ):
                continue
            parent = next(iter(owners))
            if parent == child or (child in parents and parents[child][0] != parent):
                continue
            cursor, seen = parent, {child}
            while cursor in parents and cursor not in seen:
                seen.add(cursor)
                cursor = parents[cursor][0]
            if cursor not in seen:
                parents[child] = (parent, "spawn")

        def root_for(sid: UUID, *, lineage: bool, relations=parents) -> UUID:
            seen = set()
            while sid in relations and (lineage or relations[sid][1] != "fork"):
                if sid in seen:
                    return min(seen, key=str)
                seen.add(sid)
                sid = relations[sid][0]
            return sid

        members: dict[UUID, set[UUID]] = defaultdict(set)
        for sid in headers:
            members[root_for(sid, lineage=False)].add(sid)
        for root, ids in members.items():
            ancestors = set()
            cursor = root
            while cursor in parents and parents[cursor][0] not in ancestors:
                cursor = parents[cursor][0]
                ancestors.add(cursor)
            ancestors -= ids
            runs.append(
                TopologyRun(
                    root_session_id=root,
                    lineage_root_session_id=root_for(root, lineage=True),
                    session_ids=tuple(sorted(ids, key=str)),
                    source_paths=tuple(
                        sorted(path for sid in ids for path in paths[sid])
                    ),
                    ancestor_paths=tuple(
                        sorted(path for sid in ancestors for path in paths[sid])
                    ),
                    header=headers[root],
                    modified=max(
                        rows[path].modified for sid in ids for path in paths[sid]
                    ),
                    vendors=tuple(
                        sorted(
                            {headers[sid].vendor for sid in ids}, key=lambda v: v.value
                        )
                    ),
                )
            )
    return sorted(
        runs, key=lambda run: (run.header.project or "", str(run.root_session_id))
    )
