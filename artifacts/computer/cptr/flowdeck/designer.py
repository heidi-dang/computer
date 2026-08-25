"""Deterministic, evidence-only Designer capability.

The designer deliberately does not call a model, browser, MCP, FDX, or a
transcript renderer.  It turns bounded files supplied by the authenticated
workspace into durable facts that a frontend can render.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cptr.flowdeck.contracts import DelegationRequest, FlowDeckMode
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.delegation import validate_delegation
from cptr.flowdeck.durable import DurableFlowDeck, RunStatus, StepStatus
from cptr.flowdeck.errors import DelegationPolicyError
from cptr.flowdeck.registry import get_agent

MAX_FILES = 100
MAX_BYTES = 1_000_000
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


class DesignerContractError(ValueError):
    """Malformed or unverifiable design input."""


@dataclass(frozen=True)
class DesignerRequest:
    request_key: str
    operation: str
    workspace: str
    user_id: str
    input: dict[str, Any]
    parent_chat_id: str = "designer"


# Public names used by API consumers.  The request remains intentionally
# small so it can also be embedded in a CoordinatorRequest objective.
DesignRequest = DesignerRequest


def _root(path: str) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise DesignerContractError("workspace is not a directory")
    return root


def _contained(root: Path, value: Any, *, image: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DesignerContractError("evidence path is required")
    try:
        path = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DesignerContractError("evidence path must be inside the workspace") from exc
    if not path.is_file():
        raise DesignerContractError("evidence file does not exist")
    if image and path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise DesignerContractError("screenshot must be a supported image")
    if image:
        header = path.read_bytes()[:12]
        valid = (
            (path.suffix.lower() == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
            or (path.suffix.lower() in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
            or (path.suffix.lower() == ".gif" and header[:6] in {b"GIF87a", b"GIF89a"})
            or (path.suffix.lower() == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
        )
        if not valid:
            raise DesignerContractError("screenshot content does not match its extension")
    return path


def _files(root: Path, requested: Any = None) -> list[Path]:
    paths = requested if requested is not None else [
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".css", ".scss", ".html", ".tsx", ".jsx"}
    ]
    if not isinstance(paths, list) or len(paths) > MAX_FILES:
        raise DesignerContractError("design evidence file list is invalid or too large")
    result = [_contained(root, item) for item in paths]
    total = 0
    for path in result:
        size = path.stat().st_size
        total += size
        if size > MAX_BYTES or total > MAX_BYTES:
            raise DesignerContractError("design evidence exceeds the bounded size limit")
    return sorted(set(result), key=str)


def _extract(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    paths = _files(root, data.get("files"))
    text = "\n".join(p.read_text(encoding="utf-8", errors="strict") for p in paths)
    colors = sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", text)))
    variables = sorted(set(re.findall(r"--([A-Za-z0-9_-]+)\s*:\s*([^;}\n]+)", text)))
    fonts = sorted(set(re.findall(r"font-family\s*:\s*([^;}\n]+)", text)))
    radii = sorted(set(re.findall(r"border-radius\s*:\s*([^;}\n]+)", text)))
    return {
        "files": [str(p.relative_to(root)) for p in paths],
        "tokens": {"colors": colors, "variables": variables, "fonts": fonts, "radii": radii},
        "media_queries": sorted(set(re.findall(r"@media[^{]+", text))),
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def _result(req: DesignerRequest, root: Path) -> dict[str, Any]:
    data = req.input
    op = req.operation
    if op in {"extract", "design_system", "design-system"}:
        return {"operation": "design_system", "design_system": _extract(root, data)}
    if op in {"variants", "render_variants", "render-variants"}:
        base = _extract(root, data)
        names = data.get("variants", ["balanced", "compact", "expressive"])
        if not isinstance(names, list) or not names or len(names) > 8 or not all(isinstance(x, str) and x for x in names):
            raise DesignerContractError("variants must be a non-empty list of names")
        return {"operation": "render_variants", "variants": [
            {"id": name, "deterministic_key": hashlib.sha256(
                json.dumps({"name": name, "source": base["content_sha256"]}, sort_keys=True).encode()
            ).hexdigest(), "design_system": base["tokens"]} for name in names
        ]}
    if op in {"apply", "mix", "select"}:
        choices = data.get("selection")
        if not isinstance(choices, dict) or not choices:
            raise DesignerContractError("selection must be a non-empty object")
        if data.get("mutate") is True:
            raise DesignerContractError("Designer is read-only; use the existing qualified mutation path")
        return {"operation": op, "selection": choices, "applied": False,
                "reason": "selection recorded; no workspace mutation performed"}
    if op in {"reconstruct", "screenshot_to_ui", "screenshot-to-ui"}:
        shot = _contained(root, data.get("screenshot"), image=True)
        raw = shot.read_bytes()
        if not raw:
            raise DesignerContractError("screenshot is empty or unverifiable")
        return {"operation": "screenshot_reconstruction", "screenshot": str(shot.relative_to(root)),
                "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                "ui_hints": {"width": data.get("width"), "height": data.get("height"),
                             "components": data.get("components", [])}}
    if op in {"responsive", "responsive_check", "responsive-check"}:
        viewports = data.get("viewports", {"mobile": 375, "tablet": 768, "desktop": 1440})
        if not isinstance(viewports, dict) or set(viewports) != {"mobile", "tablet", "desktop"}:
            raise DesignerContractError("viewports must contain mobile, tablet, and desktop")
        system = _extract(root, data)
        return {"operation": "responsive_check", "viewports": viewports,
                "checks": {name: {"status": "observed", "media_queries": len(system["media_queries"])}
                           for name in ("mobile", "tablet", "desktop")}}
    if op in {"compare", "screenshot_compare", "repair", "screenshot-repair"}:
        left = _contained(root, data.get("expected") or data.get("before"), image=True)
        right = _contained(root, data.get("actual") or data.get("after"), image=True)
        left_hash, right_hash = hashlib.sha256(left.read_bytes()).hexdigest(), hashlib.sha256(right.read_bytes()).hexdigest()
        return {"operation": "screenshot_comparison", "equal": left_hash == right_hash,
                "expected_sha256": left_hash, "actual_sha256": right_hash,
                "repair": {"applied": False, "reason": "repair requires an existing qualified mutation path"}}
    raise DesignerContractError("unknown designer operation")


async def run_designer(request: DesignerRequest, *, store: DurableFlowDeck) -> dict[str, Any]:
    """Execute deterministic design analysis under the durable CPTR lifecycle."""
    config = FlowDeckConfig.from_env()
    if not config.enabled or config.mode not in {FlowDeckMode.READ_ONLY, FlowDeckMode.CONTROLLED} or config.governance != "strict":
        raise DesignerContractError("designer requires enabled strict read-only or controlled FlowDeck")
    try:
        validate_delegation(DelegationRequest("heidi", "designer", 1, get_agent("designer").capabilities), config)
    except DelegationPolicyError as exc:
        raise DesignerContractError(str(exc)) from exc
    root = _root(request.workspace)
    # Validate and compute all evidence before reserving durable state. A
    # malformed screenshot/path must have no durable side effects.
    output = _result(request, root)
    run, _ = await store.create_run(request_key=request.request_key, owner=request.user_id, workspace=str(root), step_name="designer")
    if run.status == RunStatus.SUCCEEDED.value:
        return {"run_id": run.id, "status": "succeeded", "reused": True}
    if run.status == RunStatus.PENDING.value:
        await store.start_run(run.id)
    step = await store.get_step(run.id)
    if step.status == StepStatus.PENDING.value:
        await store.start_step(step.id)
    await store.record_event(run.id, "DESIGN_RESULT_CREATED", {"operation": output["operation"], "result": output})
    await store.finish_step(step.id, status=StepStatus.SUCCEEDED)
    await store.complete_run(run.id, status=RunStatus.SUCCEEDED)
    return {"run_id": run.id, "status": "succeeded", "reused": False, "result": output}


class DesignerService:
    """Thin service facade for callers that already own a durable store."""

    def __init__(self, store: DurableFlowDeck):
        self.store = store

    async def execute(self, request: DesignerRequest) -> dict[str, Any]:
        return await run_designer(request, store=self.store)


def design_contract(operation: str, input: dict[str, Any]) -> dict[str, Any]:
    """Return the validated, deterministic contract without executing it."""
    if not isinstance(operation, str) or not operation.strip() or not isinstance(input, dict):
        raise DesignerContractError("operation and input are required")
    return {"operation": operation.strip().lower(), "input": dict(input), "read_only": True}