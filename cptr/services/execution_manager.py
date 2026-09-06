"""In-process execution-plane state boundary for command sessions.

The API layer deliberately talks to this registry instead of owning ad-hoc
lifecycle policy. It remains in-process today because CPTR command/process and
browser ownership are process-local; this boundary is the seam for a future IPC
execution service without prematurely enabling unsafe multi-worker serving.
"""

from __future__ import annotations

import asyncio
import errno
import os
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from cptr.env import COMMAND_SESSION_MAX_RETAINED, COMMAND_SESSION_TTL_SECONDS


class CommandSessionRegistry:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self._launch_reservations: dict[object, dict[str, Any]] = {}
        self.total_created = 0
        self.total_reaped = 0

    def register(
        self,
        session_id: str,
        session: dict[str, Any],
        *,
        reservation_token: object | None = None,
    ) -> None:
        if reservation_token is not None:
            missing = object()
            reservation = self._launch_reservations.get(reservation_token, missing)
            if reservation is missing:
                raise RuntimeError("command launch reservation is not active")
            if reservation.get("user_id") != session.get("user_id"):
                raise RuntimeError("command launch reservation owner mismatch")
            del self._launch_reservations[reservation_token]
        self.sessions[session_id] = session
        self.total_created += 1

    def get(self, session_id: str) -> dict[str, Any] | None:
        session = self.sessions.get(session_id)
        if session is not None:
            self._reconcile_session(session)
        return session

    def remove(self, session_id: str) -> dict[str, Any] | None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            self.total_reaped += 1
        return session

    def values(self) -> Iterable[dict[str, Any]]:
        self.reconcile()
        return self.sessions.values()

    @staticmethod
    def _os_process_exit_state(session: dict[str, Any]) -> bool | None:
        """Ask POSIX for authoritative child/process-group liveness without reaping it.

        asyncio process handles can retain ``returncode=None`` when their waiter was
        cancelled during request/lifespan teardown.  Capacity accounting must not
        treat that stale Python object as stronger evidence than the kernel.  Return
        True only when exit is proven, False when the owned child/group is live, and
        None when the platform cannot decide safely.
        """
        if os.name != "posix":
            return None
        proc = session.get("proc")
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return None

        waitid = getattr(os, "waitid", None)
        p_pid = getattr(os, "P_PID", None)
        wexited = int(getattr(os, "WEXITED", 0) or 0)
        wnohang = int(getattr(os, "WNOHANG", 0) or 0)
        wnowait = int(getattr(os, "WNOWAIT", 0) or 0)
        if callable(waitid) and p_pid is not None and wexited and wnohang and wnowait:
            try:
                info = waitid(p_pid, pid, wexited | wnohang | wnowait)
            except ProcessLookupError:
                return True
            except ChildProcessError:
                # The child may already have been reaped by asyncio; verify the
                # process group below before declaring the command complete.
                pass
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return True
            else:
                if info is not None and int(getattr(info, "si_pid", 0) or 0) == pid:
                    return True
                return False

        killpg = getattr(os, "killpg", None)
        if not callable(killpg):
            return None
        try:
            # Every CPTR command is launched in its own process group/session.
            # Signal 0 performs an existence/permission check without signalling.
            killpg(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return True
            if exc.errno == errno.EPERM:
                return False
            return None
        return False

    @staticmethod
    def _process_exit_state(session: dict[str, Any]) -> tuple[bool, int | None]:
        """Return whether the owned child has exited without trusting cached `done`."""
        wait_task = session.get("process_wait_task")
        task_done = getattr(wait_task, "done", None)
        task_result = getattr(wait_task, "result", None)
        if callable(task_done) and callable(task_result):
            try:
                if task_done():
                    result = task_result()
                    try:
                        return True, int(result) if result is not None else None
                    except (TypeError, ValueError):
                        return True, None
            except BaseException:
                # A cancelled/failed watcher is not proof of process exit; fall
                # back to the concrete process handle below.
                pass

        proc = session.get("proc")
        if proc is None:
            return False, None

        returncode = getattr(proc, "returncode", None)
        if returncode is None:
            poll = getattr(proc, "poll", None)
            if callable(poll):
                try:
                    returncode = poll()
                except (ChildProcessError, OSError, ProcessLookupError):
                    return False, None
                except Exception:
                    return False, None
        if returncode is None:
            os_exited = CommandSessionRegistry._os_process_exit_state(session)
            if os_exited is True:
                return True, None
            return False, None
        try:
            return True, int(returncode)
        except (TypeError, ValueError):
            return True, None

    @staticmethod
    def _capture_finished(session: dict[str, Any]) -> bool:
        task = session.get("log_task")
        if task is None:
            return True
        done = getattr(task, "done", None)
        if not callable(done):
            return False
        try:
            return bool(done())
        except Exception:
            return False

    def _reconcile_session(self, session: dict[str, Any], *, now: float | None = None) -> bool:
        """Repair stale completion metadata once the child and capture task are quiescent."""
        if session.get("done"):
            return False
        exited, exit_code = self._process_exit_state(session)
        if not exited or not self._capture_finished(session):
            return False
        session["done"] = True
        session.setdefault("completed_at", time.time() if now is None else now)
        if session.get("exit_code") is None and exit_code is not None:
            session["exit_code"] = exit_code
        return True

    def reconcile(self, *, now: float | None = None) -> int:
        """Self-heal stale registry flags after child-process completion."""
        repaired = 0
        for session in self.sessions.values():
            if self._reconcile_session(session, now=now):
                repaired += 1
        return repaired

    def is_active(self, session: dict[str, Any]) -> bool:
        """A command consumes a concurrency slot only while its child is alive."""
        if session.get("done"):
            return False
        exited, _ = self._process_exit_state(session)
        return not exited

    def active_count(self, user_id: str | None = None) -> int:
        self.reconcile()
        return sum(
            1
            for session in self.sessions.values()
            if self.is_active(session) and (user_id is None or session.get("user_id") == user_id)
        )

    @staticmethod
    def _task_finished(task: Any) -> bool:
        if task is None:
            return False
        done = getattr(task, "done", None)
        if not callable(done):
            return False
        try:
            return bool(done())
        except Exception:
            return False

    def reconcile_launch_reservations(self) -> int:
        """Release orphan launch slots without ever ignoring a live owned child.

        A reservation normally lives only inside ``reserve_launch``.  If request
        teardown prevents that context from unwinding, retain the slot while its
        owner task is live.  Once the owner is gone, an unbound reservation is
        provably orphaned; a process-bound reservation remains charged until the
        child itself has exited.
        """
        removed = 0
        for token, reservation in list(self._launch_reservations.items()):
            owner_task = reservation.get("owner_task")
            if not self._task_finished(owner_task):
                continue
            proc = reservation.get("proc")
            if proc is not None:
                exited, _ = self._process_exit_state({"proc": proc})
                if not exited:
                    continue
            if self._launch_reservations.pop(token, None) is not None:
                removed += 1
        return removed

    def bind_launch_process(self, reservation_token: object | None, proc: Any) -> bool:
        """Bind a reserved slot to its spawned process before any post-spawn await."""
        if reservation_token is None:
            return False
        reservation = self._launch_reservations.get(reservation_token)
        if reservation is None:
            return False
        reservation["proc"] = proc
        return True

    def launch_reservation_count(self, user_id: str | None = None) -> int:
        self.reconcile_launch_reservations()
        return sum(
            1
            for reservation in self._launch_reservations.values()
            if user_id is None or reservation.get("user_id") == user_id
        )

    def capacity_count(self, user_id: str | None = None) -> int:
        return self.active_count(user_id) + self.launch_reservation_count(user_id)

    def try_reserve_launch(self, user_id: str | None, limit: int) -> object | None:
        """Atomically reserve one launch slot before the first spawn await."""
        if limit <= 0:
            return None
        self.reconcile()
        self.reconcile_launch_reservations()
        active = sum(
            1
            for session in self.sessions.values()
            if self.is_active(session) and (user_id is None or session.get("user_id") == user_id)
        )
        if active + self.launch_reservation_count(user_id) >= limit:
            return None
        token = object()
        try:
            owner_task = asyncio.current_task()
        except RuntimeError:
            owner_task = None
        self._launch_reservations[token] = {
            "user_id": user_id,
            "owner_task": owner_task,
            "proc": None,
        }
        return token

    def release_launch_reservation(self, reservation_token: object | None) -> bool:
        if reservation_token is None or reservation_token not in self._launch_reservations:
            return False
        del self._launch_reservations[reservation_token]
        return True

    @contextmanager
    def reserve_launch(self, user_id: str | None, limit: int) -> Iterator[object | None]:
        """Reserve capacity for one launch and always release an unconsumed slot."""
        reservation_token = self.try_reserve_launch(user_id, limit)
        try:
            yield reservation_token
        finally:
            self.release_launch_reservation(reservation_token)

    def clear_launch_reservations(self) -> int:
        """Discard pre-registration slots that cannot survive an application lifespan."""
        cleared = len(self._launch_reservations)
        self._launch_reservations.clear()
        return cleared

    def reap(self, *, now: float | None = None) -> list[str]:
        """Reconcile completion, evict expired sessions, and enforce the retained cap."""
        current = time.time() if now is None else now
        self.reconcile(now=current)
        self.reconcile_launch_reservations()
        removable = [
            (session_id, session)
            for session_id, session in self.sessions.items()
            if session.get("done")
            and current - float(session.get("completed_at") or session.get("created_at") or current)
            >= COMMAND_SESSION_TTL_SECONDS
        ]
        removed: list[str] = []
        for session_id, _ in removable:
            if self.remove(session_id) is not None:
                removed.append(session_id)

        completed = sorted(
            (
                (session_id, session)
                for session_id, session in self.sessions.items()
                if session.get("done")
            ),
            key=lambda item: float(item[1].get("completed_at") or item[1].get("created_at") or 0.0),
            reverse=True,
        )
        excess = max(0, len(completed) - COMMAND_SESSION_MAX_RETAINED)
        for session_id, _ in reversed(completed[-excess:] if excess else []):
            if self.remove(session_id) is not None:
                removed.append(session_id)
        return removed

    def passive_stats(self) -> dict[str, int]:
        """Return a read-only execution projection without reconciliation or OS probes.

        Monitoring must never mutate command ownership or consume process state. A
        child whose cached returncode/process-wait task already proves exit is
        reported separately as ``exited_unreconciled`` until the normal reaper or
        explicit maintenance path reconciles the registry.
        """
        active = 0
        exited_unreconciled = 0
        completed = 0
        retained_output_bytes = 0
        for session in self.sessions.values():
            retained_output_bytes += len(session.get("output") or b"")
            if session.get("done"):
                completed += 1
                continue
            proc = session.get("proc")
            cached_exit = proc is not None and getattr(proc, "returncode", None) is not None
            wait_task = session.get("process_wait_task")
            waiter_done = False
            done = getattr(wait_task, "done", None)
            if callable(done):
                try:
                    waiter_done = bool(done())
                except Exception:
                    waiter_done = False
            if cached_exit or waiter_done:
                exited_unreconciled += 1
            else:
                active += 1

        launching = len(self._launch_reservations)
        return {
            "active": active,
            "launching": launching,
            "capacity_used": active + launching,
            "completed_retained": completed,
            "exited_unreconciled": exited_unreconciled,
            "total_retained": len(self.sessions),
            "retained_output_bytes": retained_output_bytes,
            "total_created": self.total_created,
            "total_reaped": self.total_reaped,
            "retained_cap": COMMAND_SESSION_MAX_RETAINED,
        }

    def stats(self) -> dict[str, int]:
        self.reconcile()
        self.reconcile_launch_reservations()
        active = sum(1 for session in self.sessions.values() if self.is_active(session))
        completed = len(self.sessions) - active
        retained_output_bytes = sum(
            len(session.get("output") or b"") for session in self.sessions.values()
        )
        launching = len(self._launch_reservations)
        return {
            "active": active,
            "launching": launching,
            "capacity_used": active + launching,
            "completed_retained": completed,
            "total_retained": len(self.sessions),
            "retained_output_bytes": retained_output_bytes,
            "total_created": self.total_created,
            "total_reaped": self.total_reaped,
            "retained_cap": COMMAND_SESSION_MAX_RETAINED,
        }


command_session_registry = CommandSessionRegistry()
