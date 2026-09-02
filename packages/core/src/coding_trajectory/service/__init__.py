"""Service layer implementing the session-api.json contract.

Split into ``serializers`` (output shaping and id parsing), ``store``
(discovery-backed DocumentStore construction and the index cache), and
``handlers`` (dispatch plus the per-method handlers). All previously public
names are re-exported here so ``coding_trajectory.service.X`` keeps working.
"""

from coding_trajectory.service.handlers import (
    SERVICE_HANDLERS,
    ServiceContext,
    ServiceHandler,
    dispatch,
)
from coding_trajectory.service.serializers import (
    serialize_event_detail,
    serialize_llm_detail,
    serialize_session_graph_detail,
    serialize_text_detail,
    serialize_tool_call_detail,
    serialize_usage_detail,
)
from coding_trajectory.service.store import (
    TEMPORARY_PROJECT_KEY,
    IndexCache,
    project_list_metadata,
    resolve_collection,
    resolve_resource,
    resolve_store,
)

__all__ = [
    "IndexCache",
    "SERVICE_HANDLERS",
    "ServiceContext",
    "ServiceHandler",
    "TEMPORARY_PROJECT_KEY",
    "dispatch",
    "project_list_metadata",
    "resolve_collection",
    "resolve_resource",
    "resolve_store",
    "serialize_event_detail",
    "serialize_llm_detail",
    "serialize_session_graph_detail",
    "serialize_text_detail",
    "serialize_tool_call_detail",
    "serialize_usage_detail",
]
