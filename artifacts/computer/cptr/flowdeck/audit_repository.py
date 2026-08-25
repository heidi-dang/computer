"""Bounded, read-only repository understanding for Heidi audits.

This module inventories repository evidence only. It never executes commands,
invokes providers, follows untrusted instructions, or mutates the workspace.
The native CPTR specialist remains responsible for model/tool observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUDIT_CATEGORIES = (
    "architecture",
    "module_boundaries",
    "dependencies",
    "configuration",
    "migrations",
    "tests",
    "runtime_entry_points",
    "data_flows",
    "authentication_authorization",
    "state_ownership",
    "provider_boundaries",
    "recent_changes",
)
_DEFAULT_IGNORED = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "build", "dist"}
)
_MAX_FILES = 500
_MAX_EVIDENCE_PER_FACT = 40
_MAX_SCOPE_KEYS = 32
_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "architecture": ("README", "replit.md", "pyproject.toml", "package.json", "src", "cptr"),
    "module_boundaries": ("src", "app", "cptr", "lib", "routers", "services", "components"),
    "dependencies": ("pyproject.toml", "requirements.txt", "package.json", "pnpm-lock.yaml", "package-lock.json"),
    "configuration": (".env.example", "config.toml", "vite.config", "tsconfig.json", "settings.py"),
    "migrations": ("migrations", "alembic", "drizzle", "prisma", "migration"),
    "tests": ("tests", "test", "pytest.ini", "vitest.config", "playwright.config"),
    "runtime_entry_points": ("main.py", "app.py", "server.py", "index.ts", "index.js", "package.json"),
    "data_flows": ("models", "schema", "schemas", "api", "routers", "routes", "services"),
    "authentication_authorization": ("auth", "session", "permissions", "middleware", "identity"),
    "state_ownership": ("stores", "state", "models", "durable", "database", "db"),
    "provider_boundaries": ("provider", "providers", "gateway", "model", "connection", "integrations"),
    "recent_changes": (".git", "CHANGELOG", "HISTORY", "release"),
}
_CATEGORY_ALIASES = {
    "deps": "dependencies",
    "config": "configuration",
    "runtime": "runtime_entry_points",
    "data": "data_flows",
    "auth": "authentication_authorization",
    "state": "state_ownership",
    "providers": "provider_boundaries",
    "changes": "recent_changes",
}


class AuditRepositoryPolicyError(ValueError):
    """Raised when an audit scope or repository cannot be safely inspected."""


@dataclass(frozen=True)
class RepositoryFact:
    category: str
    status: str
    summary: str
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class AuditInspection:
    workspace: str
    categories: tuple[str, ...]
    facts: tuple[RepositoryFact, ...]
    read_only: bool = True
    authoritative_source: str = "cptr-read-only-repository-inventory"

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "categories": list(self.categories),
            "facts": [fact.as_dict() for fact in self.facts],
            "read_only": self.read_only,
            "authoritative_source": self.authoritative_source,
        }


def _workspace_root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise AuditRepositoryPolicyError("owned audit workspace is not a directory")
    return root


def normalize_audit_scope(scope: dict[str, Any]) -> tuple[str, ...]:
    if not isinstance(scope, dict) or len(scope) > _MAX_SCOPE_KEYS:
        raise AuditRepositoryPolicyError("audit scope is missing or too large")
    requested = scope.get("categories", scope.get("areas", AUDIT_CATEGORIES))
    if requested is True:
        requested = AUDIT_CATEGORIES
    if not isinstance(requested, (list, tuple, set)):
        raise AuditRepositoryPolicyError("audit scope categories must be a list")
    categories = tuple(
        dict.fromkeys(
            _CATEGORY_ALIASES.get(str(item).strip(), str(item).strip())
            for item in requested
            if str(item).strip()
        )
    )
    if not categories:
        raise AuditRepositoryPolicyError("audit scope must select at least one category")
    unknown = sorted(set(categories) - set(AUDIT_CATEGORIES))
    if unknown:
        raise AuditRepositoryPolicyError(f"unsupported audit scope categories: {unknown}")
    return categories


def _relative_file_paths(root: Path) -> tuple[str, ...]:
    paths: list[str] = []
    for item in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix().casefold()):
        try:
            resolved = item.resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if not item.is_file() or any(part in _DEFAULT_IGNORED for part in item.relative_to(root).parts):
            continue
        paths.append(item.relative_to(root).as_posix())
        if len(paths) >= _MAX_FILES:
            break
    return tuple(paths)


def _matches(category: str, path: str) -> bool:
    lowered = path.casefold()
    return any(hint.casefold() in lowered for hint in _CATEGORY_HINTS[category])


def collect_repository_facts(workspace: str, scope: dict[str, Any]) -> AuditInspection:
    """Inventory bounded repository facts without shell or provider access."""
    root = _workspace_root(workspace)
    categories = normalize_audit_scope(scope)
    paths = _relative_file_paths(root)
    facts = []
    for category in categories:
        evidence = tuple(path for path in paths if _matches(category, path))[:_MAX_EVIDENCE_PER_FACT]
        if category == "recent_changes" and (root / ".git").exists():
            evidence = tuple(dict.fromkeys((".git", *evidence)))[:_MAX_EVIDENCE_PER_FACT]
            facts.append(
                RepositoryFact(
                    category=category,
                    status="unknown",
                    summary="Repository metadata exists, but recent history is not verified by this bounded inventory.",
                    evidence=evidence,
                )
            )
            continue
        if evidence:
            summary = f"Found {len(evidence)} in-workspace evidence paths for {category}."
            status = "verified"
        else:
            summary = f"No bounded in-workspace evidence path was found for {category}."
            status = "unknown"
        facts.append(
            RepositoryFact(
                category=category,
                status=status,
                summary=summary,
                evidence=evidence,
            )
        )
    return AuditInspection(workspace=str(root), categories=categories, facts=tuple(facts))
