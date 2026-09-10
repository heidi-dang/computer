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
                    elif op == "version":
                        value = {"version": "9.9.9-test"}
                    elif op == "health":
                        value = {"healthy": True, "service": "fake-fdx"}
                    elif op == "read":
                        value = {
                            "path": str(root_arg / request["args"]["path"]),
                            "language": "python",
                            "mode": request["args"].get("mode", "auto"),
                            "total_lines": 10,
                            "symbols": [],
                            "dependencies": [],
                            "options": request["args"],
                        }
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

    async def test_status_and_read_use_resident_daemon_without_per_call_cli_spawns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            binary = _fake_fdx(root)
            service = FdxIntelligenceService()
            try:
                with (
                    patch("cptr.services.fdx_intelligence.FDX_BINARY", str(binary)),
                    patch.object(
                        service,
                        "_run_cli",
                        new=AsyncMock(side_effect=AssertionError("per-call FDX CLI spawn used")),
                    ),
                ):
                    status = await service.execute(
                        user_id="user_1",
                        workspace_id="ws_1",
                        root=root,
                        identity=_identity(temp),
                        action="status",
                        options={},
                    )
                    read = await service.execute(
                        user_id="user_1",
                        workspace_id="ws_1",
                        root=root,
                        identity=_identity(temp),
                        action="read",
                        options={
                            "path": "src/main.py",
                            "mode": "deep",
                            "symbol": "main",
                            "limit": 20,
                            "offset": 2,
                            "with_deps": True,
                            "no_cache": False,
                        },
                    )

                self.assertEqual(status["status"], "ok")
                self.assertEqual(status["data"]["version"], {"text": "fdx 9.9.9-test"})
                self.assertEqual(read["status"], "ok")
                self.assertEqual(read["data"]["path"], "src/main.py")
                self.assertEqual(read["data"]["options"]["mode"], "deep")
                self.assertEqual(read["data"]["options"]["symbol"], "main")
                self.assertTrue(read["data"]["options"]["with_deps"])
                self.assertEqual(len(service._daemons), 1)
            finally:
                await service.close_all()

    async def test_legacy_text_only_daemon_read_falls_back_to_cli_during_rolling_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            daemon = SimpleNamespace(
                request=AsyncMock(return_value={"path": str(root / "src.py"), "text": "legacy"})
            )
            fallback = AsyncMock(
                return_value={
                    "path": str(root / "src.py"),
                    "language": "python",
                    "mode": "prototype",
                    "total_lines": 1,
                    "symbols": [],
                    "dependencies": [],
                }
            )
            service = FdxIntelligenceService()
            with (
                patch.object(service, "_daemon", new=AsyncMock(return_value=daemon)),
                patch.object(service, "_run_cli", new=fallback),
            ):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="read",
                    options={"path": "src.py", "mode": "prototype"},
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["mode"], "prototype")
        fallback.assert_awaited_once()

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

    def test_resolve_binary_discovers_standard_cargo_install_location(self):
        with tempfile.TemporaryDirectory() as temp:
            cargo_bin = Path(temp) / ".cargo" / "bin"
            cargo_bin.mkdir(parents=True)
            binary = cargo_bin / ("fdx.exe" if os.name == "nt" else "fdx")
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            service = FdxIntelligenceService()
            with (
                patch("cptr.services.fdx_intelligence.FDX_BINARY", ""),
                patch("cptr.services.fdx_intelligence.shutil.which", return_value=None),
            ):
                resolved = service._resolve_binary(_identity(temp))
        self.assertEqual(resolved, str(binary.resolve()))

    async def test_cli_environment_exposes_managed_semantic_provider_bin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            home = Path(temp) / "home"
            managed_bin = home / ".cptr" / "lsp" / "node_modules" / ".bin"
            managed_bin.mkdir(parents=True)
            scip = managed_bin / "scip-typescript"
            scip.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            scip.chmod(0o755)
            binary = root / "fdx"
            binary.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import shutil
                    print(json.dumps({
                        "path": os.environ.get("PATH", ""),
                        "scip": shutil.which("scip-typescript"),
                    }))
                    """
                ),
                encoding="utf-8",
            )
            binary.chmod(0o755)
            service = FdxIntelligenceService()
            with patch("cptr.services.fdx_intelligence.FDX_BINARY", str(binary)):
                result = await service._run_cli(
                    root=root,
                    identity=_identity(str(home)),
                    argv=["semantic", "status"],
                )

        self.assertEqual(result["scip"], str(scip.resolve()))
        self.assertIn(str(managed_bin), result["path"].split(os.pathsep))

    async def test_unavailable_binary_returns_typed_fallback_instead_of_failing_direct_coding(self):
        with tempfile.TemporaryDirectory() as temp:
            service = FdxIntelligenceService()
            with (
                patch("cptr.services.fdx_intelligence.FDX_BINARY", str(Path(temp) / "missing-fdx")),
                patch("cptr.services.fdx_intelligence.DATA_DIR", str(Path(temp) / "data")),
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

    async def test_degraded_reason_redacts_absolute_host_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = FdxIntelligenceService()
            with patch.object(
                service,
                "_run_cli",
                new=AsyncMock(
                    side_effect=FdxIntelligenceError(
                        "FDX_OPERATION_FAILED",
                        "native failure at /etc/cptr-private/config",
                    )
                ),
            ):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="capabilities",
                    options={},
                )
        self.assertEqual(result["status"], "degraded")
        self.assertIn("<redacted-path>", result["reason"])
        self.assertNotIn("/etc/cptr-private/config", result["reason"])

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

    def test_sanitizer_preserves_javascript_regex_literals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = r"assert.match(description, /DEGRADED\/UNVERIFIED/);"
            sanitized = FdxIntelligenceService._sanitize_string(source, root)
        self.assertEqual(sanitized, source)

    def test_legacy_impact_defaults_to_native_supported_depth_one(self):
        request = FdxIntelligenceRequest(action="impact", paths=["src"])
        self.assertIsNone(request.depth)
        args = FdxIntelligenceService._daemon_args("impact", {"paths": ["src"]})
        self.assertEqual(args["depth"], 1)

    def test_grep_cli_uses_only_supported_read_only_flags(self):
        argv = FdxIntelligenceService._cli_argv(
            "grep", {"query": "needle", "path": "src", "max_matches": 1}
        )
        self.assertNotIn("--no-tee", argv)
        self.assertIn("--format", argv)
        self.assertIn("json", argv)

    async def test_index_status_self_heals_absent_repository_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            service = FdxIntelligenceService()
            run_cli = AsyncMock(
                side_effect=[
                    {"text": "INDEX absent\nschema=0\ngeneration=0\nfiles=0"},
                    {"text": "INDEX fresh\nfiles=12\nchanged=12\ngeneration=1"},
                    {"text": "INDEX fresh\nschema=10\ngeneration=1\nfiles=12"},
                ]
            )
            with patch.object(service, "_run_cli", new=run_cli):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="index_status",
                    options={},
                )

        self.assertEqual(result["status"], "ok")
        self.assertIn("generation=1", result["data"]["text"])
        self.assertEqual(
            [call.kwargs["argv"] for call in run_cli.await_args_list],
            [["index", "status"], ["index", "--refresh"], ["index", "status"]],
        )

    async def test_build_graph_self_heals_stale_build_providers_before_query(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            service = FdxIntelligenceService()
            run_cli = AsyncMock(
                side_effect=[
                    {
                        "text": (
                            "provider=builtin-package-json\n"
                            "  health=misconfigured\n"
                            "  freshness=stale\n"
                            "  generation=0"
                        )
                    },
                    {"text": "REFRESH provider=builtin-package-json ok nodes=10 edges=9 gen=1"},
                    {"nodes": [{"id": "pkg"}], "edges": []},
                ]
            )
            with patch.object(service, "_run_cli", new=run_cli):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="build_graph",
                    options={},
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["nodes"], [{"id": "pkg"}])
        self.assertEqual(
            [call.kwargs["argv"] for call in run_cli.await_args_list],
            [["build", "status"], ["build", "refresh"], ["build", "graph", "--format", "json"]],
        )

    async def test_semantic_refresh_stays_read_only_without_root_project_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            (root / "src.ts").write_text("export const value = 1;\n", encoding="utf-8")
            service = FdxIntelligenceService()
            run_cli = AsyncMock(
                side_effect=[
                    {"text": "INDEX fresh\nschema=10\ngeneration=1\nfiles=1"},
                    {"text": "SEMANTIC no providers"},
                    {
                        "text": "source=TreeSitter completeness=Conservative strength=Structural degraded=true"
                    },
                ]
            )
            with patch.object(service, "_run_cli", new=run_cli):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="semantic_references",
                    options={"symbol": "value", "lang": "typescript"},
                )

        self.assertEqual(result["status"], "degraded")
        self.assertFalse((root / "tsconfig.json").exists())
        self.assertEqual(
            [call.kwargs["argv"] for call in run_cli.await_args_list],
            [
                ["index", "status"],
                ["semantic", "status"],
                [
                    "semantic",
                    "references",
                    "value",
                    "--lang",
                    "typescript",
                    "--intent",
                    "reference_complete",
                ],
            ],
        )

    async def test_semantic_references_self_heal_missing_semantic_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            (root / "tsconfig.json").write_text("{}", encoding="utf-8")
            service = FdxIntelligenceService()
            run_cli = AsyncMock(
                side_effect=[
                    {"text": "INDEX fresh\nschema=10\ngeneration=1\nfiles=12"},
                    {"text": "SEMANTIC no providers"},
                    {
                        "text": (
                            "SEMANTIC scip-typescript fresh documents=10 occurrences=20 "
                            "nodes=8 edges=12 generation=1"
                        )
                    },
                    {
                        "text": (
                            "source=SCIP completeness=Complete strength=Semantic "
                            "degraded=false matches=2"
                        )
                    },
                ]
            )
            with patch.object(service, "_run_cli", new=run_cli):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="semantic_references",
                    options={"symbol": "PaymentService", "lang": "typescript"},
                )

        self.assertEqual(result["status"], "ok")
        self.assertIn("degraded=false", result["data"]["text"])
        self.assertEqual(
            [call.kwargs["argv"] for call in run_cli.await_args_list],
            [
                ["index", "status"],
                ["semantic", "status"],
                ["semantic", "refresh"],
                [
                    "semantic",
                    "references",
                    "PaymentService",
                    "--lang",
                    "typescript",
                    "--intent",
                    "reference_complete",
                ],
            ],
        )

    async def test_degraded_assurance_preserves_data_and_recommends_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            service = FdxIntelligenceService()
            with patch.object(
                service,
                "_run_cli",
                new=AsyncMock(return_value={"assurance": "DEGRADED", "selected_checks": []}),
            ):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="plan",
                    options={"base": "HEAD"},
                )
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["fallback_recommended"])
        self.assertEqual(result["assurance"], "DEGRADED")
        self.assertEqual(result["data"]["selected_checks"], [])
        self.assertIn("cptr_code_read_file", result["fallback_tools"])

    async def test_semantic_degraded_marker_recommends_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            service = FdxIntelligenceService()
            run_cli = AsyncMock(
                side_effect=[
                    {"text": "INDEX fresh\nschema=10\ngeneration=1\nfiles=12"},
                    {
                        "text": (
                            "provider=scip-typescript\n"
                            "  health=available\n"
                            "  freshness=fresh\n"
                            "  generation=1"
                        )
                    },
                    {
                        "text": "source=TreeSitter completeness=Conservative strength=Structural degraded=true"
                    },
                ]
            )
            with patch.object(service, "_run_cli", new=run_cli):
                result = await service.execute(
                    user_id="user_1",
                    workspace_id="ws_1",
                    root=root,
                    identity=_identity(temp),
                    action="semantic_references",
                    options={"symbol": "PaymentService", "lang": "typescript"},
                )
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["fallback_recommended"])
        self.assertIn("degraded=true", result["data"]["text"])


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
