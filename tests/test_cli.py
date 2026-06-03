import pytest

from coding_trajectory_cli.cli import _build_parser


def test_project_sessions_maps_to_project_session_listing() -> None:
    args = _build_parser().parse_args(["project", "sessions", "CodingTrajectory"])

    assert args._method == "project.sessions"
    assert args._params(args) == {"project_name": "CodingTrajectory"}


def test_session_overview_accepts_narrative_view() -> None:
    args = _build_parser().parse_args(
        ["session", "overview", "--view", "narrative", "session-1", "--turns", "3"]
    )

    assert args._method == "session.overview"
    assert args._params(args) == {
        "view": "narrative",
        "session_id": "session-1",
        "num_turns": 3,
    }


def test_session_usage_accepts_scope() -> None:
    args = _build_parser().parse_args(
        ["session", "usage", "--scope", "tool", "session-1", "--extra-billing"]
    )

    assert args._method == "session.usage"
    assert args._params(args) == {
        "scope": "tool",
        "extra_billing": True,
        "session_id": "session-1",
    }


def test_session_usage_turn_scope_accepts_turn_id() -> None:
    args = _build_parser().parse_args(
        ["session", "usage", "--scope", "turn", "session-1", "turn-1"]
    )

    assert args._method == "session.usage"
    assert args._params(args) == {
        "scope": "turn",
        "extra_billing": False,
        "session_id": "session-1",
        "turn_id": "turn-1",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["graph", "usage"],
        ["project", "graphs"],
        ["session", "narrative"],
        ["step", "detail", "step-1"],
        ["event", "detail", "event-1"],
    ],
)
def test_old_command_surface_is_removed(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(argv)
