"""Per-agent session registry. Each agent is identified by its bearer token."""
from __future__ import annotations

import threading

from .session import Session


class Agent:
    """All sessions belonging to one bearer-token holder."""

    def __init__(self, token: str, buffer_capacity: int) -> None:
        self.token = token
        self.buffer_capacity = buffer_capacity
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(
        self,
        cmd: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> Session:
        sess = Session(
            cmd,
            self.buffer_capacity,
            cwd=cwd,
            env=env,
            rows=rows,
            cols=cols,
        )
        with self._lock:
            self._sessions[sess.id] = sess
        return sess

    def get(self, sid: str) -> Session:
        with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            raise KeyError(f"unknown session: {sid}")
        return sess

    def remove(self, sid: str) -> Session | None:
        with self._lock:
            return self._sessions.pop(sid, None)

    def list(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())


class AgentRegistry:
    """Map bearer tokens → Agent. Lazily creates an Agent on first use."""

    def __init__(self, buffer_capacity: int) -> None:
        self.buffer_capacity = buffer_capacity
        self._agents: dict[str, Agent] = {}
        self._lock = threading.Lock()

    def get_or_create(self, token: str) -> Agent:
        with self._lock:
            agent = self._agents.get(token)
            if agent is None:
                agent = Agent(token, self.buffer_capacity)
                self._agents[token] = agent
            return agent
