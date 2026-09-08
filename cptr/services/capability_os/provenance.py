"""Standard supply-chain documents for generated Capability OS tools.

The documents in this module are descriptive evidence only. They never grant
execution authority. Tool Forge stores them content-addressed and promotion
checks require their trusted server-produced references.
"""

from __future__ import annotations

import hashlib
from typing import Any

from cptr.services.capability_os.contracts import digest_payload


SPDX_VERSION = "SPDX-2.3"
SPDX_MEDIA_TYPE = "application/spdx+json"
SLSA_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
SLSA_MEDIA_TYPE = "application/vnd.in-toto+json"
CPTR_BUILD_TYPE = "https://cptr.dev/capability-os/tool-forge/v1"
CPTR_BUILDER_ID = "https://cptr.dev/capability-os/tool-forge"


def _sha256_hex(value: str) -> str:
    raw = str(value).strip()
    if not raw.startswith("sha256:") or len(raw) != 71:
        raise ValueError("expected a sha256 digest")
    return raw.split(":", 1)[1]


def _bounded_text(value: Any, *, field: str, max_length: int = 1000) -> str:
    text = str(value).strip()
    if not text or len(text) > max_length:
        raise ValueError(f"{field} must be bounded and non-empty")
    return text


def build_spdx_23_document(
    *,
    tool_id: str,
    version: str,
    source_digest: str,
    files: dict[str, str],
    created_at: str,
) -> dict[str, Any]:
    tool_id = _bounded_text(tool_id, field="tool_id", max_length=300)
    version = _bounded_text(version, field="version", max_length=200)
    created_at = _bounded_text(created_at, field="created_at", max_length=80)
    source_hex = _sha256_hex(source_digest)
    if not files:
        raise ValueError("SPDX generation requires source files")

    file_rows: list[dict[str, Any]] = []
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": "SPDXRef-Package",
        }
    ]
    for index, path in enumerate(sorted(files), start=1):
        content = files[path]
        if not isinstance(content, str):
            raise TypeError("SPDX source file content must be text")
        file_id = f"SPDXRef-File-{index}"
        file_rows.append(
            {
                "fileName": path,
                "SPDXID": file_id,
                "checksums": [
                    {
                        "algorithm": "SHA256",
                        "checksumValue": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    }
                ],
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {
                "spdxElementId": "SPDXRef-Package",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": file_id,
            }
        )

    return {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"cptr-tool-{tool_id}-{version}",
        "documentNamespace": f"urn:cptr:spdx:{source_hex}",
        "creationInfo": {
            "created": created_at,
            "creators": ["Tool: CPTR Capability OS Tool Forge"],
        },
        "packages": [
            {
                "name": tool_id,
                "SPDXID": "SPDXRef-Package",
                "versionInfo": version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": True,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "externalRefs": [
                    {
                        "referenceCategory": "OTHER",
                        "referenceType": "cptr-source-digest",
                        "referenceLocator": source_digest,
                    }
                ],
            }
        ],
        "files": file_rows,
        "relationships": relationships,
    }


def build_slsa_v1_statement(
    *,
    tool_id: str,
    version: str,
    build_id: str,
    source_digest: str,
    artifact_digest: str,
    runtime_class: str,
    entrypoint: str,
    requested_capabilities: list[dict[str, Any]],
    broker_attestation: dict[str, Any],
    sbom_digest: str,
) -> dict[str, Any]:
    tool_id = _bounded_text(tool_id, field="tool_id", max_length=300)
    version = _bounded_text(version, field="version", max_length=200)
    build_id = _bounded_text(build_id, field="build_id", max_length=200)
    runtime_class = _bounded_text(runtime_class, field="runtime_class", max_length=80)
    entrypoint = _bounded_text(entrypoint, field="entrypoint", max_length=1000)
    source_hex = _sha256_hex(source_digest)
    artifact_hex = _sha256_hex(artifact_digest)
    sbom_hex = _sha256_hex(sbom_digest)
    if not isinstance(broker_attestation, dict):
        raise TypeError("broker attestation must be an object")
    attestation_digest = digest_payload(broker_attestation)

    return {
        "_type": IN_TOTO_STATEMENT_TYPE,
        "subject": [
            {
                "name": f"{tool_id}@{version}",
                "digest": {"sha256": artifact_hex},
            }
        ],
        "predicateType": SLSA_PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": CPTR_BUILD_TYPE,
                "externalParameters": {
                    "runtimeClass": runtime_class,
                    "entrypoint": entrypoint,
                    "requestedCapabilities": list(requested_capabilities),
                },
                "internalParameters": {
                    "sourceDigest": source_digest,
                },
                "resolvedDependencies": [
                    {
                        "uri": f"urn:cptr:source:sha256:{source_hex}",
                        "digest": {"sha256": source_hex},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": CPTR_BUILDER_ID},
                "metadata": {"invocationId": build_id},
                "byproducts": [
                    {
                        "name": "broker-attestation",
                        "digest": {"sha256": _sha256_hex(attestation_digest)},
                    },
                    {
                        "name": "spdx-sbom",
                        "digest": {"sha256": sbom_hex},
                    },
                ],
            },
        },
    }


def supply_chain_reference(*, kind: str, digest: str) -> dict[str, str]:
    digest = f"sha256:{_sha256_hex(digest)}"
    if kind == "sbom":
        return {
            "format": "spdx-json",
            "specVersion": SPDX_VERSION,
            "mediaType": SPDX_MEDIA_TYPE,
            "digest": digest,
        }
    if kind == "provenance":
        return {
            "format": "in-toto-statement",
            "predicateType": SLSA_PREDICATE_TYPE,
            "mediaType": SLSA_MEDIA_TYPE,
            "digest": digest,
        }
    raise ValueError("unsupported supply-chain reference kind")


def validate_supply_chain_references(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError("build evidence requires supplyChain references")
    if set(value) != {"sbom", "provenance"}:
        raise ValueError("build evidence requires SPDX SBOM and SLSA provenance")
    sbom = dict(value["sbom"]) if isinstance(value.get("sbom"), dict) else {}
    provenance = dict(value["provenance"]) if isinstance(value.get("provenance"), dict) else {}
    expected = {
        "sbom": ("spdx-json", SPDX_VERSION, SPDX_MEDIA_TYPE),
        "provenance": ("in-toto-statement", SLSA_PREDICATE_TYPE, SLSA_MEDIA_TYPE),
    }
    for name, ref in (("sbom", sbom), ("provenance", provenance)):
        if name == "sbom":
            actual = (ref.get("format"), ref.get("specVersion"), ref.get("mediaType"))
        else:
            actual = (ref.get("format"), ref.get("predicateType"), ref.get("mediaType"))
        if actual != expected[name]:
            raise ValueError(f"invalid {name} supply-chain reference")
        _sha256_hex(str(ref.get("digest") or ""))
    return {"sbom": sbom, "provenance": provenance}
