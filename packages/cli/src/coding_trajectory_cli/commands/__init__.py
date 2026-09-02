"""Command group registrars for the ct CLI."""

from coding_trajectory_cli.commands.api import register as register_api
from coding_trajectory_cli.commands.collector import register as register_collector
from coding_trajectory_cli.commands.doctor import register as register_doctor
from coding_trajectory_cli.commands.plugin import dispatch_plugin_argv, register as register_plugin
from coding_trajectory_cli.commands.project import register as register_project
from coding_trajectory_cli.commands.session import register as register_session

REGISTRARS = [
    register_project,
    register_session,
    register_api,
    register_collector,
    register_doctor,
    register_plugin,
]

__all__ = ["REGISTRARS", "dispatch_plugin_argv"]
