"""Runtime profile discovery and fail-closed isolation selection."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum


class RuntimeClass(str, Enum):
    WASM = "wasm"
    GVISOR = "gvisor"
    MICROVM = "microvm"
    NAMESPACE_DEV = "namespace-dev"


class RuntimeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeSelection:
    runtime_class: RuntimeClass
    executable: str


@dataclass(frozen=True)
class RuntimeInventory:
    runsc: str | None
    wasmtime: str | None
    firecracker: str | None
    bubblewrap: str | None

    @classmethod
    def detect(cls) -> "RuntimeInventory":
        return cls(
            runsc=shutil.which("runsc"),
            wasmtime=shutil.which("wasmtime"),
            firecracker=shutil.which("firecracker"),
            bubblewrap=shutil.which("bwrap"),
        )

    @property
    def namespace_dev_available(self) -> bool:
        return self.bubblewrap is not None

    def available(self, runtime_class: RuntimeClass) -> bool:
        return {
            RuntimeClass.GVISOR: self.runsc is not None,
            RuntimeClass.WASM: self.wasmtime is not None,
            RuntimeClass.MICROVM: self.firecracker is not None,
            RuntimeClass.NAMESPACE_DEV: self.bubblewrap is not None,
        }[runtime_class]


class RuntimeBroker:
    def __init__(self, *, inventory: RuntimeInventory | None = None, production: bool = True) -> None:
        self.inventory = inventory or RuntimeInventory.detect()
        self.production = bool(production)

    def require(self, runtime_class: RuntimeClass) -> RuntimeSelection:
        if runtime_class is RuntimeClass.NAMESPACE_DEV and self.production:
            raise RuntimeUnavailable(
                "namespace-dev is not an approved production isolation boundary"
            )
        executable = {
            RuntimeClass.GVISOR: self.inventory.runsc,
            RuntimeClass.WASM: self.inventory.wasmtime,
            RuntimeClass.MICROVM: self.inventory.firecracker,
            RuntimeClass.NAMESPACE_DEV: self.inventory.bubblewrap,
        }[runtime_class]
        if not executable:
            raise RuntimeUnavailable(f"runtime profile {runtime_class.value} is unavailable")
        return RuntimeSelection(runtime_class=runtime_class, executable=executable)

    def production_snapshot(self) -> dict[str, object]:
        return {
            "production": self.production,
            "wasm": bool(self.inventory.wasmtime),
            "gvisor": bool(self.inventory.runsc),
            "microvm": bool(self.inventory.firecracker),
            "namespace_dev": bool(self.inventory.bubblewrap),
            "generated_native_ready": bool(self.inventory.runsc or self.inventory.firecracker),
        }
