import asyncio
import json
import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastmcp import Context, FastMCP

load_dotenv()
BACKEND = os.environ["CPTR_BACKEND_URL"].rstrip("/")
TOKEN = os.environ["CPTR_API_TOKEN"]
BASE = BACKEND + "/api/control/v1/capability-os"
HEADERS = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}

mcp = FastMCP(
    name="os-mcp",
    instructions=(
        "Capability OS MCP surface: six core operations plus native client-model parallel fan-out. "
        "Fresh clients call cptr_forge(operation='bootstrap'); task_id is optional for bootstrap and "
        "ignored if a stale client schema supplies it. Then use the returned task.taskId for "
        "inspect/resolve/forge/execute/acquire/reflect calls. spawn_multiple_subagents requests "
        "multiple isolated client-model continuations concurrently when the MCP client advertises "
        "sampling with tool support."
    ),
    version="2.3.0",
)


def _json_object(raw: str, label: str) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    try:
        value = json.loads(raw)
    except Exception as exc:
        return None, {"error": f"Invalid {label} JSON: {exc}"}
    if not isinstance(value, dict):
        return None, {"error": f"{label} JSON must decode to an object"}
    return value, None


def _request(method: str, path: str, *, timeout: float, **kwargs: Any) -> dict:
    try:
        response = httpx.request(method, BASE + path, headers=HEADERS, timeout=timeout, **kwargs)
        try:
            return response.json()
        except Exception:
            return {
                "error": f"Capability OS returned HTTP {response.status_code} with a non-JSON response"
            }
    except Exception as exc:
        return {"error": str(exc)}


async def _async_request(method: str, path: str, *, timeout: float, **kwargs: Any) -> dict:
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=timeout) as client:
            response = await client.request(method, BASE + path, **kwargs)
        try:
            return response.json()
        except Exception:
            return {
                "error": f"Capability OS returned HTTP {response.status_code} with a non-JSON response"
            }
    except Exception as exc:
        return {"error": str(exc)}


def _required_task_id(task_id: str) -> tuple[str | None, dict[str, str] | None]:
    value = task_id.strip()
    if not value:
        return None, {
            "error": (
                "task_id must not be blank; fresh clients should call "
                "cptr_forge(operation='bootstrap') first"
            )
        }
    return value, None


def _effect_requests(values: Optional[list]) -> tuple[list[dict[str, Any]] | None, dict[str, str] | None]:
    result: list[dict[str, Any]] = []
    for item in values or []:
        if isinstance(item, str):
            action = item.strip()
            if not action:
                return None, {"error": "effect strings must not be blank"}
            result.append({"action": action, "resource": "*"})
            continue
        if isinstance(item, dict):
            action = str(item.get("action") or "").strip()
            resource = str(item.get("resource") or "").strip()
            if not action or not resource:
                return None, {"error": "effect objects require non-empty action and resource"}
            normalized = {"action": action, "resource": resource}
            constraints = item.get("constraints")
            if isinstance(constraints, dict) and constraints:
                normalized["constraints"] = dict(constraints)
            result.append(normalized)
            continue
        return None, {"error": "effects must be strings or {action, resource} objects"}
    return result, None


def _capability_digest(task_id: str, capability_id: str) -> tuple[str | None, dict | None]:
    reference = capability_id.strip()
    if not reference:
        return None, {"error": "capability_id must not be blank"}
    if reference.startswith("sha256:"):
        return reference, None

    inspected = _request("GET", "/inspect", timeout=15, params={"task_id": task_id, "limit": 100})
    if "detail" in inspected or "error" in inspected:
        return None, inspected

    matches: list[dict] = []
    for artifact in inspected.get("artifacts") or []:
        if not isinstance(artifact, dict) or artifact.get("kind") != "Capability":
            continue
        metadata = artifact.get("metadata") or {}
        artifact_id = str(metadata.get("id") or "")
        version = str(metadata.get("version") or "")
        if reference in {artifact_id, f"{artifact_id}@{version}"}:
            matches.append(artifact)

    if not matches:
        return None, {"error": "Capability OS capability_id was not found in the task-visible artifact set"}
    if len(matches) != 1:
        return None, {
            "error": "capability_id is ambiguous; use capability_id@version or a sha256 content digest"
        }
    digest = str((matches[0].get("metadata") or {}).get("contentDigest") or "").strip()
    if not digest.startswith("sha256:"):
        return None, {"error": "resolved Capability artifact is missing a valid content digest"}
    return digest, None


async def _async_capability_digest(task_id: str, capability_id: str) -> tuple[str | None, dict | None]:
    reference = capability_id.strip()
    if not reference:
        return None, {"error": "capability_id must not be blank"}
    if reference.startswith("sha256:"):
        return reference, None
    inspected = await _async_request(
        "GET", "/inspect", timeout=15, params={"task_id": task_id, "limit": 100}
    )
    if "detail" in inspected or "error" in inspected:
        return None, inspected
    matches: list[dict] = []
    for artifact in inspected.get("artifacts") or []:
        if not isinstance(artifact, dict) or artifact.get("kind") != "Capability":
            continue
        metadata = artifact.get("metadata") or {}
        artifact_id = str(metadata.get("id") or "")
        version = str(metadata.get("version") or "")
        if reference in {artifact_id, f"{artifact_id}@{version}"}:
            matches.append(artifact)
    if not matches:
        return None, {"error": "Capability OS capability_id was not found in the task-visible artifact set"}
    if len(matches) != 1:
        return None, {"error": "capability_id is ambiguous; use capability_id@version or a sha256 content digest"}
    digest = str((matches[0].get("metadata") or {}).get("contentDigest") or "").strip()
    if not digest.startswith("sha256:"):
        return None, {"error": "resolved Capability artifact is missing a valid content digest"}
    return digest, None


def _bound_sampling_tools(task_id: str) -> list:
    async def cptr_inspect(limit: int = 20) -> dict:
        """Inspect this subagent's isolated Capability OS task state."""
        return await _async_request(
            "GET",
            "/inspect",
            timeout=15,
            params={"task_id": task_id, "limit": max(1, min(int(limit), 100))},
        )

    async def cptr_resolve(
        required_effects: list,
        optional_effects: Optional[list] = None,
        forbidden_effects: Optional[list] = None,
    ) -> dict:
        """Resolve the best task-visible capabilities for required effects."""
        required, error = _effect_requests(required_effects)
        if error:
            return error
        optional, error = _effect_requests(optional_effects)
        if error:
            return error
        forbidden, error = _effect_requests(forbidden_effects)
        if error:
            return error
        if not required:
            return {"error": "required_effects must contain at least one effect"}
        return await _async_request(
            "POST",
            "/resolve",
            timeout=15,
            json={
                "task_id": task_id,
                "required": required,
                "optional": optional or [],
                "forbidden": forbidden or [],
            },
        )

    async def cptr_forge(operation: str, payload_json: str = "{}") -> dict:
        """Forge or evolve a tool inside this subagent's Capability OS task."""
        payload, error = _json_object(payload_json, "payload")
        if error:
            return error
        normalized_operation = operation.strip().lower()
        if normalized_operation == "bootstrap":
            return {"error": "parallel subagents already have an isolated Capability OS task"}
        return await _async_request(
            "POST",
            "/forge",
            timeout=30,
            json={"task_id": task_id, "operation": normalized_operation, "payload": payload},
        )

    async def cptr_execute(capability_id: str, inputs_json: str = "{}") -> dict:
        """Execute a task-visible Capability inside this isolated subagent task."""
        inputs, error = _json_object(inputs_json, "inputs")
        if error:
            return error
        digest, error = await _async_capability_digest(task_id, capability_id)
        if error:
            return error
        return await _async_request(
            "POST",
            "/execute",
            timeout=60,
            json={
                "task_id": task_id,
                "capability_digest": digest,
                "lease_id": None,
                "spec": {},
                "inputs": inputs,
                "approval_id": None,
            },
        )

    async def cptr_acquire(operation: str, payload_json: str = "{}") -> dict:
        """Discover, qualify, mount, invoke, or release MCP capabilities for this task."""
        payload, error = _json_object(payload_json, "payload")
        if error:
            return error
        return await _async_request(
            "POST",
            "/acquire",
            timeout=30,
            json={"task_id": task_id, "operation": operation, "payload": payload},
        )

    async def cptr_reflect(
        observation_type: str,
        description: str,
        severity: str = "info",
        context_json: str = "{}",
    ) -> dict:
        """Record evidence-backed observations from this isolated subagent task."""
        context, error = _json_object(context_json, "context")
        if error:
            return error
        observation_type = observation_type.strip().lower()
        if observation_type not in {"bug", "friction", "gap", "hypothesis", "success"}:
            return {"error": "invalid observation_type"}
        severity = severity.strip().lower()
        if severity not in {"info", "low", "medium", "high", "critical"}:
            return {"error": "invalid severity"}
        return await _async_request(
            "POST",
            "/reflect",
            timeout=15,
            json={
                "task_id": task_id,
                "kind": f"capability.observation.{observation_type}",
                "claims": {
                    "observationType": observation_type,
                    "description": description,
                    "severity": severity,
                    "context": context,
                },
            },
        )

    return [
        cptr_inspect,
        cptr_resolve,
        cptr_forge,
        cptr_execute,
        cptr_acquire,
        cptr_reflect,
    ]


@mcp.tool(description="Inspect Capability OS state for a task: capabilities, evidence, leases, mounts, and runtime state.")
def cptr_inspect(task_id: str, limit: int = 20) -> dict:
    task_id, error = _required_task_id(task_id)
    if error:
        return error
    return _request(
        "GET",
        "/inspect",
        timeout=15,
        params={"task_id": task_id, "limit": max(1, min(int(limit), 100))},
    )


@mcp.tool(
    description=(
        "Resolve capabilities by effect. Effects may be action strings such as filesystem.read "
        "or explicit {action, resource, constraints?} objects. Action strings resolve across any resource."
    )
)
def cptr_resolve(
    task_id: str,
    required_effects: list,
    optional_effects: Optional[list] = None,
    forbidden_effects: Optional[list] = None,
) -> dict:
    task_id, error = _required_task_id(task_id)
    if error:
        return error
    required, error = _effect_requests(required_effects)
    if error:
        return error
    optional, error = _effect_requests(optional_effects)
    if error:
        return error
    forbidden, error = _effect_requests(forbidden_effects)
    if error:
        return error
    if not required:
        return {"error": "required_effects must contain at least one effect"}
    return _request(
        "POST",
        "/resolve",
        timeout=15,
        json={
            "task_id": task_id,
            "required": required,
            "optional": optional or [],
            "forbidden": forbidden or [],
        },
    )


@mcp.tool(
    description=(
        "Forge tools with Capability OS. Bootstrap does not require task_id; if a stale client schema "
        "still supplies one, it is ignored and the server creates a fresh owner-bound task context. "
        "Other operations require task_id. Tool operations include create/modify/fork/build/run/persist/"
        "destroy plus supported skill operations."
    )
)
def cptr_forge(operation: str, task_id: Optional[str] = None, payload_json: str = "{}") -> dict:
    payload, error = _json_object(payload_json, "payload")
    if error:
        return error
    normalized_operation = operation.strip().lower()
    if normalized_operation == "bootstrap":
        if payload:
            return {"error": "bootstrap accepts no payload"}
        return _request(
            "POST",
            "/forge",
            timeout=15,
            json={"operation": "bootstrap", "payload": {}},
        )
    if task_id is None:
        return {
            "error": (
                "task_id is required for forge operations other than bootstrap; "
                "fresh clients should call cptr_forge(operation='bootstrap') first"
            )
        }
    task_id, error = _required_task_id(task_id)
    if error:
        return error
    return _request(
        "POST",
        "/forge",
        timeout=30,
        json={"task_id": task_id, "operation": normalized_operation, "payload": payload},
    )


@mcp.tool(
    description=(
        "Execute a task-visible Capability by unique capability_id, capability_id@version, or sha256 "
        "content digest. The adapter resolves IDs to immutable content digests before execution."
    )
)
def cptr_execute(task_id: str, capability_id: str, inputs_json: str = "{}") -> dict:
    task_id, error = _required_task_id(task_id)
    if error:
        return error
    inputs, error = _json_object(inputs_json, "inputs")
    if error:
        return error
    digest, error = _capability_digest(task_id, capability_id)
    if error:
        return error
    return _request(
        "POST",
        "/execute",
        timeout=60,
        json={
            "task_id": task_id,
            "capability_digest": digest,
            "lease_id": None,
            "spec": {},
            "inputs": inputs,
            "approval_id": None,
        },
    )


@mcp.tool(
    description=(
        "Acquire external MCPs through Capability OS. operation supports discover/qualify/mount/"
        "release and configured OAuth suboperations. payload_json must be a JSON object."
    )
)
def cptr_acquire(task_id: str, operation: str, payload_json: str = "{}") -> dict:
    task_id, error = _required_task_id(task_id)
    if error:
        return error
    payload, error = _json_object(payload_json, "payload")
    if error:
        return error
    return _request(
        "POST",
        "/acquire",
        timeout=30,
        json={"task_id": task_id, "operation": operation, "payload": payload},
    )


@mcp.tool(
    description=(
        "Record a Capability OS observation. observation_type: bug/friction/gap/hypothesis/success; "
        "severity: info/low/medium/high/critical."
    )
)
def cptr_reflect(
    task_id: str,
    observation_type: str,
    description: str,
    severity: str = "info",
    context_json: str = "{}",
) -> dict:
    task_id, task_error = _required_task_id(task_id)
    if task_error:
        return task_error
    context, error = _json_object(context_json, "context")
    if error:
        return error
    observation_type = observation_type.strip().lower()
    if observation_type not in {"bug", "friction", "gap", "hypothesis", "success"}:
        return {"error": "invalid observation_type"}
    severity = severity.strip().lower()
    if severity not in {"info", "low", "medium", "high", "critical"}:
        return {"error": "invalid severity"}
    return _request(
        "POST",
        "/reflect",
        timeout=15,
        json={
            "task_id": task_id,
            "kind": f"capability.observation.{observation_type}",
            "claims": {
                "observationType": observation_type,
                "description": description,
                "severity": severity,
                "context": context,
            },
        },
    )


@mcp.tool(
    description=(
        "Atomically prepare 2-10 isolated Capability OS child contexts, then issue all client-model "
        "sampling requests concurrently through one start barrier. Each sampled continuation gets only "
        "the six Capability OS operations bound to its own child task. There is no delegated-agent or "
        "external-model fallback; the connected MCP client must support sampling with tools."
    )
)
async def spawn_multiple_subagents(
    task_id: str,
    tasks: list[str],
    ctx: Context,
    max_tokens: int = 4096,
) -> dict:
    task_id, error = _required_task_id(task_id)
    if error:
        return error
    if not isinstance(tasks, list) or not 2 <= len(tasks) <= 10:
        return {"error": "tasks must contain between 2 and 10 non-empty objectives"}
    normalized: list[str] = []
    for item in tasks:
        objective = str(item or "").strip()
        if not objective:
            return {"error": "tasks must not contain blank objectives"}
        if len(objective) > 20_000:
            return {"error": "each task objective must be at most 20000 characters"}
        normalized.append(objective)
    token_limit = max(128, min(int(max_tokens), 32_000))

    prepared = await _async_request(
        "POST",
        "/spawn-multiple-subagents",
        timeout=15,
        json={"task_id": task_id, "objectives": normalized},
    )
    if "error" in prepared or "detail" in prepared:
        return prepared
    dispatch = prepared.get("dispatch")
    if not isinstance(dispatch, dict):
        return {"error": "Capability OS returned an invalid parallel subagent dispatch"}
    children = dispatch.get("subagents")
    if not isinstance(children, list) or len(children) != len(normalized):
        return {"error": "Capability OS returned an invalid parallel subagent cohort"}
    child_ids = [
        str(((item.get("task") or {}).get("taskId")) or "").strip()
        for item in children
        if isinstance(item, dict)
    ]
    if len(child_ids) != len(normalized) or any(not value for value in child_ids):
        return {"error": "Capability OS returned a child without a task_id"}
    if len(set(child_ids)) != len(child_ids):
        return {"error": "Capability OS returned duplicate child task_ids"}

    start_gate = asyncio.Event()

    async def run_branch(index: int, objective: str, child_task_id: str) -> dict:
        await start_gate.wait()
        prompt = (
            "You are an isolated parallel continuation of the calling ChatGPT. Work only on the assigned "
            "objective below. You already own an isolated Capability OS task; do not bootstrap another task "
            "and do not delegate to CPTR native/delegated agents or any external model worker. Use the bound "
            "Capability OS tools to inspect, resolve required effects, forge or acquire missing capabilities, "
            "execute them, and collect concrete evidence. Do not claim work you did not verify. Return a compact "
            "result with status, findings, evidence, changes, blockers, and next action.\n\n"
            f"CHILD TASK: {child_task_id}\n"
            f"BRANCH {index + 1}/{len(normalized)} OBJECTIVE:\n{objective}"
        )
        try:
            result = await ctx.sample(
                messages=prompt,
                tools=_bound_sampling_tools(child_task_id),
                max_tokens=token_limit,
                tool_concurrency=0,
                mask_error_details=True,
            )
            return {
                "index": index,
                "task_id": child_task_id,
                "objective": objective,
                "status": "complete",
                "output": result.text or "",
            }
        except Exception as exc:
            return {
                "index": index,
                "task_id": child_task_id,
                "objective": objective,
                "status": "client_sampling_failed",
                "error": "connected MCP client sampling request failed",
                "error_type": type(exc).__name__,
            }

    workers = [
        asyncio.create_task(run_branch(index, objective, child_ids[index]))
        for index, objective in enumerate(normalized)
    ]
    await asyncio.sleep(0)
    start_gate.set()
    results = await asyncio.gather(*workers)
    completed = sum(item["status"] == "complete" for item in results)
    return {
        "task": prepared.get("task"),
        "requested": len(normalized),
        "completed": completed,
        "failed": len(normalized) - completed,
        "dispatch": {
            **dispatch,
            "protocol": "MCP sampling/createMessage",
            "samplingRequestsIssuedConcurrently": True,
            "hostParallelInferenceVerified": False,
            "nativeSubagentMapping": "client-defined",
        },
        "results": results,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8788"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
