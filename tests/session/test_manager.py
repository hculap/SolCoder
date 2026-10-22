
import json
from pathlib import Path

import pytest
from datetime import UTC, datetime

from solcoder.session.manager import TRANSCRIPT_LIMIT, SessionContext, SessionLoadError, SessionManager


def test_create_session(tmp_path: Path) -> None:
    manager = SessionManager(root=tmp_path)
    context = manager.start(active_project="/tmp/project")

    assert isinstance(context, SessionContext)
    assert (tmp_path / context.metadata.session_id / "state.json").exists()
    assert context.metadata.active_project == "/tmp/project"


def test_resume_missing_session_raises(tmp_path: Path) -> None:
    manager = SessionManager(root=tmp_path)
    with pytest.raises(FileNotFoundError):
        manager.start(session_id="missing123")


def test_resume_existing_session(tmp_path: Path) -> None:
    manager = SessionManager(root=tmp_path)
    context = manager.start()
    manager.save(context)

    manager2 = SessionManager(root=tmp_path)
    resumed = manager2.start(session_id=context.metadata.session_id, active_project="/path/to/project")

    assert resumed.metadata.session_id == context.metadata.session_id
    assert resumed.metadata.active_project == "/path/to/project"


def test_rotation_keeps_recent(tmp_path: Path) -> None:
    manager = SessionManager(root=tmp_path)
    ids = []
    for _ in range(25):
        ctx = manager.start()
        manager.save(ctx)
        ids.append(ctx.metadata.session_id)

    remaining = {p.name for p in tmp_path.iterdir()}
    assert len(remaining) <= 20
    assert set(ids[-20:]).issuperset(remaining)


def test_rotation_tolerates_todo_only_session(tmp_path: Path) -> None:
    manager = SessionManager(root=tmp_path)
    manager.save_todo("todo-only", {"items": []})

    manager.start()

    assert manager.todo_exists("todo-only")


def test_save_trims_transcript(tmp_path: Path) -> None:
    manager = SessionManager(root=tmp_path)
    context = manager.start()
    context.transcript = [
        {
            "role": "user",
            "message": f"msg-{i}",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        for i in range(TRANSCRIPT_LIMIT + 5)
    ]

    manager.save(context)

    state_path = tmp_path / context.metadata.session_id / "state.json"
    persisted = json.loads(state_path.read_text())
    assert len(persisted["transcript"]) == TRANSCRIPT_LIMIT
    assert len(context.transcript) == TRANSCRIPT_LIMIT
    assert persisted["transcript"][0]["message"] == f"msg-{5}"
    assert "timestamp" in persisted["transcript"][0]


def test_corrupted_session_raises_load_error(tmp_path: Path) -> None:
    manager = SessionManager(root=tmp_path)
    context = manager.start()
    state_path = tmp_path / context.metadata.session_id / "state.json"
    state_path.write_text("this is not json")

    with pytest.raises(SessionLoadError):
        manager.start(session_id=context.metadata.session_id)
