from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.services.workspace_resolver import (
    AmbiguousWorkspaceError,
    ResolutionStage,
    UnsafeResolutionError,
    WorkspaceNotFoundError,
    WorkspaceResolver,
    resolve_workspace,
    resolve_workspace_for_user,
    slugify,
)


class TestWorkspaceResolver(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.resolver = WorkspaceResolver()

    def test_slugify(self) -> None:
        self.assertEqual(slugify("Cross Repo"), "cross-repo")
        self.assertEqual(slugify("chatgpt_computer-plugin"), "chatgpt-computer-plugin")
        self.assertEqual(slugify("   @cptr/core!   "), "cptr-core")
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify(None), "")

    def test_resolve_empty_query_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.resolver.resolve("", workspaces=[])
        with self.assertRaises(ValueError):
            self.resolver.resolve("   ", workspaces=[])

    def test_uuid_exact_match(self) -> None:
        ws1 = SimpleNamespace(id="ws-uuid-1", name="Alpha", path="/projects/alpha", data={})
        ws2 = SimpleNamespace(id="ws-uuid-2", name="Beta", path="/projects/beta", data={})

        result = self.resolver.resolve_detailed("ws-uuid-1", workspaces=[ws1, ws2])
        self.assertEqual(result.workspace, ws1)
        self.assertEqual(result.stage, ResolutionStage.UUID)
        self.assertEqual(result.confidence, 1.0)

        # Case-insensitive UUID
        result_ci = self.resolver.resolve_detailed("WS-UUID-2", workspaces=[ws1, ws2])
        self.assertEqual(result_ci.workspace, ws2)
        self.assertEqual(result_ci.stage, ResolutionStage.UUID)

    def test_uuid_ambiguity_raises(self) -> None:
        ws1 = SimpleNamespace(id="dup-id", name="Alpha", path="/projects/alpha", data={})
        ws2 = SimpleNamespace(id="dup-id", name="Beta", path="/projects/beta", data={})

        with self.assertRaises(AmbiguousWorkspaceError) as ctx:
            self.resolver.resolve("dup-id", workspaces=[ws1, ws2])
        self.assertEqual(ctx.exception.stage, ResolutionStage.UUID.value)
        self.assertEqual(ctx.exception.candidates, [ws1, ws2])

    def test_legacy_exact_path_preserved_with_duplicate_names(self) -> None:
        # Two workspaces with the exact same name "computer", but different paths
        ws1 = SimpleNamespace(
            id="ws-1",
            name="computer",
            path="/home/user/repos/team-a/computer",
            data={},
        )
        ws2 = SimpleNamespace(
            id="ws-2",
            name="computer",
            path="/home/user/repos/team-b/computer",
            data={},
        )

        # Legacy exact path must resolve uniquely to ws1
        res1 = self.resolver.resolve_detailed(
            "/home/user/repos/team-a/computer",
            workspaces=[ws1, ws2],
        )
        self.assertEqual(res1.workspace, ws1)
        self.assertEqual(res1.stage, ResolutionStage.EXACT_PATH)
        self.assertEqual(res1.confidence, 1.0)

        # Legacy path with trailing slash
        res2 = self.resolver.resolve_detailed(
            "/home/user/repos/team-b/computer/",
            workspaces=[ws1, ws2],
        )
        self.assertEqual(res2.workspace, ws2)
        self.assertEqual(res2.stage, ResolutionStage.EXACT_PATH)

    def test_legacy_exact_path_with_spaces_and_quotes(self) -> None:
        ws = SimpleNamespace(
            id="ws-space",
            name="cross repo",
            path="/home/shacker/Desktop/cross repo/computer",
            data={},
        )

        # Exact path with spaces
        res1 = self.resolver.resolve_detailed(
            "/home/shacker/Desktop/cross repo/computer",
            workspaces=[ws],
        )
        self.assertEqual(res1.workspace, ws)
        self.assertEqual(res1.stage, ResolutionStage.EXACT_PATH)

        # Quoted path
        res2 = self.resolver.resolve_detailed(
            '"/home/shacker/Desktop/cross repo/computer"',
            workspaces=[ws],
        )
        self.assertEqual(res2.workspace, ws)
        self.assertEqual(res2.stage, ResolutionStage.EXACT_PATH)

        # URL-encoded path with %20
        res3 = self.resolver.resolve_detailed(
            "/home/shacker/Desktop/cross%20repo/computer",
            workspaces=[ws],
        )
        self.assertEqual(res3.workspace, ws)
        self.assertEqual(res3.stage, ResolutionStage.EXACT_PATH)

        # file:// URI
        res4 = self.resolver.resolve_detailed(
            "file:///home/shacker/Desktop/cross%20repo/computer",
            workspaces=[ws],
        )
        self.assertEqual(res4.workspace, ws)
        self.assertEqual(res4.stage, ResolutionStage.EXACT_PATH)

    def test_duplicate_names_raise_ambiguity_error(self) -> None:
        # Resolving by duplicate name "computer" without path must fail closed
        ws1 = SimpleNamespace(
            id="ws-1",
            name="computer",
            path="/home/user/repos/team-a/computer",
            data={},
        )
        ws2 = SimpleNamespace(
            id="ws-2",
            name="computer",
            path="/home/user/repos/team-b/computer",
            data={},
        )

        with self.assertRaises(AmbiguousWorkspaceError) as ctx:
            self.resolver.resolve("computer", workspaces=[ws1, ws2])

        self.assertIn("duplicate", str(ctx.exception).lower())
        self.assertEqual(ctx.exception.stage, ResolutionStage.EXACT_NAME.value)
        self.assertEqual(len(ctx.exception.candidates), 2)
        self.assertIn(ws1, ctx.exception.candidates)
        self.assertIn(ws2, ctx.exception.candidates)

    def test_duplicate_names_disambiguated_by_unique_alias_or_slug(self) -> None:
        # Both have duplicate name "analytics"
        ws1 = SimpleNamespace(
            id="ws-1",
            name="analytics",
            path="/data/analytics-v1",
            data={"alias": "analytics-legacy", "slug": "analytics-v1"},
        )
        ws2 = SimpleNamespace(
            id="ws-2",
            name="analytics",
            path="/data/analytics-v2",
            data={"alias": "analytics-next", "slug": "analytics-v2"},
        )

        # Exact name is ambiguous
        with self.assertRaises(AmbiguousWorkspaceError):
            self.resolver.resolve("analytics", workspaces=[ws1, ws2])

        # Resolving by unique alias succeeds
        res_alias1 = self.resolver.resolve("analytics-legacy", workspaces=[ws1, ws2])
        self.assertEqual(res_alias1, ws1)

        res_alias2 = self.resolver.resolve("analytics-next", workspaces=[ws1, ws2])
        self.assertEqual(res_alias2, ws2)

        # Resolving by unique slug succeeds
        res_slug1 = self.resolver.resolve("analytics-v1", workspaces=[ws1, ws2])
        self.assertEqual(res_slug1, ws1)

        res_slug2 = self.resolver.resolve("analytics-v2", workspaces=[ws1, ws2])
        self.assertEqual(res_slug2, ws2)

    def test_unique_slug_resolution(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-1",
            name="My Awesome Project",
            path="/projects/my_project",
            data={"slug": "awesome-project"},
        )
        ws2 = SimpleNamespace(
            id="ws-2",
            name="Other Project",
            path="/projects/other",
            data={},
        )

        # Explicit slug in data
        res1 = self.resolver.resolve_detailed("awesome-project", workspaces=[ws1, ws2])
        self.assertEqual(res1.workspace, ws1)
        self.assertEqual(res1.stage, ResolutionStage.SLUG)

        # Derived slug from name when queried as slug
        res2 = self.resolver.resolve_detailed("other-project", workspaces=[ws1, ws2])
        self.assertEqual(res2.workspace, ws2)
        self.assertEqual(res2.stage, ResolutionStage.SLUG)

    def test_duplicate_slug_raises_ambiguity_error(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-1",
            name="Service 1",
            path="/projects/srv-1",
            data={"slug": "web-service"},
        )
        ws2 = SimpleNamespace(
            id="ws-2",
            name="Service 2",
            path="/projects/srv-2",
            data={"slug": "web-service"},
        )

        with self.assertRaises(AmbiguousWorkspaceError) as ctx:
            self.resolver.resolve("web-service", workspaces=[ws1, ws2])
        self.assertEqual(ctx.exception.stage, ResolutionStage.SLUG.value)
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_unique_alias_resolution(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-1",
            name="Service One",
            path="/projects/one",
            data={"alias": "primary-api"},
        )
        ws2 = SimpleNamespace(
            id="ws-2",
            name="Service Two",
            path="/projects/two",
            data={"aliases": ["secondary-api", "backup-api"]},
        )

        res1 = self.resolver.resolve_detailed("PRIMARY-API", workspaces=[ws1, ws2])
        self.assertEqual(res1.workspace, ws1)
        self.assertEqual(res1.stage, ResolutionStage.ALIAS)

        res2 = self.resolver.resolve_detailed("backup-api", workspaces=[ws1, ws2])
        self.assertEqual(res2.workspace, ws2)
        self.assertEqual(res2.stage, ResolutionStage.ALIAS)

    def test_duplicate_alias_raises_ambiguity_error(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-1",
            name="One",
            path="/p/1",
            data={"alias": "prod"},
        )
        ws2 = SimpleNamespace(
            id="ws-2",
            name="Two",
            path="/p/2",
            data={"alias": "prod"},
        )

        with self.assertRaises(AmbiguousWorkspaceError) as ctx:
            self.resolver.resolve("prod", workspaces=[ws1, ws2])
        self.assertEqual(ctx.exception.stage, ResolutionStage.ALIAS.value)
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_exact_name_unique_resolution(self) -> None:
        ws1 = SimpleNamespace(id="ws-1", name="Cross Repo Core", path="/projects/core", data={})
        ws2 = SimpleNamespace(id="ws-2", name="ChatGPT Plugin", path="/projects/plugin", data={})

        res = self.resolver.resolve_detailed("cross repo core", workspaces=[ws1, ws2])
        self.assertEqual(res.workspace, ws1)
        self.assertEqual(res.stage, ResolutionStage.EXACT_NAME)

    def test_repo_basename_match(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-1", name="Workspace One", path="/opt/repos/data-engine", data={}
        )
        ws2 = SimpleNamespace(
            id="ws-2", name="Workspace Two", path="/opt/repos/web-engine", data={}
        )

        res = self.resolver.resolve_detailed("data-engine", workspaces=[ws1, ws2])
        self.assertEqual(res.workspace, ws1)
        self.assertEqual(res.stage, ResolutionStage.REPO_MATCH)

    def test_duplicate_repo_basename_raises_ambiguity_error(self) -> None:
        ws1 = SimpleNamespace(id="ws-1", name="Alpha Engine", path="/opt/v1/engine", data={})
        ws2 = SimpleNamespace(id="ws-2", name="Beta Engine", path="/opt/v2/engine", data={})

        with self.assertRaises(AmbiguousWorkspaceError) as ctx:
            self.resolver.resolve("engine", workspaces=[ws1, ws2])
        self.assertEqual(ctx.exception.stage, ResolutionStage.REPO_MATCH.value)
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_fuzzy_matching_in_non_destructive_mode(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-1", name="ChatGPT Chrome Extension", path="/r/chrome-ext", data={}
        )
        ws2 = SimpleNamespace(id="ws-2", name="Dark Factory Engine", path="/r/factory-srv", data={})

        # Single fuzzy match
        res = self.resolver.resolve_detailed("Chrome", workspaces=[ws1, ws2], destructive=False)
        self.assertEqual(res.workspace, ws1)
        self.assertEqual(res.stage, ResolutionStage.FUZZY)

    def test_fuzzy_matching_ambiguity(self) -> None:
        ws1 = SimpleNamespace(id="ws-1", name="chatgpt-chrome-extension", path="/r/chrome", data={})
        ws2 = SimpleNamespace(
            id="ws-2", name="chatgpt-terminal-plugin", path="/r/terminal", data={}
        )

        with self.assertRaises(AmbiguousWorkspaceError) as ctx:
            self.resolver.resolve("chatgpt", workspaces=[ws1, ws2], destructive=False)
        self.assertEqual(ctx.exception.stage, ResolutionStage.FUZZY.value)
        self.assertEqual(len(ctx.exception.candidates), 2)

    def test_destructive_mode_forbids_fuzzy_resolution(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-1", name="Critical Production Repo", path="/repos/critical-repo", data={}
        )

        # In non-destructive mode, "Production" resolves fuzzily
        res = self.resolver.resolve("Production", workspaces=[ws1], destructive=False)
        self.assertEqual(res, ws1)

        # In destructive mode, fuzzy resolution MUST be denied
        with self.assertRaises(UnsafeResolutionError) as ctx:
            self.resolver.resolve("Production", workspaces=[ws1], destructive=True)
        self.assertIn("destructive", str(ctx.exception).lower())
        self.assertIn(ws1, ctx.exception.candidates)

    def test_destructive_mode_allows_exact_matches(self) -> None:
        ws1 = SimpleNamespace(
            id="ws-dest-1",
            name="Production DB",
            path="/repos/production-db",
            data={"alias": "prod-db", "slug": "production-db"},
        )

        # Exact UUID in destructive mode
        self.assertEqual(
            self.resolver.resolve("ws-dest-1", workspaces=[ws1], destructive=True),
            ws1,
        )

        # Exact path in destructive mode
        self.assertEqual(
            self.resolver.resolve("/repos/production-db", workspaces=[ws1], destructive=True),
            ws1,
        )

        # Exact name in destructive mode
        self.assertEqual(
            self.resolver.resolve("Production DB", workspaces=[ws1], destructive=True),
            ws1,
        )

        # Exact alias in destructive mode
        self.assertEqual(
            self.resolver.resolve("prod-db", workspaces=[ws1], destructive=True),
            ws1,
        )

        # Exact slug in destructive mode
        self.assertEqual(
            self.resolver.resolve("production-db", workspaces=[ws1], destructive=True),
            ws1,
        )

    def test_unknown_workspace_raises_not_found(self) -> None:
        ws1 = SimpleNamespace(id="ws-1", name="Repo Alpha", path="/r/alpha", data={})

        with self.assertRaises(WorkspaceNotFoundError) as ctx:
            self.resolver.resolve("non-existent-workspace", workspaces=[ws1])
        self.assertEqual(ctx.exception.query, "non-existent-workspace")

        with self.assertRaises(WorkspaceNotFoundError):
            self.resolver.resolve("non-existent-workspace", workspaces=[ws1], destructive=True)

    def test_duck_typing_with_dicts(self) -> None:
        ws1 = {
            "id": "dict-1",
            "name": "Dict Workspace",
            "path": "/tmp/dict-workspace",
            "data": {"alias": "d-ws"},
        }
        res_alias = resolve_workspace("d-ws", workspaces=[ws1])
        self.assertEqual(res_alias, ws1)

        res_path = resolve_workspace("/tmp/dict-workspace", workspaces=[ws1])
        self.assertEqual(res_path, ws1)

    async def test_resolve_for_user_from_db(self) -> None:
        ws1 = SimpleNamespace(id="user-ws-1", name="User Repo", path="/user/repo", data={})
        ws_archived = SimpleNamespace(
            id="user-ws-2",
            name="Archived Repo",
            path="/user/archived",
            data={"_cptr_archived": True},
        )

        with patch(
            "cptr.models.workspaces.Workspace.get_by_user",
            new=AsyncMock(return_value=[ws1, ws_archived]),
        ) as mock_get:
            # Active repo resolves
            res = await resolve_workspace_for_user("u-123", "User Repo")
            self.assertEqual(res, ws1)
            mock_get.assert_awaited_once_with("u-123")

            # Archived repo excluded by default
            with self.assertRaises(WorkspaceNotFoundError):
                await resolve_workspace_for_user("u-123", "Archived Repo")

            # Archived repo included if requested
            res_archived = await resolve_workspace_for_user(
                "u-123", "Archived Repo", include_archived=True
            )
            self.assertEqual(res_archived, ws_archived)


class TestWorkspaceResolverHardening(unittest.IsolatedAsyncioTestCase):
    """Regression tests for hardening fixes applied after initial audit.

    Covers:
    - slugify(None) returning "" (static contract mismatch fix)
    - blank user_id rejected at resolve_for_user boundary
    - allow_fuzzy=False path raises WorkspaceNotFoundError, not fuzzy result
    - UnsafeResolutionError.candidates populated correctly for destructive+fuzzy
    - resolve_for_user does NOT leak archived workspaces via blank filter
    """

    def setUp(self) -> None:
        self.resolver = WorkspaceResolver()

    # -----------------------------------------------------------------------
    # Fix 1: slugify None-safety (annotation + runtime contract)
    # -----------------------------------------------------------------------

    def test_slugify_none_returns_empty_string(self) -> None:
        """slugify(None) must return '' — annotation now accepts str | None."""
        self.assertEqual(slugify(None), "")

    def test_slugify_none_type_accepted(self) -> None:
        """Calling slugify with explicit None must not raise TypeError."""
        try:
            result = slugify(None)
        except TypeError as exc:  # pragma: no cover
            self.fail(f"slugify(None) raised TypeError: {exc}")
        self.assertIsInstance(result, str)

    def test_slugify_whitespace_only_returns_empty_string(self) -> None:
        """Pure-whitespace input collapses to empty slug."""
        self.assertEqual(slugify("   "), "")

    def test_slugify_special_chars_only_returns_empty_string(self) -> None:
        """Input that normalizes entirely to separators yields empty slug."""
        self.assertEqual(slugify("---"), "")
        self.assertEqual(slugify("@#$"), "")

    # -----------------------------------------------------------------------
    # Fix 2: blank user_id guard at resolve_for_user public boundary
    # -----------------------------------------------------------------------

    async def test_resolve_for_user_blank_user_id_raises_value_error(self) -> None:
        """resolve_for_user must reject blank user_id before touching the DB."""
        for bad_id in ("", "   ", "	"):
            with self.assertRaises(ValueError) as ctx:
                await self.resolver.resolve_for_user(bad_id, "some-workspace")
            self.assertIn("user_id", str(ctx.exception).lower())

    async def test_resolve_workspace_for_user_blank_user_id_raises_value_error(self) -> None:
        """resolve_workspace_for_user convenience function propagates blank user_id guard."""
        with self.assertRaises(ValueError):
            await resolve_workspace_for_user("", "some-workspace")

    # -----------------------------------------------------------------------
    # allow_fuzzy=False path (previously uncovered boundary)
    # -----------------------------------------------------------------------

    def test_allow_fuzzy_false_raises_not_found_instead_of_fuzzy(self) -> None:
        """When allow_fuzzy=False, resolver must raise WorkspaceNotFoundError
        even if fuzzy matching would produce a unique candidate."""
        ws1 = SimpleNamespace(
            id="ws-1",
            name="Production Backend",
            path="/repos/prod-backend",
            data={},
        )
        with self.assertRaises(WorkspaceNotFoundError) as ctx:
            self.resolver.resolve("backend", workspaces=[ws1], allow_fuzzy=False)
        self.assertIn("fuzzy", str(ctx.exception).lower())
        self.assertEqual(ctx.exception.query, "backend")

    # -----------------------------------------------------------------------
    # UnsafeResolutionError candidates populated (regression guard)
    # -----------------------------------------------------------------------

    def test_destructive_fuzzy_unsafe_error_has_candidates(self) -> None:
        """UnsafeResolutionError raised for destructive+fuzzy must expose candidates."""
        ws1 = SimpleNamespace(
            id="ws-critical",
            name="Critical Production DB",
            path="/repos/critical-db",
            data={},
        )
        with self.assertRaises(UnsafeResolutionError) as ctx:
            self.resolver.resolve("Production", workspaces=[ws1], destructive=True)
        self.assertGreater(len(ctx.exception.candidates), 0)
        self.assertEqual(ctx.exception.query, "Production")

    def test_destructive_no_match_raises_not_found_not_unsafe(self) -> None:
        """If destructive mode has zero fuzzy candidates it raises WorkspaceNotFoundError,
        not UnsafeResolutionError — the two error types must not be confused."""
        ws1 = SimpleNamespace(
            id="ws-1",
            name="Alpha Service",
            path="/repos/alpha",
            data={},
        )
        with self.assertRaises(WorkspaceNotFoundError):
            self.resolver.resolve("zzz-no-match-at-all", workspaces=[ws1], destructive=True)

    # -----------------------------------------------------------------------
    # Ambiguity determinism: stage attribute must always be the .value string
    # -----------------------------------------------------------------------

    def test_ambiguous_error_stage_is_always_string_value(self) -> None:
        """AmbiguousWorkspaceError.stage must be the string value of ResolutionStage,
        not the enum member — preserves Phase 1 serialization compatibility."""
        ws1 = SimpleNamespace(id="dup", name="Dup", path="/a", data={})
        ws2 = SimpleNamespace(id="dup", name="Dup2", path="/b", data={})

        with self.assertRaises(AmbiguousWorkspaceError) as ctx:
            self.resolver.resolve("dup", workspaces=[ws1, ws2])

        self.assertIsInstance(ctx.exception.stage, str)
        self.assertNotIsInstance(ctx.exception.stage, ResolutionStage)

    # -----------------------------------------------------------------------
    # Verification: UUID -> slug -> alias -> legacy path/name/repo ordering
    # -----------------------------------------------------------------------

    def test_precedence_uuid_over_slug_and_path(self) -> None:
        """Exact UUID takes precedence over conflicting slug and legacy path."""
        ws_uuid = SimpleNamespace(id="target-id", name="Target", path="/p/1", data={})
        ws_slug = SimpleNamespace(
            id="other-1", name="Other", path="/p/2", slug="target-id", data={}
        )
        ws_path = SimpleNamespace(id="other-2", name="Other2", path="target-id", data={})

        res = self.resolver.resolve_detailed("target-id", workspaces=[ws_uuid, ws_slug, ws_path])
        self.assertEqual(res.workspace, ws_uuid)
        self.assertEqual(res.stage, ResolutionStage.UUID)

    def test_precedence_slug_over_alias_and_legacy_path(self) -> None:
        """Explicit slug takes precedence over conflicting alias and legacy path."""
        ws_slug = SimpleNamespace(id="ws-slug", name="S1", path="/p/s1", slug="common-ref", data={})
        ws_alias = SimpleNamespace(
            id="ws-alias", name="S2", path="/p/s2", data={"alias": "common-ref"}
        )
        ws_path = SimpleNamespace(id="ws-path", name="S3", path="common-ref", data={})

        res = self.resolver.resolve_detailed("common-ref", workspaces=[ws_slug, ws_alias, ws_path])
        self.assertEqual(res.workspace, ws_slug)
        self.assertEqual(res.stage, ResolutionStage.SLUG)

    def test_precedence_alias_over_legacy_path_and_name(self) -> None:
        """Alias takes precedence over conflicting legacy path and name."""
        ws_alias = SimpleNamespace(
            id="ws-alias", name="Service A", path="/p/a", data={"alias": "shared-key"}
        )
        ws_path = SimpleNamespace(id="ws-path", name="Service B", path="shared-key", data={})
        ws_name = SimpleNamespace(id="ws-name", name="shared-key", path="/p/c", data={})

        res = self.resolver.resolve_detailed("shared-key", workspaces=[ws_alias, ws_path, ws_name])
        self.assertEqual(res.workspace, ws_alias)
        self.assertEqual(res.stage, ResolutionStage.ALIAS)

    def test_precedence_legacy_path_over_name(self) -> None:
        """Legacy exact filesystem path takes precedence over exact workspace name."""
        ws_path = SimpleNamespace(
            id="ws-path", name="Other Name", path="/repos/shared-loc", data={}
        )
        ws_name = SimpleNamespace(
            id="ws-name", name="/repos/shared-loc", path="/repos/other", data={}
        )

        res = self.resolver.resolve_detailed("/repos/shared-loc", workspaces=[ws_path, ws_name])
        self.assertEqual(res.workspace, ws_path)
        self.assertEqual(res.stage, ResolutionStage.EXACT_PATH)

    def test_precedence_name_over_derived_slug_and_repo_basename(self) -> None:
        """Exact workspace name takes precedence over repo basename match."""
        ws_name = SimpleNamespace(
            id="ws-name", name="core-engine", path="/repos/other-path", data={}
        )
        ws_repo = SimpleNamespace(id="ws-repo", name="Other", path="/repos/core-engine", data={})

        res = self.resolver.resolve_detailed("core-engine", workspaces=[ws_name, ws_repo])
        self.assertEqual(res.workspace, ws_name)
        self.assertEqual(res.stage, ResolutionStage.EXACT_NAME)

    def test_precedence_derived_slug_over_repo_basename_and_fuzzy(self) -> None:
        """Derived slug takes precedence over substring fuzzy matches."""
        ws_derived = SimpleNamespace(id="ws-dev", name="Custom App", path="/repos/custom", data={})
        ws_fuzzy = SimpleNamespace(
            id="ws-fuz", name="Custom App Extended", path="/repos/ext", data={}
        )

        res = self.resolver.resolve_detailed("custom-app", workspaces=[ws_derived, ws_fuzzy])
        self.assertEqual(res.workspace, ws_derived)
        self.assertEqual(res.stage, ResolutionStage.SLUG)

    # -----------------------------------------------------------------------
    # WorkspaceRef Compatibility Verification
    # -----------------------------------------------------------------------

    def test_workspace_ref_attribute_slug_compatibility(self) -> None:
        """Resolver recognizes WorkspaceRef objects where slug is an attribute (ws.slug)."""
        ws1 = SimpleNamespace(id="ws-1", name="Plugin", path="/repo/plugin", slug="plugin", data={})
        ws2 = SimpleNamespace(
            id="ws-2", name="Plugin", path="/repo/plugin-pr", slug="plugin-pr", data={}
        )

        # Resolving "plugin" matches ws1 by explicit slug attribute without duplicate-name conflict
        res = self.resolver.resolve_detailed("plugin", workspaces=[ws1, ws2])
        self.assertEqual(res.workspace, ws1)
        self.assertEqual(res.stage, ResolutionStage.SLUG)

    def test_workspace_ref_alias_collection_parameter(self) -> None:
        """Resolver accepts external aliases collection (WorkspaceAlias-compatible objects)."""
        ws1 = SimpleNamespace(id="ws-1", name="Plugin", path="/repo/plugin", slug="plugin", data={})
        alias_item = SimpleNamespace(workspace_id="ws-1", alias="main-stack")

        res = self.resolver.resolve_detailed(
            "main-stack",
            workspaces=[ws1],
            aliases=[alias_item],
        )
        self.assertEqual(res.workspace, ws1)
        self.assertEqual(res.stage, ResolutionStage.ALIAS)

    def test_workspace_ref_precedence_parity(self) -> None:
        """Verify full parity with choose_workspace_ref test cases from test_workspace_refs.py."""
        first = SimpleNamespace(
            id="ws-1", name="Plugin", path="/repo/plugin", slug="plugin", data={}
        )
        alias = SimpleNamespace(workspace_id="ws-1", alias="main-stack")

        # 1. id -> UUID
        self.assertEqual(
            self.resolver.resolve_detailed("ws-1", workspaces=[first], aliases=[]).stage,
            ResolutionStage.UUID,
        )
        # 2. slug -> SLUG
        self.assertEqual(
            self.resolver.resolve_detailed("plugin", workspaces=[first], aliases=[]).stage,
            ResolutionStage.SLUG,
        )
        # 3. alias -> ALIAS
        self.assertEqual(
            self.resolver.resolve_detailed("main-stack", workspaces=[first], aliases=[alias]).stage,
            ResolutionStage.ALIAS,
        )
        # 4. legacy_path -> EXACT_PATH
        self.assertEqual(
            self.resolver.resolve_detailed("/repo/plugin", workspaces=[first], aliases=[]).stage,
            ResolutionStage.EXACT_PATH,
        )

    # -----------------------------------------------------------------------
    # String-valued ambiguity stage serialization for all stages
    # -----------------------------------------------------------------------

    def test_all_ambiguity_stages_serialize_as_string(self) -> None:
        """Every ResolutionStage used in AmbiguousWorkspaceError produces a string stage."""
        stages = [
            ResolutionStage.UUID,
            ResolutionStage.SLUG,
            ResolutionStage.ALIAS,
            ResolutionStage.EXACT_PATH,
            ResolutionStage.EXACT_NAME,
            ResolutionStage.REPO_MATCH,
            ResolutionStage.FUZZY,
        ]
        for stage in stages:
            err = AmbiguousWorkspaceError("test", query="q", candidates=[], stage=stage)
            self.assertIsInstance(err.stage, str)
            self.assertEqual(err.stage, stage.value)
            self.assertNotIsInstance(err.stage, ResolutionStage)

            # Also when passed as a raw string directly
            err_str = AmbiguousWorkspaceError("test", query="q", candidates=[], stage=stage.value)
            self.assertIsInstance(err_str.stage, str)
            self.assertEqual(err_str.stage, stage.value)


if __name__ == "__main__":
    unittest.main()
