import hashlib
import unittest

from cptr.services.capability_os.contracts import digest_payload
from cptr.services.capability_os.provenance import (
    CPTR_BUILD_TYPE,
    IN_TOTO_STATEMENT_TYPE,
    SLSA_PREDICATE_TYPE,
    SPDX_VERSION,
    build_slsa_v1_statement,
    build_spdx_23_document,
    supply_chain_reference,
    validate_supply_chain_references,
)


class CapabilityOsProvenanceTests(unittest.TestCase):
    def test_spdx_23_document_binds_every_source_file_and_source_digest(self):
        source_digest = "sha256:" + "a" * 64
        files = {
            "main.py": "print('ok')\n",
            "lib/helper.py": "VALUE = 1\n",
        }
        document = build_spdx_23_document(
            tool_id="tool.example",
            version="2",
            source_digest=source_digest,
            files=files,
            created_at="2026-09-08T06:00:00Z",
        )

        self.assertEqual(document["spdxVersion"], SPDX_VERSION)
        self.assertEqual(document["SPDXID"], "SPDXRef-DOCUMENT")
        self.assertEqual(document["documentNamespace"], "urn:cptr:spdx:" + "a" * 64)
        package = document["packages"][0]
        self.assertEqual(package["name"], "tool.example")
        self.assertEqual(package["externalRefs"][0]["referenceLocator"], source_digest)
        by_name = {row["fileName"]: row for row in document["files"]}
        self.assertEqual(set(by_name), set(files))
        self.assertEqual(
            by_name["main.py"]["checksums"][0]["checksumValue"],
            hashlib.sha256(files["main.py"].encode()).hexdigest(),
        )
        relationships = document["relationships"]
        self.assertTrue(any(row["relationshipType"] == "DESCRIBES" for row in relationships))
        self.assertEqual(
            sum(row["relationshipType"] == "CONTAINS" for row in relationships),
            len(files),
        )

    def test_slsa_v1_statement_binds_subject_source_runtime_and_attestation_digest(self):
        source_digest = "sha256:" + "b" * 64
        artifact_digest = "sha256:" + "c" * 64
        sbom_digest = "sha256:" + "d" * 64
        attestation = {"runtimeClass": "gvisor", "rootfsDigest": "sha256:" + "e" * 64}
        statement = build_slsa_v1_statement(
            tool_id="tool.example",
            version="2",
            build_id="build-1",
            source_digest=source_digest,
            artifact_digest=artifact_digest,
            runtime_class="gvisor",
            entrypoint="main.py",
            requested_capabilities=[{"action": "filesystem.read", "resource": "repo:cptr/**"}],
            broker_attestation=attestation,
            sbom_digest=sbom_digest,
        )

        self.assertEqual(statement["_type"], IN_TOTO_STATEMENT_TYPE)
        self.assertEqual(statement["predicateType"], SLSA_PREDICATE_TYPE)
        self.assertEqual(statement["subject"][0]["digest"]["sha256"], "c" * 64)
        build_definition = statement["predicate"]["buildDefinition"]
        self.assertEqual(build_definition["buildType"], CPTR_BUILD_TYPE)
        self.assertEqual(build_definition["internalParameters"]["sourceDigest"], source_digest)
        self.assertEqual(build_definition["externalParameters"]["runtimeClass"], "gvisor")
        byproducts = statement["predicate"]["runDetails"]["byproducts"]
        attestation_ref = next(row for row in byproducts if row["name"] == "broker-attestation")
        self.assertEqual(
            attestation_ref["digest"]["sha256"],
            digest_payload(attestation).split(":", 1)[1],
        )
        sbom_ref = next(row for row in byproducts if row["name"] == "spdx-sbom")
        self.assertEqual(sbom_ref["digest"]["sha256"], "d" * 64)

    def test_supply_chain_reference_validation_fails_closed(self):
        refs = {
            "sbom": supply_chain_reference(kind="sbom", digest="sha256:" + "1" * 64),
            "provenance": supply_chain_reference(
                kind="provenance", digest="sha256:" + "2" * 64
            ),
        }
        self.assertEqual(validate_supply_chain_references(refs), refs)

        with self.assertRaisesRegex(ValueError, "SPDX SBOM and SLSA provenance"):
            validate_supply_chain_references({"sbom": refs["sbom"]})
        tampered = {"sbom": dict(refs["sbom"]), "provenance": dict(refs["provenance"])}
        tampered["provenance"]["predicateType"] = "caller-defined"
        with self.assertRaisesRegex(ValueError, "invalid provenance"):
            validate_supply_chain_references(tampered)


if __name__ == "__main__":
    unittest.main()
