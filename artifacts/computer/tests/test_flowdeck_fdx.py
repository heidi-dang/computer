import asyncio
import os
import stat
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.flowdeck.durable import DurableFlowDeck
from cptr.flowdeck.fdx import (
    FDXConfig,
    FDXPolicyError,
    FDXResult,
    run_fdx,
    run_optional_fdx,
    validate_workspace_jail,
)
from cptr.models.base import Base


class FDXTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.executable = self.root / "fdx"
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"version\":\"1\","
            "\"health\":\"ok\",\"capabilities\":{\"read_only\":true,\"network_writes\":false,"
            "\"workspace_mutation\":false,\"process_persistence\":false},\"payload\":\"ok\"}'\n"
        )
        self.executable.chmod(
            self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
        )

    def tearDown(self):
        self.temp.cleanup()

    async def asyncSetUp(self):
        db_fd, self.db_path = tempfile.mkstemp()
        os.close(db_fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False),
            clock=lambda: 1000,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_path)

    async def _run_fdx(self, payload, **kwargs):
        kwargs.pop("workspace", None)
        run, _ = await self.store.create_run(
            request_key=f"fdx-{id(payload)}-{len(kwargs)}",
            owner="fdx-test",
            workspace=str(self.workspace),
        )
        if run.status == "PENDING":
            await self.store.start_run(run.id)
        return await run_fdx(
            payload,
            workspace=str(self.workspace),
            store=self.store,
            run_id=run.id,
            **kwargs,
        )

    async def _run_optional_fdx(self, payload, *, fallback, **kwargs):
        kwargs.pop("workspace", None)
        run, _ = await self.store.create_run(
            request_key=f"optional-fdx-{id(payload)}-{len(kwargs)}",
            owner="fdx-test",
            workspace=str(self.workspace),
        )
        if run.status == "PENDING":
            await self.store.start_run(run.id)
        return await run_optional_fdx(
            payload,
            workspace=str(self.workspace),
            store=self.store,
            run_id=run.id,
            fallback=fallback,
            **kwargs,
        )

    def config(self, **overrides):
        values = {
            "enabled": True,
            "executable": str(self.executable),
            "protocol": "flowdeck-fdx/1",
            "timeout_seconds": 2,
            "max_output_bytes": 1024,
            "read_only_verified": True,
        }
        values.update(overrides)
        return FDXConfig(**values)

    async def test_fdx_requires_protocol_and_absolute_executable(self):
        with self.assertRaises(FDXPolicyError):
            await self._run_fdx(
                {},
                workspace=str(self.workspace),
                config=self.config(protocol="wrong"),
            )
        with self.assertRaises(FDXPolicyError):
            await self._run_fdx(
                {},
                workspace=str(self.workspace),
                config=self.config(executable="fdx"),
            )

    async def test_workspace_jail_rejects_escape(self):
        with self.assertRaises(FDXPolicyError):
            validate_workspace_jail(str(self.root), str(self.workspace))
        with self.assertRaises(FDXPolicyError):
            await run_fdx(
                {},
                workspace=str(self.workspace),
                configured_root=str(self.workspace / "nested"),
                config=self.config(),
            )

    async def test_fdx_returns_bounded_structured_output(self):
        result = await self._run_fdx(
            {"task": "inspect"},
            workspace=str(self.workspace),
            configured_root=str(self.root),
            config=self.config(),
        )
        self.assertTrue(result.used_fdx)
        self.assertFalse(result.authoritative)
        self.assertEqual(result.output["protocol"], "flowdeck-fdx/1")

    async def test_fdx_requires_health_and_version(self):
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"payload\":\"ok\"}'\n"
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        with self.assertRaises(FDXPolicyError):
            await self._run_fdx(
                {},
                workspace=str(self.workspace),
                configured_root=str(self.root),
                config=self.config(read_only_verified=False),
            )

    async def test_fdx_rejects_oversized_input_and_timeout_falls_back(self):
        with self.assertRaises(FDXPolicyError):
            await self._run_fdx(
                {"payload": "x" * 2048},
                workspace=str(self.workspace),
                configured_root=str(self.root),
                config=self.config(max_input_bytes=32),
            )
        self.executable.write_text(
            "#!/bin/sh\n"
            "sleep 2\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"version\":\"1\","
            "\"health\":\"ok\",\"capabilities\":{\"read_only\":true,\"network_writes\":false,"
            "\"workspace_mutation\":false,\"process_persistence\":false}}'\n"
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        async def fallback():
            return FDXResult(
                status="succeeded",
                output={"native": True},
                authoritative=True,
                used_fdx=False,
            )

        result = await self._run_optional_fdx(
            {},
            workspace=str(self.workspace),
            configured_root=str(self.root),
            config=self.config(timeout_seconds=0.05),
            fallback=fallback,
        )
        self.assertFalse(result.used_fdx)
        self.assertTrue(result.authoritative)
        self.assertEqual(result.output, {"native": True})

    async def test_enabled_fdx_failure_falls_back_to_native_result(self):
        async def fallback():
            return type(
                "Fallback",
                (),
                {
                    "status": "succeeded",
                    "output": {"native": True},
                    "authoritative": True,
                },
            )()

        result = await self._run_optional_fdx(
            {},
            workspace=str(self.workspace),
            config=self.config(executable=str(self.root / "missing")),
            fallback=fallback,
            configured_root=str(self.root),
        )
        self.assertFalse(result.used_fdx)
        self.assertTrue(result.authoritative)
        self.assertEqual(result.output, {"native": True})
        self.assertIsNotNone(result.fallback_reason)

    async def test_fdx_rejects_workspace_side_effect_and_falls_back(self):
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "printf side-effect > created.txt\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"version\":\"1\","
            "\"health\":\"ok\",\"capabilities\":{\"read_only\":true,\"network_writes\":false,"
            "\"workspace_mutation\":false,\"process_persistence\":false}}'\n"
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        async def fallback():
            return FDXResult(
                status="succeeded",
                output={"native": True},
                authoritative=True,
                used_fdx=False,
            )

        result = await self._run_optional_fdx(
            {},
            workspace=str(self.workspace),
            configured_root=str(self.root),
            config=self.config(read_only_verified=True),
            fallback=fallback,
        )
        self.assertFalse(result.used_fdx)
        self.assertTrue(result.authoritative)
        self.assertFalse((self.workspace / "created.txt").exists())

    async def test_fdx_cleans_workspace_before_releasing_lease(self):
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "printf side-effect > created.txt\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"version\":\"1\","
            "\"health\":\"ok\",\"capabilities\":{\"read_only\":true,\"network_writes\":false,"
            "\"workspace_mutation\":false,\"process_persistence\":false}}'\n"
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        from cptr.flowdeck import fdx

        events = []
        original_restore = fdx._restore_files
        original_release = self.store.release_workspace_lease

        def record_restore(*args, **kwargs):
            events.append("cleanup")
            return original_restore(*args, **kwargs)

        async def record_release(*args, **kwargs):
            events.append("release")
            return await original_release(*args, **kwargs)

        with (
            mock.patch.object(fdx, "_restore_files", side_effect=record_restore),
            mock.patch.object(
                self.store, "release_workspace_lease", side_effect=record_release
            ),
        ):
            with self.assertRaises(FDXPolicyError):
                await self._run_fdx(
                    {},
                    workspace=str(self.workspace),
                    configured_root=str(self.root),
                    config=self.config(),
                )

        self.assertEqual(events, ["cleanup", "release"])
        self.assertFalse((self.workspace / "created.txt").exists())

    async def test_fdx_preserves_newer_mutation_during_cleanup(self):
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "printf fdx > created.txt\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"version\":\"1\","
            "\"health\":\"ok\",\"capabilities\":{\"read_only\":true,\"network_writes\":false,"
            "\"workspace_mutation\":false,\"process_persistence\":false}}'\n"
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        from cptr.flowdeck import fdx

        original_snapshot = fdx._snapshot_files
        snapshots = 0

        def snapshot_with_race(root):
            nonlocal snapshots
            result = original_snapshot(root)
            snapshots += 1
            if snapshots == 3:
                (self.workspace / "created.txt").write_text("newer external data")
            return result

        with mock.patch.object(fdx, "_snapshot_files", side_effect=snapshot_with_race):
            with self.assertRaises(FDXPolicyError):
                await self._run_fdx(
                    {},
                    workspace=str(self.workspace),
                    configured_root=str(self.root),
                    config=self.config(),
                )

        self.assertEqual(
            (self.workspace / "created.txt").read_text(), "newer external data"
        )

    async def test_fdx_rejects_configured_jail_escape_and_restores_it(self):
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "printf escape > ../escaped.txt\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"version\":\"1\","
            "\"health\":\"ok\",\"capabilities\":{\"read_only\":true,\"network_writes\":false,"
            "\"workspace_mutation\":false,\"process_persistence\":false}}'\n"
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        with self.assertRaises(FDXPolicyError):
            await self._run_fdx(
                {},
                workspace=str(self.workspace),
                configured_root=str(self.root),
                config=self.config(read_only_verified=True),
            )
        self.assertFalse((self.root / "escaped.txt").exists())

    async def test_fdx_kills_persistent_child_processes(self):
        self.executable.write_text(
            "#!/bin/sh\n"
            "read payload\n"
            "(sleep 0.3; printf child > child.txt) &\n"
            "printf '%s' '{\"protocol\":\"flowdeck-fdx/1\",\"version\":\"1\","
            "\"health\":\"ok\",\"capabilities\":{\"read_only\":true,\"network_writes\":false,"
            "\"workspace_mutation\":false,\"process_persistence\":false}}'\n"
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        with self.assertRaises(FDXPolicyError):
            await self._run_fdx(
                {},
                workspace=str(self.workspace),
                configured_root=str(self.root),
                config=self.config(read_only_verified=True),
            )
        await asyncio.sleep(0.5)
        self.assertFalse((self.workspace / "child.txt").exists())

    async def test_fdx_rejects_unverified_read_only_parity(self):
        with self.assertRaises(FDXPolicyError):
            await self._run_fdx(
                {},
                workspace=str(self.workspace),
                configured_root=str(self.root),
                config=self.config(read_only_verified=False),
            )

    async def test_fdx_requires_exclusive_durable_workspace_ownership(self):
        first, _ = await self.store.create_run(
            request_key="fdx-owner",
            owner="owner",
            workspace=str(self.workspace),
        )
        if first.status == "PENDING":
            await self.store.start_run(first.id)
        lease = await self.store.acquire_workspace_lease(
            workspace=str(self.workspace),
            run_id=first.id,
            owner="owner",
            ttl_ms=120_000,
        )
        self.assertIsNotNone(lease)
        second, _ = await self.store.create_run(
            request_key="fdx-contender",
            owner="contender",
            workspace=str(self.workspace),
        )
        if second.status == "PENDING":
            await self.store.start_run(second.id)
        with self.assertRaises(FDXPolicyError):
            await run_fdx(
                {},
                workspace=str(self.workspace),
                configured_root=str(self.root),
                config=self.config(read_only_verified=True),
                store=self.store,
                run_id=second.id,
                owner="contender",
            )
        await self.store.release_workspace_lease(
            workspace=str(self.workspace),
            owner="owner",
            epoch=lease.epoch,
        )

    async def test_disabled_fdx_preserves_fallback_without_process(self):
        called = False

        async def fallback():
            nonlocal called
            called = True
            return FDXResult(
                status="succeeded",
                output={},
                authoritative=True,
                used_fdx=False,
            )

        result = await run_optional_fdx(
            {},
            workspace=str(self.workspace),
            config=FDXConfig(),
            fallback=fallback,
        )
        self.assertTrue(called)
        self.assertFalse(result.used_fdx)
        self.assertTrue(result.authoritative)
