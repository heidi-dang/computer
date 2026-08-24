"""Bounded dependency-aware Build scheduling.

This module only validates and schedules orchestration metadata. The callback
is the integration point for CPTR's authenticated native execution path; no
provider, tool, shell, or model is invoked here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable


class BuildNodeStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CONFLICT = "CONFLICT"


class ParallelBuildPlanError(ValueError):
    """Raised when a Build DAG or overlap policy is unsafe."""


@dataclass(frozen=True)
class ParallelBuildNode:
    key: str
    dependencies: tuple[str, ...] = ()
    mutation: bool = False
    workspace: str = ""
    worktree: str | None = None
    common_base: str | None = None
    overlap_paths: tuple[str, ...] = ()
    role: str = ""
    branch: str | None = None


@dataclass(frozen=True)
class ParallelBuildPlan:
    nodes: tuple[ParallelBuildNode, ...]
    max_concurrency: int = 3


@dataclass
class ParallelBuildState:
    status: dict[str, BuildNodeStatus] = field(default_factory=dict)
    results: dict[str, object] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def validate_parallel_build_plan(plan: ParallelBuildPlan) -> None:
    if not plan.nodes:
        raise ParallelBuildPlanError("parallel Build plan must contain a node")
    if not 1 <= plan.max_concurrency <= 8:
        raise ParallelBuildPlanError("parallel Build concurrency must be between 1 and 8")
    keys = [node.key for node in plan.nodes]
    if any(not key.strip() for key in keys) or len(set(keys)) != len(keys):
        raise ParallelBuildPlanError("Build node keys must be unique and non-empty")
    known = set(keys)
    for node in plan.nodes:
        if node.key in node.dependencies:
            raise ParallelBuildPlanError(f"Build node depends on itself: {node.key}")
        if not set(node.dependencies) <= known:
            missing = sorted(set(node.dependencies) - known)
            raise ParallelBuildPlanError(f"Build node {node.key} has unknown dependencies: {missing}")
        if node.mutation and not node.worktree:
            raise ParallelBuildPlanError(f"mutating Build node requires an isolated worktree: {node.key}")
        if node.mutation and not node.common_base:
            raise ParallelBuildPlanError(f"mutating Build node requires a common base: {node.key}")

    visiting: set[str] = set()
    visited: set[str] = set()
    by_key = {node.key: node for node in plan.nodes}

    def visit(key: str) -> None:
        if key in visiting:
            raise ParallelBuildPlanError("Build plan contains a dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key].dependencies:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)


def ready_parallel_nodes(
    plan: ParallelBuildPlan, state: ParallelBuildState
) -> tuple[ParallelBuildNode, ...]:
    """Return deterministic nodes whose dependencies have all succeeded."""
    return tuple(
        node
        for node in plan.nodes
        if state.status.get(node.key, BuildNodeStatus.PENDING) == BuildNodeStatus.PENDING
        and all(state.status.get(dep) == BuildNodeStatus.SUCCEEDED for dep in node.dependencies)
    )


def overlapping_parallel_nodes(plan: ParallelBuildPlan) -> tuple[tuple[str, str], ...]:
    """Report overlapping mutation paths before any integration is attempted."""
    mutations = [node for node in plan.nodes if node.mutation]
    overlaps: list[tuple[str, str]] = []
    for index, left in enumerate(mutations):
        left_paths = set(left.overlap_paths)
        for right in mutations[index + 1 :]:
            if left_paths & set(right.overlap_paths):
                overlaps.append((left.key, right.key))
    return tuple(overlaps)


async def run_parallel_build_batch(
    nodes: tuple[ParallelBuildNode, ...],
    *,
    state: ParallelBuildState,
    execute: Callable[[ParallelBuildNode], Awaitable[object]],
    cancellation: asyncio.Event | None = None,
    max_concurrency: int = 3,
) -> ParallelBuildState:
    """Run one dependency-ready batch through a caller-owned CPTR callback."""
    if not nodes:
        return state
    semaphore = asyncio.Semaphore(max(1, min(max_concurrency, 8)))

    async def run(node: ParallelBuildNode) -> None:
        if cancellation and cancellation.is_set():
            state.status[node.key] = BuildNodeStatus.CANCELLED
            return
        state.status[node.key] = BuildNodeStatus.RUNNING
        try:
            async with semaphore:
                if cancellation and cancellation.is_set():
                    state.status[node.key] = BuildNodeStatus.CANCELLED
                    return
                state.results[node.key] = await execute(node)
        except asyncio.CancelledError:
            state.status[node.key] = BuildNodeStatus.CANCELLED
            raise
        except Exception as exc:
            state.status[node.key] = BuildNodeStatus.FAILED
            state.errors[node.key] = str(exc)[:500]
            return
        state.status[node.key] = BuildNodeStatus.SUCCEEDED

    await asyncio.gather(*(run(node) for node in nodes))
    return state