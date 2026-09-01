"""Compatibility facade for the decomposed projection."""

# ruff: noqa: F401
from __future__ import annotations

from datahub_plugin.projections.analytical_read_models_clients import *
from datahub_plugin.projections.analytical_read_models_clients import (
    CanonicalFactsApiClient,
    DocumentStoreApiClient,
    build_canonical_fact_rows,
)
from datahub_plugin.projections.analytical_read_models_facts import *
from datahub_plugin.projections.analytical_read_models_facts import (
    _build_canonical_fact_rows_from_store,
    _economics_fact_rows,
    _graph_fact_rows,
    build_canonical_root_fact_rows,
    build_model_usage_rows,
    build_token_efficiency_project_rows,
    canonical_fact_entity_kinds,
)
from datahub_plugin.projections.analytical_read_models_reconstruction import *
from datahub_plugin.projections.analytical_read_models_reconstruction import (
    _adapter_execute,
    _detail_mutations,
    _error_item,
    _fact_mutation,
    _mutation,
    _project_items_from_session_facts,
    _project_list_from_store,
    _reconstruct,
    _row_payload,
    _scope_key,
    _session_entrypoint,
    _telemetry_payload_id,
    _token_row_id,
    _value,
    _without,
    analytical_scope_key,
    page_metadata,
    reconstruct_model_usage,
    reconstruct_token_efficiency_project,
)
