from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from run_agent import AIAgent


def test_session_db_fork_keeps_child_connection_after_parent_close(tmp_path):
    db_path = tmp_path / "state.db"
    parent = SessionDB(db_path=db_path)
    child = None
    try:
        parent.create_session("parent-session", source="test")
        child = parent.fork()

        assert child.db_path == parent.db_path
        assert child.read_only == parent.read_only
        assert child._conn is not parent._conn
        assert child._lock is not parent._lock

        parent.close()
        child.create_session("child-session", source="test")
        assert child.get_session("child-session") is not None
    finally:
        if child is not None:
            child.close()
        else:
            parent.close()


def _make_agent(session_db, *, owns_session_db):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            owns_session_db=owns_session_db,
        )
    agent.client = MagicMock()
    agent.session_id = "session-under-test"
    return agent


def test_borrowed_session_db_is_not_closed_by_agent_close():
    session_db = MagicMock()
    agent = _make_agent(session_db, owns_session_db=False)

    agent.close()

    session_db.close.assert_not_called()
    session_db.end_session.assert_called_once_with("session-under-test", "agent_close")


def test_owned_session_db_is_ended_and_closed_once():
    session_db = MagicMock()
    agent = _make_agent(session_db, owns_session_db=True)

    agent.close()
    agent.close()

    session_db.end_session.assert_called_once_with("session-under-test", "agent_close")
    session_db.close.assert_called_once_with()
