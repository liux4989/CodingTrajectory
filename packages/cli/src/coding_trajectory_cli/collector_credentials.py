"""Compatibility exports for existing collector credential integrations."""

from coding_trajectory.control_plane.connections import (
    CollectorCredentialError,
    CollectorCredentialProfile,
    CollectorCredentials,
    configure_profile,
    load_profile,
    load_profile_credentials,
    profile_path,
    profile_summary,
)

__all__ = [
    "CollectorCredentialError",
    "CollectorCredentialProfile",
    "CollectorCredentials",
    "configure_profile",
    "load_profile",
    "load_profile_credentials",
    "profile_path",
    "profile_summary",
]
