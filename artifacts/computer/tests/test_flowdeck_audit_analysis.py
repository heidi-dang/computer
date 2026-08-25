import unittest
from dataclasses import replace

from cptr.flowdeck.audit_analysis import (
    ANALYSIS_CONTRACT_VERSION,
    AuditAnalysisError,
    analyze_repository_facts,
    validate_analysis,
)
from cptr.flowdeck.audit_repository import AUDIT_CATEGORIES


def inspection(*, facts=None, read_only=True):
    return {
        "workspace": "/owned/repository",
        "read_only": read_only,
        "facts": facts
        if facts is not None
        else [
            {
                "category": category,
                "status": "verified",
                "summary": "evidence",
                "evidence": [f"{category}/evidence.txt"],
            }
            for category in AUDIT_CATEGORIES
        ],
    }


class AuditAnalysisTests(unittest.TestCase):
    def test_verified_facts_produce_complete_pass_checks_deterministically(self):
        first = analyze_repository_facts(inspection())
        second = analyze_repository_facts(inspection())
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(first.contract_version, ANALYSIS_CONTRACT_VERSION)
        self.assertEqual(first.overall_status, "unverified")
        self.assertEqual(len(first.checks), 16)
        self.assertTrue(all(check.status == "unverified" for check in first.checks))
        self.assertEqual(len(first.findings), 16)

    def test_unknown_never_promotes_to_pass_and_has_traceable_provenance(self):
        result = analyze_repository_facts(
            inspection(
                facts=[
                    {
                        "category": "tests",
                        "status": "unknown",
                        "summary": "not observed",
                        "evidence": [],
                    }
                ]
            )
        )
        self.assertEqual(result.overall_status, "unverified")
        self.assertTrue(any(check.status == "unverified" for check in result.checks))
        self.assertTrue(result.findings)
        self.assertTrue(
            all(
                finding.status != "passed"
                and finding.evidence
                and finding.impact
                and finding.scope
                and finding.remediation
                for finding in result.findings
            )
        )

    def test_evidence_tampering_and_unsupported_confidence_fail_closed(self):
        with self.assertRaises(AuditAnalysisError):
            analyze_repository_facts(inspection(read_only=False))
        with self.assertRaises(AuditAnalysisError):
            analyze_repository_facts(
                inspection(
                    facts=[
                        {
                            "category": "tests",
                            "status": "verified",
                            "summary": "forged",
                            "evidence": [],
                        }
                    ]
                )
            )
        analysis = analyze_repository_facts(inspection())
        bad_finding = replace(
            next(iter(analyze_repository_facts(inspection(facts=[])).findings)),
            confidence="certain",
        )
        with self.assertRaises(AuditAnalysisError):
            validate_analysis(replace(analysis, findings=(bad_finding,)))

    def test_escape_duplicate_and_unsupported_fact_payloads_are_rejected(self):
        escaped = inspection(
            facts=[
                {
                    "category": "tests",
                    "status": "verified",
                    "summary": "forged",
                    "evidence": ["../outside.txt"],
                }
            ]
        )
        with self.assertRaises(AuditAnalysisError):
            analyze_repository_facts(escaped)
        duplicate = inspection(
            facts=[
                {
                    "category": "tests",
                    "status": "unknown",
                    "summary": "one",
                    "evidence": [],
                },
                {
                    "category": "tests",
                    "status": "unknown",
                    "summary": "two",
                    "evidence": [],
                },
            ]
        )
        with self.assertRaises(AuditAnalysisError):
            analyze_repository_facts(duplicate)
