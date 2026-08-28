import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.coding import FdxIntelligenceRequest, run_fdx_intelligence
from cptr.services.fdx_intelligence import (
    FdxIntelligenceError,
    FdxIntelligenceService,
)
from cptr.utils.identity import ExecutionIdentity


def _identity(home: str) -> ExecutionIdentity:
    return ExecutionIdentity(
        app_user_id="user_1",
        username="tester",
        uid=os.getuid() if hasattr(os, "getuid") else None,
        gid=os.getgid() if hasattr(os, "getgid") else None,
        groups=tuple(os.getgroups()) if hasattr(os, "getgroups") else (),
        home=home,
        shell=os.environ.get("SHELL", "/bin/sh"),
        is_pam=False,
    )


def _fake_fdx(root: Path) -> Path:
    script = root / "fdx"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys
            from pathlib import Path

            if "--version" in sys.argv:
                print("fdx 9.9.9-test")
                raise SystemExit(0)

            if len(sys.argv) > 1 and sys.argv[1] == "capabilities":
                print(json.dumps({"fdx_protocol_version": 2, "capability_contract_version": 1}))
                raise SystemExit(0)

            if len(sys.argv) > 1 and sys.argv[1] == "tree":
                print(json.dumps({"payload": "x" * 2000000}))
                raise SystemExit(0)

            if len(sys.argv) > 1 and sys.argv[1] == "serve":
                root_arg = Path(sys.argv[sys.argv.index("--root") + 1]).resolve()
                for line in sys.stdin:
                    request = json.loads(line)
                    op = request["op"]
                    if op == "negotiate":
                        value = {
                            "protocol": 2,
                            "selected_capabilities": request["args"].get("capabilities", []),
                            "server_capabilities": request["args"].get("capabilities", []),
                        }
                    elif op == "health":
                        value = {"healthy": True, "service": "fake-fdx"}
                    elif op == "search":
                        value = [{"path": str(root_arg / "src" / "main.py"), "symbol": "main"}]
                    elif op == "impact-v2":
                        value = {
                            "assurance": "EXACT",
                            "payload": "x" * 70000,
                            "items": list(range(150)),
                        }
                    else:
                        value = {"op": op, "args": request.get("args", {})}
                    print(json.dumps({"id": request["id"], "ok": True, "value": value}), flush=True)
                raise SystemExit(0)

            print(json.dumps({"argv": sys.argv[1:]}))
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class FdxIntelligenceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_daemon_negotiates_reuses_and_redacts_repository_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            (root / "src").mkdir()
            binary = _fake_fdx(root)
            service = FdxIntelligenceService()
            try:
                with patch("cptr.services.fdx_intelligence.FDX_BINARY", str(binary)):
                    first = await service.execute(
                        user_id="user_1",
                        workspace_id="ws_1",
                        root=root,
                        identity=_identity(temp),
                        action="status",
                        options={},
                    )
                    search = await service.execute(
                        user_id="user_1",
                        workspace_id="ws_1",
                        root=root,
                        identity=_identity(temp),
                        action="search",
                        options={"query": "main", "path": "src", "max_matches": 10},
                    )

                self.assertEqual(first["status"], "ok")
                self.assertEqual(search["status"], "ok")
                self.assertEqual(search["data"][0]["path"], "src/main.py")
                self.assertEqual(len(service._daemons), 1)
                daemon = next(iter(service._daemons.values()))
                self.assertIsNotNone(daemon.process)
                self.assertIsNone(daemon.process.returncode)
            finally:
                await service.close_all()
            self.assertEqual(service._daemons, {})

    async def test_daemon_accepts_jsonl_responses_larger_than_asyncio_default_reader_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            binary = _fake_fdx(root)
            service = FdxIntelligenceService()
            try:
                with patch("cptr.services.fdx_intelligence.FDX_BINARY", str(binary)):
                    result = await service.execute(
                        user_id="user_1",
                        workspace_id="ws_1",
                        root=root,
                        identity=_identity(temp),
                        action="impact_v2",
                        options={"base": "HEAD", "depth": 3},
                    )
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["assurance"], "EXACT")
                self.assertTrue(result["data"]["payload"].startswith("x" * 1000))
                self.assertIn("[FDX string truncated by CPTR]", result["data"]["payload"])
                self.assertEqual(len(result["data"]["items"]), 101)
                self.assertEqual(result["data"]["items"][-1], {"truncated_items": 50})
            finally:
                await service.close_all()

    async def test_repository_bound_action_never_walks_above_authorized_non_git_root(self):
        with tempfile.TemporaryDirectory() as temp:
            service = FdxIntelligenceService()
            result = await service.execute(
                user_id="user_1",
                workspace_id="ws_1",
                root=Path(temp),
                identity=_identity(temp),
                action="impact_v2",
                options={"base": "HEAD"},
            )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"], "FDX_REPOSITORY_ROOT_REQUIRED")
        self.assertTrue(result["fallback_recommended"])

    async def test_unavailable_binary_returns_typed_fallback_instead_of_failing_direct_coding(self):
        with tempfile.TemporaryDirectory() as temp:
            service = FdxIntelligenceService()
            with (
                patch("cptr.services.fdx_intelligence.FDX_BINARY", str(Path(temp) / "missing-fdx")),
                patch("cptr.services.fdx_intelligence.shutil.which", return_value=None),
            ):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=Path(temp),
                    identity=_identity(temp),
                    action="capabilities",
                    options={},
                )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error_code"], "FDX_BINARY_UNAVAILABLE")
        self.assertIn("cptr_code_search_files", result["fallback_tools"])

    async def test_cli_capture_fails_closed_when_native_output_exceeds_transport_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = _fake_fdx(root)
            service = FdxIntelligenceService()
            with patch("cptr.services.fdx_intelligence.FDX_BINARY", str(binary)):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="tree",
                    options={"path": ".", "depth": 2},
                )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_code"], "FDX_RESPONSE_TOO_LARGE")
        self.assertTrue(result["fallback_recommended"])

    def test_cli_builder_rejects_option_shaped_git_refs(self):
        with self.assertRaises(FdxIntelligenceError) as caught:
            FdxIntelligenceService._cli_argv("diff", {"base": "--output=/tmp/leak"})
        self.assertEqual(caught.exception.code, "FDX_INVALID_GIT_REF")

    def test_sanitizer_does_not_corrupt_source_code_division_operators(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = 'candidate = Path(DATA_DIR) / "bin" / executable_name\npath = "/etc/passwd"'
            sanitized = FdxIntelligenceService._sanitize_string(source, root)
        self.assertIn('Path(DATA_DIR) / "bin" / executable_name', sanitized)
        self.assertIn("<redacted-path>", sanitized)
        self.assertNotIn("/etc/passwd", sanitized)


class FdxIntelligenceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_binds_explicit_nested_repo_and_forwards_worker_identity(self):
        request = SimpleNamespace()
        with tempfile.TemporaryDirectory() as temp:
            coding_root = Path(temp)
            repo = coding_root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            body = FdxIntelligenceRequest(
                action="search",
                worker_id="dcw_1",
                repo_path="repo",
                query="PaymentService",
                path="src",
            )
            identity = _identity(temp)
            execute = AsyncMock(
                return_value={
                    "workspace_id": "ws_1",
                    "action": "search",
                    "provider": "fdx_native",
                    "status": "ok",
                    "fallback_recommended": False,
                    "data": [],
                }
            )
            with (
                patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
                patch(
                    "cptr.routers.coding._workspace",
                    new=AsyncMock(return_value=SimpleNamespace(path=temp, user_id="user_1")),
                ),
                patch("cptr.routers.coding._coding_root", new=AsyncMock(return_value=coding_root)),
                patch(
                    "cptr.routers.coding.identity_for_context", new=AsyncMock(return_value=identity)
                ),
                patch("cptr.routers.coding.fdx_intelligence_service.execute", new=execute),
                patch("cptr.routers.coding._touch_worker", new=AsyncMock()) as touch,
            ):
                result = await run_fdx_intelligence(request, "ws_1", body)

        kwargs = execute.await_args.kwargs
        self.assertEqual(kwargs["root"], repo.resolve())
        self.assertEqual(kwargs["action"], "search")
        self.assertEqual(kwargs["options"]["path"], "src")
        self.assertEqual(result["repo_path"], "repo")
        self.assertEqual(result["worker_id"], "dcw_1")
        touch.assert_awaited_once_with("user_1", "ws_1", "dcw_1")


if __name__ == "__main__":
    unittest.main()
