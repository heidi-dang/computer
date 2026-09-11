import unittest
from types import SimpleNamespace

from cptr.services.workspace_refs import (
    WorkspaceRefAmbiguous,
    WorkspaceRefNotFound,
    choose_workspace_ref,
)


def ws(workspace_id, name, path, slug, *, archived=False):
    return SimpleNamespace(
        id=workspace_id,
        name=name,
        path=path,
        slug=slug,
        data={"_cptr_archived": True} if archived else {},
    )


class WorkspaceRefTests(unittest.TestCase):
    def setUp(self):
        self.first = ws("ws-1", "Plugin", "/repo/plugin", "plugin")
        self.second = ws("ws-2", "Plugin", "/repo/plugin-pr", "plugin-pr")
        self.alias = SimpleNamespace(workspace_id="ws-1", alias="main-stack")

    def test_resolution_precedence_is_stable_id_slug_alias_then_legacy_path(self):
        self.assertEqual(
            choose_workspace_ref(reference="ws-1", workspaces=[self.first], aliases=[]).matched_by,
            "id",
        )
        self.assertEqual(
            choose_workspace_ref(reference="plugin", workspaces=[self.first], aliases=[]).matched_by,
            "slug",
        )
        self.assertEqual(
            choose_workspace_ref(
                reference="main-stack", workspaces=[self.first], aliases=[self.alias]
            ).matched_by,
            "alias",
        )
        self.assertEqual(
            choose_workspace_ref(
                reference="/repo/plugin", workspaces=[self.first], aliases=[]
            ).matched_by,
            "legacy_path",
        )

    def test_duplicate_legacy_name_is_explicitly_ambiguous(self):
        with self.assertRaises(WorkspaceRefAmbiguous) as raised:
            choose_workspace_ref(
                reference="Plugin",
                workspaces=[self.first, self.second],
                aliases=[],
            )
        self.assertEqual(
            {candidate["workspace_id"] for candidate in raised.exception.candidates},
            {"ws-1", "ws-2"},
        )

    def test_archived_workspace_is_not_selected_without_explicit_opt_in(self):
        archived = ws("ws-old", "Old", "/old", "old", archived=True)
        with self.assertRaises(WorkspaceRefNotFound):
            choose_workspace_ref(reference="old", workspaces=[archived], aliases=[])
        resolved = choose_workspace_ref(
            reference="old",
            workspaces=[archived],
            aliases=[],
            include_archived=True,
        )
        self.assertEqual(resolved.workspace.id, "ws-old")


if __name__ == "__main__":
    unittest.main()
