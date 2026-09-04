import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.env import (
    COMMAND_IDEMPOTENCY_CACHE_MAX_ENTRIES,
    COMMAND_IDEMPOTENCY_CACHE_TTL_SECONDS,
    COMMAND_INLINE_WAIT_MAX_SECONDS,
    COMMAND_LOG_MAX_BYTES,
    COMMAND_SESSION_MAX_RETAINED,
    COMMAND_SESSION_TTL_SECONDS,
    LIVE_EVENT_MAX_REPLAY_EVENTS,
)
from cptr.models import Base, User
from cptr.routers.coding import (
    CommandRequest,
    TestTargetRequest as CodingTestTargetRequest,
    _COMMAND_IDEMPOTENCY,
    _command_idempotency_get,
    _command_idempotency_put,
    start_workspace_command,
)


class ExecutionPersistenceConfigurationTests(unittest.TestCase):
    def test_long_running_state_is_durable_while_inline_wait_is_short(self):
        self.assertEqual(COMMAND_INLINE_WAIT_MAX_SECONDS, 60)
        self.assertGreaterEqual(COMMAND_SESSION_TTL_SECONDS, 30 * 24 * 60 * 60)
        self.assertGreaterEqual(COMMAND_SESSION_MAX_RETAINED, 512)
        self.assertGreaterEqual(COMMAND_LOG_MAX_BYTES, 128 * 1024 * 1024)
        self.assertGreaterEqual(COMMAND_IDEMPOTENCY_CACHE_TTL_SECONDS, 24 * 60 * 60)
        self.assertGreaterEqual(COMMAND_IDEMPOTENCY_CACHE_MAX_ENTRIES, 4096)
        self.assertGreaterEqual(LIVE_EVENT_MAX_REPLAY_EVENTS, 5000)

    def _read_inline_wait_cap_from_fresh_process(self, value: str | None) -> int:
        env = os.environ.copy()
        if value is None:
            env.pop("CPTR_COMMAND_INLINE_WAIT_MAX_SECONDS", None)
        else:
            env["CPTR_COMMAND_INLINE_WAIT_MAX_SECONDS"] = value
        repo_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from cptr.env import COMMAND_INLINE_WAIT_MAX_SECONDS; "
                "print(COMMAND_INLINE_WAIT_MAX_SECONDS)",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return int(completed.stdout.strip())

    def test_inline_wait_defaults_to_protocol_cap_without_service_override(self):
        self.assertEqual(self._read_inline_wait_cap_from_fresh_process(None), 60)

    def test_inline_wait_cannot_be_raised_above_protocol_cap(self):
        self.assertEqual(self._read_inline_wait_cap_from_fresh_process("3600"), 60)

    def test_inline_wait_can_be_tightened_to_fully_nonblocking(self):
        self.assertEqual(self._read_inline_wait_cap_from_fresh_process("0"), 0)

    def test_direct_command_and_test_profiles_are_resume_first_by_default(self):
        self.assertEqual(CommandRequest(command="python -m pytest").wait_seconds, 0)
        self.assertEqual(CodingTestTargetRequest(target="python_pytest").wait_seconds, 0)

    def test_direct_command_schema_accepts_protocol_inline_wait_cap(self):
        request = CommandRequest(
            command="python -m pytest", wait_seconds=COMMAND_INLINE_WAIT_MAX_SECONDS
        )
        self.assertEqual(request.wait_seconds, COMMAND_INLINE_WAIT_MAX_SECONDS)
        with self.assertRaises(ValidationError):
            CommandRequest(
                command="python -m pytest", wait_seconds=COMMAND_INLINE_WAIT_MAX_SECONDS + 1
            )


class DurableCommandIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id="user-1", role="admin", settings={}, created_at=1))
            await db.commit()
        _COMMAND_IDEMPOTENCY.clear()

    async def asyncTearDown(self):
        _COMMAND_IDEMPOTENCY.clear()
        await self.engine.dispose()

    async def _db(self):
        return self.sessions()

    async def test_command_idempotency_survives_memory_cache_loss(self):
        with patch("cptr.routers.coding.get_db", new=AsyncMock(side_effect=self._db)):
            winner = await _command_idempotency_put(
                "user-1", "workspace-1:-", "stable-key", "deadbeef"
            )
            self.assertEqual(winner, "deadbeef")
            _COMMAND_IDEMPOTENCY.clear()
            recovered = await _command_idempotency_get("user-1", "workspace-1:-", "stable-key")
        self.assertEqual(recovered, "deadbeef")

    async def test_replayed_start_returns_original_durable_transcript_without_reexecution(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            log_dir = Path(workspace_root, ".cptr", "task_logs")
            log_dir.mkdir(parents=True)
            (log_dir / "deadbeef.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "start",
                                "command": "python -m pytest",
                                "pid": 1,
                                "ts": 10.0,
                                "pty": False,
                                "rows": 24,
                                "cols": 80,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "output",
                                "stream": "stdout",
                                "data": "verified\n",
                                "offset_end": 9,
                                "ts": 10.1,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "end",
                                "exit_code": 0,
                                "total_bytes": 9,
                                "started_at": 10.0,
                                "command": "python -m pytest",
                                "pty": False,
                                "rows": 24,
                                "cols": 80,
                                "ts": 10.2,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            workspace = SimpleNamespace(path=workspace_root, user_id="user-1")
            request = SimpleNamespace(state=SimpleNamespace(control_scopes=set()))
            body = CommandRequest(
                command="python -m pytest",
                wait_seconds=0,
                idempotency_key="stable-key",
            )
            with patch("cptr.routers.coding.get_db", new=AsyncMock(side_effect=self._db)):
                await _command_idempotency_put("user-1", "workspace-1:-", "stable-key", "deadbeef")
                _COMMAND_IDEMPOTENCY.clear()
                with (
                    patch("cptr.routers.coding._user", new=AsyncMock(return_value="user-1")),
                    patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                    patch(
                        "cptr.routers.coding._coding_root",
                        new=AsyncMock(return_value=Path(workspace_root)),
                    ),
                    patch(
                        "cptr.routers.coding.run_command",
                        new=AsyncMock(side_effect=AssertionError("duplicate command executed")),
                    ),
                ):
                    result = await start_workspace_command(request, "workspace-1", body)

        self.assertEqual(result["command_id"], "deadbeef")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["output"], "verified\n")
        self.assertTrue(result["recovered"])


if __name__ == "__main__":
    unittest.main()
