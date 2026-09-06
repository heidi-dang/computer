"""Bounded deterministic convergence for one-click CPTR maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

ServiceTarget = Literal["all", "backend", "plugin", "extension", "mcp_transport"]
SystemStatus = Literal["STABLE", "DEGRADED", "ACTION_REQUIRED", "FAILED"]
RepairFn = Callable[[str], Awaitable[bool]]
SnapshotFn = Callable[[], Awaitable[dict]]
ActionRequiredFn = Callable[[], bool]
PassFn = Callable[[int, tuple[str, ...]], None]

SERVICE_ORDER = ("backend", "plugin", "extension", "mcp_transport")


@dataclass(frozen=True)
class StabilizeResult:
    system_status: SystemStatus
    final_band: str
    pass_count: int
    repaired_targets: tuple[str, ...]


def _service_band(snapshot: dict, service_id: str) -> str:
    if service_id == "all":
        return str(snapshot.get("aggregate") or "unhealthy")
    match = next(
        (
            item
            for item in snapshot.get("services", [])
            if isinstance(item, dict) and item.get("id") == service_id
        ),
        None,
    )
    return str(match.get("band") or snapshot.get("aggregate") or "unhealthy") if match else str(
        snapshot.get("aggregate") or "unhealthy"
    )


def _nonhealthy_targets(snapshot: dict, service_id: str) -> list[str]:
    if service_id != "all":
        return [] if _service_band(snapshot, service_id) == "healthy" else [service_id]
    bands = {
        str(item.get("id")): str(item.get("band") or "unhealthy")
        for item in snapshot.get("services", [])
        if isinstance(item, dict) and item.get("id") in SERVICE_ORDER
    }
    return [target for target in SERVICE_ORDER if bands.get(target, "unhealthy") != "healthy"]


class MaintenanceOrchestrator:
    """Run safe maintenance playbooks until health converges or budget is spent."""

    def __init__(self, *, max_passes: int = 2) -> None:
        self.max_passes = max(1, min(int(max_passes), 3))

    async def stabilize(
        self,
        *,
        service_id: ServiceTarget,
        repair_fn: RepairFn,
        snapshot_fn: SnapshotFn,
        action_required_fn: ActionRequiredFn | None = None,
        on_pass_fn: PassFn | None = None,
    ) -> StabilizeResult:
        targets = list(SERVICE_ORDER) if service_id == "all" else [service_id]
        repaired: list[str] = []
        final_band = "unhealthy"
        pass_count = 0

        for pass_number in range(1, self.max_passes + 1):
            pass_count = pass_number
            if on_pass_fn is not None:
                on_pass_fn(pass_number, tuple(targets))
            for target in targets:
                await repair_fn(target)
                repaired.append(target)

            snapshot = await snapshot_fn()
            final_band = _service_band(snapshot, service_id)
            if final_band == "healthy":
                return StabilizeResult(
                    system_status="STABLE",
                    final_band=final_band,
                    pass_count=pass_count,
                    repaired_targets=tuple(repaired),
                )

            if pass_number >= self.max_passes:
                break
            targets = _nonhealthy_targets(snapshot, service_id)
            if not targets:
                break

        if final_band == "moderate":
            action_required = bool(action_required_fn and action_required_fn())
            system_status: SystemStatus = "ACTION_REQUIRED" if action_required else "DEGRADED"
        elif final_band == "healthy":
            system_status = "STABLE"
        else:
            system_status = "FAILED"

        return StabilizeResult(
            system_status=system_status,
            final_band=final_band,
            pass_count=pass_count,
            repaired_targets=tuple(repaired),
        )
