import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_runtime import FactoryRuntime
from cptr.services.factory_store import SqlFactoryStore


class FactoryRestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_interrupted_mutation_enters_recovering_once_and_preserves_prior_state(self):
        run = await self._create_run("recover-implementing")
        await self._advance(run.id, FactoryState.IMPLEMENTING)
        runtime = FactoryRuntime(
            store=self.store,
            owner_token="runtime-a",
            lease_ms=5_000,
        )

        recovered = await runtime.recover_active_runs()
        again = await runtime.recover_active_runs()

        self.assertEqual(recovered, [run.id])
        self.assertEqual(again, [])
        reloaded = await self.store.get_run(run.id)
        self.assertEqual(reloaded.state, FactoryState.RECOVERING.value)
        self.assertEqual(reloaded.resumable_state, FactoryState.IMPLEMENTING.value)
        self.assertNotEqual(reloaded.state, FactoryState.COMPLETE.value)
        events = await self.store.list_events(run.id)
        recovery_edges = [
            event
            for event in events
            if event.from_state == FactoryState.IMPLEMENTING.value
            and event.to_state == FactoryState.RECOVERING.value
        ]
        self.assertEqual(len(recovery_edges), 1)

    async def test_recovered_run_can_resume_only_to_its_recorded_prior_state(self):
        run = await self._create_run("recover-resume")
        await self._advance(run.id, FactoryState.IMPLEMENTING)
        runtime = FactoryRuntime(store=self.store, owner_token="runtime-a", lease_ms=5_000)
        await runtime.recover_active_runs()

        with self.assertRaisesRegex(ValueError, "resumable"):
            await self.store.transition(
                run.id,
                to_state=FactoryState.AUDITING,
                actor=FactoryActor.SYSTEM,
                reason="unsafe recovery shortcut",
                idempotency_key="unsafe-recovery-resume",
            )

        resumed = await self.store.transition(
            run.id,
            to_state=FactoryState.IMPLEMENTING,
            actor=FactoryActor.SYSTEM,
            reason="execution reconciliation proved safe to resume",
            idempotency_key="safe-recovery-resume",
        )
        self.assertEqual(resumed.state, FactoryState.IMPLEMENTING.value)
        self.assertIsNone(resumed.resumable_state)

    async def test_pause_and_approval_states_remain_waiting_across_restart(self):
        paused = await self._create_run("recover-paused")
        await self._advance(paused.id, FactoryState.IMPLEMENTING)
        await self.store.transition(
            paused.id,
            to_state=FactoryState.PAUSED,
            actor=FactoryActor.USER,
            reason="user paused",
            idempotency_key="pause-before-restart",
        )

        approval = await self._create_run("recover-approval")
        await self._advance(approval.id, FactoryState.IMPLEMENTING)
        await self.store.transition(
            approval.id,
            to_state=FactoryState.APPROVAL_REQUIRED,
            actor=FactoryActor.SYSTEM,
            reason="await external mutation approval",
            idempotency_key="approval-before-restart",
        )

        runtime = FactoryRuntime(store=self.store, owner_token="runtime-a", lease_ms=5_000)
        recovered = await runtime.recover_active_runs()

        self.assertNotIn(paused.id, recovered)
        self.assertNotIn(approval.id, recovered)
        self.assertEqual((await self.store.get_run(paused.id)).state, FactoryState.PAUSED.value)
        self.assertEqual(
            (await self.store.get_run(approval.id)).state,
            FactoryState.APPROVAL_REQUIRED.value,
        )

    async def test_initial_mission_recovery_keeps_normal_baseline_path(self):
        run = await self._create_run("recover-mission")
        runtime = FactoryRuntime(store=self.store, owner_token="runtime-a", lease_ms=5_000)

        recovered = await runtime.recover_active_runs()
        reloaded = await self.store.get_run(run.id)

        self.assertEqual(recovered, [run.id])
        self.assertEqual(reloaded.state, FactoryState.RECOVERING.value)
        self.assertIsNone(reloaded.resumable_state)
        baseline = await self.store.transition(
            run.id,
            to_state=FactoryState.BASELINING,
            actor=FactoryActor.SYSTEM,
            reason="initial recovery complete",
            idempotency_key="initial-recovery-baseline",
        )
        self.assertEqual(baseline.state, FactoryState.BASELINING.value)

    async def _create_run(self, key: str):
        return await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission=f"mission {key}",
            acceptance_criteria=["criterion"],
            policy={},
            budget={},
            model_id="configured-model",
            idempotency_key=key,
        )

    async def _advance(self, run_id: str, target: FactoryState) -> None:
        chain = (
            FactoryState.RECOVERING,
            FactoryState.BASELINING,
            FactoryState.UNDERSTANDING,
            FactoryState.AUDITING,
            FactoryState.SELECTING_FINDING,
            FactoryState.CAPABILITY_ANALYSIS,
            FactoryState.SKILL_DISCOVERY,
            FactoryState.TRUST_EVALUATION,
            FactoryState.SKILL_SELECTION,
            FactoryState.REPRODUCING,
            FactoryState.ROOT_CAUSE_ANALYSIS,
            FactoryState.PLANNING,
            FactoryState.IMPLEMENTING,
        )
        for index, state in enumerate(chain):
            if FactoryState((await self.store.get_run(run_id)).state) is state:
                if state is target:
                    return
                continue
            await self.store.transition(
                run_id,
                to_state=state,
                actor=FactoryActor.SYSTEM,
                reason=f"advance to {state.value}",
                idempotency_key=f"advance-{run_id}-{index}",
            )
            if state is target:
                return
        raise AssertionError(f"target state {target.value} was not reached")


_FACTORY_SEED_SCRIPT = r"""
import asyncio
import os

from cptr.models import User, Workspace
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_store import SqlFactoryStore
from cptr.utils.db import init_db


async def main():
    await init_db()
    user_id = await User.create("factory-restart", "password-hash", role="user", created_at=1)
    workspace = await Workspace.upsert(
        user_id,
        os.environ["FACTORY_RESTART_WORKSPACE"],
        "factory-restart",
        {},
    )
    store = SqlFactoryStore()
    run = await store.create_run(
        user_id=user_id,
        workspace_id=workspace.id,
        mission="factory restart recovery",
        acceptance_criteria=["restart remains fail closed"],
        policy={},
        budget={},
        model_id="configured-model",
        idempotency_key="factory-restart-run",
    )
    chain = (
        FactoryState.RECOVERING,
        FactoryState.BASELINING,
        FactoryState.UNDERSTANDING,
        FactoryState.AUDITING,
        FactoryState.SELECTING_FINDING,
        FactoryState.CAPABILITY_ANALYSIS,
        FactoryState.SKILL_DISCOVERY,
        FactoryState.TRUST_EVALUATION,
        FactoryState.SKILL_SELECTION,
        FactoryState.REPRODUCING,
        FactoryState.ROOT_CAUSE_ANALYSIS,
        FactoryState.PLANNING,
        FactoryState.IMPLEMENTING,
    )
    for index, state in enumerate(chain):
        await store.transition(
            run.id,
            to_state=state,
            actor=FactoryActor.SYSTEM,
            reason=f"seed {state.value}",
            idempotency_key=f"factory-restart-seed-{index}",
        )


asyncio.run(main())
"""


class FactoryProcessRestartRecoveryTests(unittest.TestCase):
    def test_server_restart_recovers_factory_once_without_duplicate_transition(self):
        with (
            tempfile.TemporaryDirectory() as data_dir,
            tempfile.TemporaryDirectory() as workspace_dir,
        ):
            subprocess.run(["git", "init", "-q", workspace_dir], check=True)
            env = {
                **os.environ,
                "CPTR_DATA_DIR": data_dir,
                "FACTORY_RESTART_WORKSPACE": workspace_dir,
            }
            subprocess.run([sys.executable, "-c", _FACTORY_SEED_SCRIPT], check=True, env=env)

            port = self._free_port()
            first = self._start_server(data_dir, port)
            second = None
            try:
                self._wait_for_health(port)
                self._wait_until(
                    lambda: (
                        self._query(data_dir, "select state from factory_runs")
                        == FactoryState.RECOVERING.value
                    )
                )
                self.assertEqual(
                    self._query(
                        data_dir,
                        "select count(*) from factory_events "
                        "where from_state='IMPLEMENTING' and to_state='RECOVERING'",
                    ),
                    "1",
                )

                first.terminate()
                first.wait(timeout=10)

                second = self._start_server(data_dir, port)
                self._wait_for_health(port)
                self.assertEqual(
                    self._query(data_dir, "select state from factory_runs"),
                    FactoryState.RECOVERING.value,
                )
                self.assertEqual(
                    self._query(
                        data_dir,
                        "select count(*) from factory_events "
                        "where from_state='IMPLEMENTING' and to_state='RECOVERING'",
                    ),
                    "1",
                )
            finally:
                for process in (first, second):
                    if process and process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _start_server(data_dir, port):
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cptr.cli",
                "run",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--headless",
            ],
            env={**os.environ, "CPTR_DATA_DIR": data_dir},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _wait_for_health(port):
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=1
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("CPTR health endpoint did not start")

    @staticmethod
    def _query(data_dir, query):
        import sqlite3

        connection = sqlite3.connect(os.path.join(data_dir, "app.db"))
        try:
            return str(connection.execute(query).fetchone()[0])
        finally:
            connection.close()

    @staticmethod
    def _wait_until(predicate):
        deadline = time.time() + 15
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError("condition did not become true before timeout")


if __name__ == "__main__":
    unittest.main()
