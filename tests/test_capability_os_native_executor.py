import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cptr.services.capability_os.authority import CapabilityLease
from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.native_executor import (
    NativeActionDenied,
    NativeActionExecutor,
    NativeWorkspaceContext,
)
from cptr.utils.runtime import FileError


class _Resolver:
    def __init__(self, root: Path):
        self.root = root
        self.calls = []

    async def resolve(self, *, user_id: str, workspace_id: str):
        self.calls.append((user_id, workspace_id))
        return NativeWorkspaceContext(
            user_id=user_id,
            workspace_id=workspace_id,
            root=self.root,
            identity=SimpleNamespace(),
        )


class _Runtime:
    files = {}
    writes = []

    @classmethod
    def reset(cls):
        cls.files = {}
        cls.writes = []

    @classmethod
    async def stat_as(cls, identity, path):
        if path not in cls.files:
            raise FileError("not found", 404)
        content = cls.files[path]
        return {"type": "file", "size": len(content.encode("utf-8"))}

    @classmethod
    async def read_text_file_as(cls, identity, path, max_bytes):
        if path not in cls.files:
            raise FileError("not found", 404)
        content = cls.files[path]
        if len(content.encode("utf-8")) > max_bytes:
            raise FileError("too large", 413)
        return {"content": content, "size": len(content.encode("utf-8")), "binary": False}

    @classmethod
    async def write_file_as(cls, identity, path, content):
        cls.files[path] = str(content)
        cls.writes.append((path, str(content)))
        return {"ok": True}

    @classmethod
    async def list_tree_entries_as(cls, identity, path, recursive, offset, limit):
        return {
            "entries": [{"path": "a.txt", "type": "file", "size": 1}],
            "total": 1,
            "truncated": False,
            "next_offset": None,
        }


def _lease(*permissions, limits=None, context=True):
    return CapabilityLease(
        lease_id="lease-1",
        task_id="task-1",
        workload_id="capability:test",
        artifact_digest="sha256:" + "1" * 64,
        permissions=tuple(permissions),
        resource_limits=dict(limits or {}),
        network={"outbound": "deny", "destinations": []},
        credentials={"logicalNames": []},
        approval_id=None,
        parent_lease_id=None,
        policy_decision_id="policy-1",
        runtime_profile="cptr-vm",
        issued_at_ms=1_000,
        expires_at_ms=9_999_999,
        status="active",
        execution_context=(
            {"userId": "user-1", "workspaceId": "workspace-1", "taskSource": "workbench"}
            if context
            else {}
        ),
    )


class NativeActionExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        _Runtime.reset()
        self.resolver = _Resolver(self.root)
        self.status_calls = []
        self.diff_calls = []

        async def status_fn(root, identity):
            self.status_calls.append(root)
            return {"branch": "main", "files": []}

        async def diff_fn(root, identity):
            self.diff_calls.append(root)
            return {"passed": True, "stdout": "", "stderr": "", "returncode": 0}

        self.executor = NativeActionExecutor(
            resolver=self.resolver,
            runtime=_Runtime,
            status_fn=status_fn,
            diff_check_fn=diff_fn,
            clock_ms=lambda: 2_000,
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_unknown_actions_versions_and_missing_context_fail_closed(self):
        permission = CapabilityRequest("filesystem.read", "workspace:workspace-1:**")
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.shell", version="v1", inputs={}, lease=_lease(permission), timeout_ms=1000
            )
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.fs.list", version="v2", inputs={}, lease=_lease(permission), timeout_ms=1000
            )
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.fs.list", version="v1", inputs={}, lease=_lease(permission, context=False), timeout_ms=1000
            )
        self.assertEqual(self.resolver.calls, [])

    async def test_read_requires_exact_lease_permission_and_blocks_escape(self):
        file_path = self.root / "note.txt"
        _Runtime.files[str(file_path)] = "hello"
        denied = _lease(CapabilityRequest("filesystem.read", "workspace:workspace-1:other.txt"))
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.fs.read_text",
                version="v1",
                inputs={"path": "note.txt"},
                lease=denied,
                timeout_ms=1000,
            )
        allowed = _lease(CapabilityRequest("filesystem.read", "workspace:workspace-1:note.txt"))
        result = await self.executor.invoke(
            action_ref="cptr.fs.read_text",
            version="v1",
            inputs={"path": "note.txt"},
            lease=allowed,
            timeout_ms=1000,
        )
        self.assertEqual(result.output["content"], "hello")
        self.assertEqual(result.output["sha256"], hashlib.sha256(b"hello").hexdigest())
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.fs.read_text",
                version="v1",
                inputs={"path": "../escape.txt"},
                lease=allowed,
                timeout_ms=1000,
            )

    async def test_write_requires_stale_hash_and_enforces_byte_budget(self):
        path = self.root / "note.txt"
        _Runtime.files[str(path)] = "old"
        permission = CapabilityRequest("filesystem.write", "workspace:workspace-1:note.txt")
        lease = _lease(permission, limits={"maxCalls": 3, "maxBytesWritten": 3})
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.fs.write_text",
                version="v1",
                inputs={"path": "note.txt", "content": "new", "overwrite": True, "expectedSha256": "0" * 64},
                lease=lease,
                timeout_ms=1000,
            )
        self.assertEqual(_Runtime.files[str(path)], "old")
        old_digest = hashlib.sha256(b"old").hexdigest()
        result = await self.executor.invoke(
            action_ref="cptr.fs.write_text",
            version="v1",
            inputs={"path": "note.txt", "content": "new", "overwrite": True, "expectedSha256": old_digest},
            lease=lease,
            timeout_ms=1000,
        )
        self.assertEqual(result.output["bytesWritten"], 3)
        self.assertEqual(_Runtime.files[str(path)], "new")
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.fs.write_text",
                version="v1",
                inputs={"path": "note.txt", "content": "x", "overwrite": True,
                        "expectedSha256": hashlib.sha256(b"new").hexdigest()},
                lease=lease,
                timeout_ms=1000,
            )
        self.assertEqual(_Runtime.files[str(path)], "new")

    async def test_call_budget_is_enforced_across_actions(self):
        permission = CapabilityRequest("filesystem.read", "workspace:workspace-1:**")
        lease = _lease(permission, limits={"maxCalls": 1})
        await self.executor.invoke(
            action_ref="cptr.fs.list", version="v1", inputs={}, lease=lease, timeout_ms=1000
        )
        with self.assertRaises(NativeActionDenied):
            await self.executor.invoke(
                action_ref="cptr.fs.list", version="v1", inputs={}, lease=lease, timeout_ms=1000
            )

    async def test_git_actions_are_explicit_argv_free_registry_entries(self):
        permission = CapabilityRequest("git.read", "workspace:workspace-1:**")
        lease = _lease(permission, limits={"maxCalls": 2})
        status = await self.executor.invoke(
            action_ref="cptr.git.status", version="v1", inputs={}, lease=lease, timeout_ms=1000
        )
        check = await self.executor.invoke(
            action_ref="cptr.git.diff_check", version="v1", inputs={}, lease=lease, timeout_ms=1000
        )
        self.assertEqual(status.output["branch"], "main")
        self.assertTrue(check.verification_passed)
        self.assertEqual(self.status_calls, [str(self.root)])
        self.assertEqual(self.diff_calls, [str(self.root)])
        self.assertNotIn("cptr.shell", self.executor.action_refs)


if __name__ == "__main__":
    unittest.main()
