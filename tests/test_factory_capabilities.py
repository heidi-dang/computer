import tempfile
import unittest
from pathlib import Path

from cptr.services.factory_capabilities import (
    CapabilityInventory,
    CapabilityRequirement,
    CapabilityTrustStatus,
    CapabilityVerificationStatus,
    normalize_manifests,
)
from cptr.utils.skills import SkillMeta


class FactoryCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_identity_is_stable_but_digest_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / ".cptr" / "skills" / "reviewer"
            skill_dir.mkdir(parents=True)
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text(
                "---\nname: reviewer\ndescription: Review code\nallowed-tools: read_file\n---\nfirst",
                encoding="utf-8",
            )
            meta = SkillMeta(
                name="reviewer",
                description="Review code",
                location=str(skill_path),
                source="workspace",
                allowed_tools="read_file",
                managed=True,
            )
            inventory = CapabilityInventory(
                skill_discoverer=lambda _workspace: [meta],
                mcp_server_loader=lambda: [],
                include_builtins=False,
            )

            first = (await inventory.discover_local(tmp))[0]
            second = (await inventory.discover_local(tmp))[0]
            skill_path.write_text(
                "---\nname: reviewer\ndescription: Review code\nallowed-tools: read_file\n---\nsecond",
                encoding="utf-8",
            )
            changed = (await inventory.discover_local(tmp))[0]

            self.assertEqual(first.stable_id, second.stable_id)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(first.stable_id, changed.stable_id)
            self.assertNotEqual(first.digest, changed.digest)
            self.assertEqual(first.origin_type, "skill")
            self.assertEqual(first.trust_status, CapabilityTrustStatus.APPROVED)

    async def test_skill_inventory_is_progressive_disclosure_and_normalizes_permissions(self):
        meta = SkillMeta(
            name="researcher",
            description="Research official docs",
            location="/tmp/researcher/SKILL.md",
            source="global",
            allowed_tools="read_file web_search write_file",
            managed=True,
        )
        inventory = CapabilityInventory(
            skill_discoverer=lambda _workspace: [meta],
            mcp_server_loader=lambda: [],
            include_builtins=False,
            skill_digest_loader=lambda _path: "digest-from-metadata-test",
        )

        manifest = (await inventory.discover_local("/workspace"))[0]

        self.assertFalse(hasattr(manifest, "body"))
        self.assertFalse(hasattr(manifest, "content"))
        self.assertEqual(
            set(manifest.permissions),
            {"network:http", "workspace:read", "workspace:write"},
        )
        self.assertEqual(manifest.network_requirements, ("external-http",))
        self.assertEqual(manifest.execution_requirements, ("skill-instructions",))
        self.assertEqual(manifest.verification_status, CapabilityVerificationStatus.LOCAL)

    async def test_configured_mcp_server_manifest_strips_secrets_and_does_not_connect(self):
        calls = 0

        async def servers():
            nonlocal calls
            calls += 1
            return [
                {
                    "id": "docs",
                    "name": "Docs MCP",
                    "type": "mcp",
                    "url": "https://user:password@example.com/mcp?token=secret",
                    "headers": {"Authorization": "Bearer secret"},
                    "version": "2",
                },
                {
                    "id": "local-tools",
                    "type": "mcp_stdio",
                    "command": "python",
                    "args": ["server.py", "--token", "secret"],
                    "env": {"TOKEN": "secret"},
                },
            ]

        inventory = CapabilityInventory(
            skill_discoverer=lambda _workspace: [],
            mcp_server_loader=servers,
            include_builtins=False,
        )
        manifests = await inventory.discover_local("/workspace")

        self.assertEqual(calls, 1)
        self.assertEqual(len(manifests), 2)
        remote = next(item for item in manifests if item.origin_uri.startswith("https://"))
        local = next(item for item in manifests if item.origin_uri.startswith("stdio:"))
        self.assertEqual(remote.origin_uri, "https://example.com/mcp")
        self.assertNotIn("secret", str(remote.to_dict()))
        self.assertEqual(remote.network_requirements, ("external-http",))
        self.assertEqual(local.network_requirements, ())
        self.assertEqual(local.permissions, ("process:execute",))

    async def test_duplicate_manifest_versions_are_normalized_deterministically(self):
        inventory = CapabilityInventory(
            skill_discoverer=lambda _workspace: [],
            mcp_server_loader=lambda: [],
            include_builtins=True,
        )
        manifests = await inventory.discover_local("/workspace")
        duplicated = list(reversed(manifests)) + manifests

        normalized = normalize_manifests(duplicated)

        self.assertEqual(len(normalized), len(manifests))
        self.assertEqual(
            [item.identity for item in normalized],
            sorted(item.identity for item in normalized),
        )

    def test_capability_requirement_normalizes_duplicates_and_rejects_blank_capability(self):
        requirement = CapabilityRequirement.create(
            requirement_id="repo-analysis",
            capabilities=["code-search", "code-search", "impact-analysis"],
            required_permissions=["workspace:read", "workspace:read"],
            network_allowed=False,
        )

        self.assertEqual(requirement.capabilities, ("code-search", "impact-analysis"))
        self.assertEqual(requirement.required_permissions, ("workspace:read",))
        with self.assertRaises(ValueError):
            CapabilityRequirement.create(
                requirement_id="bad",
                capabilities=[""],
                required_permissions=[],
                network_allowed=False,
            )


if __name__ == "__main__":
    unittest.main()
