
"""Session lifecycle utilities."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, ValidationError

def _default_root() -> Path:
    return Path(os.environ.get("SOLCODER_HOME", Path.home() / ".solcoder")) / "sessions"

DEFAULT_ROOT = _default_root()
MAX_SESSIONS = 20
# Upper bound enforced when persisting session transcripts. Set high so
# higher-level history management (configurable in CLI) can control
# summarisation thresholds without being truncated here.
TRANSCRIPT_LIMIT = 1000


class SessionLoadError(RuntimeError):
    """Raised when a session directory exists but cannot be deserialized."""


class SessionMetadata(BaseModel):
    session_id: str
    created_at: datetime
    updated_at: datetime
    active_project: str | None = None
    wallet_status: str | None = None
    spend_amount: float = 0.0
    wallet_balance: float | None = None
    last_airdrop_at: datetime | None = None
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_last_input_tokens: int = 0
    llm_last_output_tokens: int = 0
    compression_cooldown: int = 0


@dataclass
class SessionContext:
    metadata: SessionMetadata
    transcript: list[Dict[str, Any]]


class SessionManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._base58_pattern = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

    def start(self, session_id: str | None = None, *, active_project: str | None = None) -> SessionContext:
        if session_id:
            return self._load_existing(session_id, active_project=active_project)
        return self._create_new(active_project=active_project)

    def save(self, context: SessionContext) -> None:
        context.transcript = context.transcript[-TRANSCRIPT_LIMIT:]
        context.metadata.updated_at = datetime.now(UTC)
        session_dir = self.root / context.metadata.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        state_path = session_dir / "state.json"
        payload = json.dumps(
            {
                "metadata": context.metadata.model_dump(),
                "transcript": context.transcript,
            },
            default=str,
            indent=2,
        )
        tmp_path = state_path.with_suffix(".json.tmp")
        tmp_path.write_text(payload)
        tmp_path.replace(state_path)
        self._enforce_rotation()

    # ------------------------------------------------------------------
    def save_todo(self, session_id: str, payload: dict[str, Any]) -> None:
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        todo_path = self._todo_path(session_id)
        tmp_path = todo_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.replace(todo_path)

    def load_todo(self, session_id: str) -> dict[str, Any] | None:
        todo_path = self._todo_path(session_id)
        if not todo_path.exists():
            return None
        try:
            return json.loads(todo_path.read_text())
        except json.JSONDecodeError:
            return None

    def todo_exists(self, session_id: str) -> bool:
        return self._todo_path(session_id).exists()

    def _todo_path(self, session_id: str) -> Path:
        return self.root / session_id / "todo.json"

    # ------------------------------------------------------------------
    def _create_new(self, *, active_project: str | None) -> SessionContext:
        session_id = uuid.uuid4().hex[:12]
        metadata = SessionMetadata(
            session_id=session_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            active_project=active_project,
        )
        context = SessionContext(metadata=metadata, transcript=[])
        self.save(context)
        return context

    def _load_existing(self, session_id: str, *, active_project: str | None) -> SessionContext:
        session_dir = self.root / session_id
        state_path = session_dir / "state.json"
        if not state_path.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found")
        try:
            data = json.loads(state_path.read_text())
        except json.JSONDecodeError as exc:
            raise SessionLoadError(f"Session '{session_id}' state is corrupted") from exc

        try:
            metadata = SessionMetadata(**data["metadata"])
        except (KeyError, ValidationError) as exc:
            raise SessionLoadError(f"Session '{session_id}' metadata is invalid") from exc

        transcript_raw = data.get("transcript", [])
        if not isinstance(transcript_raw, list):
            raise SessionLoadError(f"Session '{session_id}' transcript is invalid")
        transcript: list[Dict[str, Any]] = []
        for entry in transcript_raw[-TRANSCRIPT_LIMIT:]:
            normalized = self._normalize_transcript_entry(entry)
            if normalized:
                transcript.append(normalized)
        if active_project:
            metadata.active_project = active_project
        return SessionContext(metadata=metadata, transcript=transcript)

    def _enforce_rotation(self) -> None:
        # A session directory's mtime can be too coarse when many sessions are
        # created in one burst. The atomically replaced state file is the
        # authoritative write and carries nanosecond-resolution recency. A
        # todo-only session has no state file yet, so fall back to its directory.
        def modified_at_ns(session_dir: Path) -> int:
            state_path = session_dir / "state.json"
            return (state_path if state_path.exists() else session_dir).stat().st_mtime_ns

        sessions = sorted(
            (path for path in self.root.iterdir() if path.is_dir()),
            key=modified_at_ns,
            reverse=True,
        )
        for extra in sessions[MAX_SESSIONS:]:
            for child in extra.iterdir():
                child.unlink()
            extra.rmdir()

    # ------------------------------------------------------------------
    def export_session(self, session_id: str, *, redact: bool = True) -> dict[str, Any]:
        context = self._load_existing(session_id, active_project=None)
        metadata = context.metadata.model_dump(mode="json")
        transcript: List[Dict[str, Any]] = []
        for entry in context.transcript:
            record: Dict[str, Any] = {
                "role": entry["role"],
                "message": entry["message"],
                "timestamp": entry.get("timestamp"),
            }
            tool_calls = entry.get("tool_calls")
            if tool_calls:
                record["tool_calls"] = tool_calls
            transcript.append(record)
        if redact:
            metadata = self._redact_mapping(metadata)
            transcript = [self._redact_entry(item) for item in transcript]
        return {
            "metadata": metadata,
            "transcript": transcript,
        }

    def _redact_mapping(self, mapping: Dict[str, Any]) -> Dict[str, Any]:
        redacted: Dict[str, Any] = {}
        for key, value in mapping.items():
            if isinstance(value, str):
                redacted[key] = self._redact_text(value)
            else:
                redacted[key] = value
        return redacted

    def _normalize_transcript_entry(self, entry: Any) -> Dict[str, Any] | None:
        if isinstance(entry, dict):
            role = entry.get("role")
            message = entry.get("message")
            if not isinstance(role, str) or not isinstance(message, str):
                return None
            timestamp = entry.get("timestamp")
            if isinstance(timestamp, str):
                ts = timestamp
            else:
                ts = datetime.now(UTC).isoformat()
            tool_calls_raw = entry.get("tool_calls")
            tool_calls: List[Dict[str, Any]] | None = None
            if isinstance(tool_calls_raw, list):
                cleaned: List[Dict[str, Any]] = []
                for item in tool_calls_raw:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    status = item.get("status")
                    summary = item.get("summary")
                    call_type = item.get("type", "tool")
                    cleaned_item: Dict[str, Any] = {
                        "type": str(call_type),
                        "name": str(name) if name is not None else "",
                        "status": str(status) if status is not None else "unknown",
                    }
                    if isinstance(summary, str) and summary:
                        cleaned_item["summary"] = summary
                    cleaned.append(cleaned_item)
                if cleaned:
                    tool_calls = cleaned
            record: Dict[str, Any] = {"role": role, "message": message, "timestamp": ts}
            if tool_calls:
                record["tool_calls"] = tool_calls
            return record
        if (
            isinstance(entry, (list, tuple))
            and len(entry) == 2
            and all(isinstance(item, str) for item in entry)
        ):
            return {
                "role": entry[0],
                "message": entry[1],
                "timestamp": datetime.now(UTC).isoformat(),
            }
        return None

    def _redact_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        redacted: Dict[str, Any] = {
            "role": entry.get("role"),
            "message": self._redact_text(entry.get("message", "")),
            "timestamp": entry.get("timestamp"),
        }
        tool_calls_raw = entry.get("tool_calls")
        if isinstance(tool_calls_raw, list):
            cleaned: List[Dict[str, Any]] = []
            for item in tool_calls_raw:
                if not isinstance(item, dict):
                    continue
                redacted_item = item.copy()
                summary = redacted_item.get("summary")
                if isinstance(summary, str):
                    redacted_item["summary"] = self._redact_text(summary)
                cleaned.append(redacted_item)
            if cleaned:
                redacted["tool_calls"] = cleaned
        return redacted

    def _redact_text(self, text: str) -> str:
        return self._base58_pattern.sub(self._mask_base58, text)

    @staticmethod
    def _mask_base58(match: re.Match[str]) -> str:
        value = match.group(0)
        if len(value) <= 8:
            return value
        return f"{value[:4]}…{value[-4:]}"


__all__ = ["SessionManager", "SessionContext", "SessionMetadata", "SessionLoadError", "DEFAULT_ROOT"]
