import unittest

from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    create_artifact,
)


class CapabilityOsContractTests(unittest.TestCase):
    def test_artifact_digest_is_content_addressed_and_ignores_creation_provenance(self):
        spec = {"entrypoint": "main.py", "runtime": {"class": "gvisor"}}
        first = create_artifact(
            artifact_id="tool.example",
            version="1.0.0",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec=spec,
            created_at="2026-09-07T01:00:00Z",
            task_origin="task-a",
        )
        second = create_artifact(
            artifact_id="tool.example",
            version="1.0.0",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec=spec,
            created_at="2026-09-07T02:00:00Z",
            task_origin="task-b",
        )
        self.assertEqual(first.metadata.content_digest, second.metadata.content_digest)
        self.assertEqual(first.api_version, "cptr.io/v1alpha1")
        self.assertEqual(first.state, ArtifactState.EPHEMERAL)

    def test_artifact_digest_changes_when_executable_spec_changes(self):
        base = dict(
            artifact_id="tool.example",
            version="1",
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            created_at="2026-09-07T01:00:00Z",
        )
        first = create_artifact(spec={"entrypoint": "a.py"}, **base)
        second = create_artifact(spec={"entrypoint": "b.py"}, **base)
        self.assertNotEqual(first.metadata.content_digest, second.metadata.content_digest)

    def test_capability_request_normalizes_and_refuses_blank_authority(self):
        request = CapabilityRequest(
            action=" Filesystem.Read ",
            resource=" repo:cptr/** ",
            constraints={"maxBytes": 1024},
        )
        self.assertEqual(request.action, "filesystem.read")
        self.assertEqual(request.resource, "repo:cptr/**")
        with self.assertRaises(ValueError):
            CapabilityRequest(action="", resource="repo:cptr/**")
        with self.assertRaises(ValueError):
            CapabilityRequest(action="filesystem.read", resource="")


if __name__ == "__main__":
    unittest.main()
