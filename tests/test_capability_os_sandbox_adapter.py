import unittest
from types import SimpleNamespace

from cptr.services.capability_os.contracts import CapabilityRequest
from cptr.services.capability_os.runtime import RuntimeClass, RuntimeUnavailable
from cptr.services.capability_os.sandbox_adapter import BrokerToolBuilder, BrokerToolRunner


class _Client:
    def __init__(self, result):
        self.result = result
        self.requests = []
    async def invoke(self, request):
        self.requests.append(request)
        return self.result


class CapabilityOsSandboxAdapterTests(unittest.IsolatedAsyncioTestCase):
    def artifact(self, resources=None):
        return SimpleNamespace(
            content_digest="sha256:" + "1" * 64,
            source_digest="sha256:" + "2" * 64,
            task_origin="task-1",
            spec={"runtime": {"entrypoint": "main.py", "language": "python"},
                  "resources": resources or {"memoryMiB": 128, "cpuMillis": 1000,
                                               "diskMiB": 64, "pids": 8,
                                               "wallTimeMs": 5000, "maxOutputBytes": 65536}},
        )

    def lease(self, limits=None, action="runtime.build", task_id="task-1"):
        return SimpleNamespace(
            lease_id="lease-1", task_id=task_id, artifact_digest="sha256:" + "1" * 64,
            runtime_profile="gvisor",
            permissions=(CapabilityRequest(action, "artifact:sha256:" + "1" * 64),),
            resource_limits=limits or {"memoryMiB": 256, "cpuMillis": 2000,
                                       "diskMiB": 128, "pids": 16,
                                       "wallTimeMs": 10000, "maxOutputBytes": 131072},
            network={"outbound": "deny", "destinations": []},
            credentials={"logicalNames": ["github"]},
        )

    async def test_builder_projects_only_bounded_noncredential_authority(self):
        client = _Client({"artifactDigest": "sha256:" + "a" * 64,
                          "attestation": {"builder": "cptr-sandbox", "runtime": "gvisor"}})
        builder = BrokerToolBuilder(client=client)
        result = await builder(runtime_class=RuntimeClass.GVISOR, artifact=self.artifact(), lease=self.lease())
        self.assertEqual(result["artifact_digest"], "sha256:" + "a" * 64)
        request = client.requests[0]
        self.assertEqual(request.operation, "build")
        self.assertEqual(request.bundle_digest, "sha256:" + "2" * 64)
        self.assertEqual(request.profile, "python")
        self.assertEqual(request.resources["memoryMiB"], 128)
        self.assertEqual(request.network["outbound"], "deny")
        self.assertNotIn("credentials", request.to_dict())
        self.assertNotIn("github", str(request.to_dict()))

    async def test_builder_rejects_resource_expansion_before_broker(self):
        client = _Client({})
        builder = BrokerToolBuilder(client=client)
        with self.assertRaises(PermissionError):
            await builder(runtime_class=RuntimeClass.GVISOR,
                          artifact=self.artifact({"memoryMiB": 512}), lease=self.lease({"memoryMiB": 256}))
        self.assertEqual(client.requests, [])

    async def test_builder_reports_namespace_dev_as_runtime_unavailable_before_broker(self):
        client = _Client({})
        builder = BrokerToolBuilder(client=client)
        lease = self.lease()
        lease.runtime_profile = RuntimeClass.NAMESPACE_DEV.value
        with self.assertRaisesRegex(RuntimeUnavailable, "namespace-dev.*production"):
            await builder(
                runtime_class=RuntimeClass.NAMESPACE_DEV,
                artifact=self.artifact(),
                lease=lease,
            )
        self.assertEqual(client.requests, [])

    async def test_runner_projects_inputs_and_returns_only_broker_result(self):
        client = _Client({
            "artifactDigest": "sha256:" + "b" * 64,
            "output": {"answer": 42},
            "attestation": {"runtimeClass": "gvisor", "stdoutDigest": "sha256:" + "c" * 64},
        })
        runner = BrokerToolRunner(client=client)
        result = await runner(
            runtime_class=RuntimeClass.GVISOR,
            artifact=self.artifact(),
            lease=self.lease(action="runtime.run"),
            inputs={"x": 21},
            timeout_ms=3000,
        )
        self.assertEqual(result.output, {"answer": 42})
        self.assertTrue(result.verification_passed)
        request = client.requests[0]
        self.assertEqual(request.operation, "run")
        self.assertEqual(request.inputs, {"x": 21})
        self.assertEqual(request.timeout_ms, 3000)
        self.assertEqual(result.metadata["executionArtifactDigest"], "sha256:" + "b" * 64)

    async def test_runner_requires_runtime_run_lease_before_broker(self):
        client = _Client({})
        runner = BrokerToolRunner(client=client)
        with self.assertRaises(PermissionError):
            await runner(
                runtime_class=RuntimeClass.GVISOR,
                artifact=self.artifact(),
                lease=self.lease(action="runtime.build"),
                inputs={},
                timeout_ms=1000,
            )
        self.assertEqual(client.requests, [])

    async def test_runner_accepts_authorized_execution_task_distinct_from_forge_origin(self):
        client = _Client({
            "artifactDigest": "sha256:" + "b" * 64,
            "output": {"ok": True},
            "attestation": {"runtimeClass": "gvisor"},
        })
        runner = BrokerToolRunner(client=client)
        result = await runner(
            runtime_class=RuntimeClass.GVISOR,
            artifact=self.artifact(),
            lease=self.lease(action="runtime.run", task_id="task-2"),
            inputs={},
            timeout_ms=1000,
        )
        self.assertEqual(result.output, {"ok": True})
        self.assertEqual(client.requests[0].task_id, "task-2")

    async def test_builder_remains_bound_to_forge_origin_task(self):
        client = _Client({})
        builder = BrokerToolBuilder(client=client)
        with self.assertRaisesRegex(PermissionError, "build lease belongs"):
            await builder(
                runtime_class=RuntimeClass.GVISOR,
                artifact=self.artifact(),
                lease=self.lease(action="runtime.build", task_id="task-2"),
            )
        self.assertEqual(client.requests, [])


if __name__ == "__main__":
    unittest.main()
