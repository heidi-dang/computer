import asyncio
import json
import os
import secrets
from typing import Any, Optional

import httpx
import mcp_types
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
        "multiple isolated client-model continuations in one MCP multi-round input batch when the "
        "client supports sampling with tools."
    ),
    version="2.3.1",
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


def _bound_sampling_tools(task_id: str) -> dict[str, Any]:
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

    return {
        "cptr_inspect": cptr_inspect,
        "cptr_resolve": cptr_resolve,
        "cptr_forge": cptr_forge,
        "cptr_execute": cptr_execute,
        "cptr_acquire": cptr_acquire,
        "cptr_reflect": cptr_reflect,
    }


_MAX_SAMPLING_ROUNDS = 6
_MAX_TOOL_CALLS_PER_BRANCH = 48
_MAX_TOOL_INPUT_CHARS = 100_000
_MAX_TOOL_RESULT_CHARS = 20_000


def _sampling_tool_definitions() -> list:
    return [
        mcp_types.Tool(
            name="cptr_inspect",
            description="Inspect this isolated Capability OS child task.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "additionalProperties": False,
            },
        ),
        mcp_types.Tool(
            name="cptr_resolve",
            description="Resolve task-visible capabilities for required effects.",
            input_schema={
                "type": "object",
                "properties": {
                    "required_effects": {"type": "array", "items": {}},
                    "optional_effects": {"type": ["array", "null"], "items": {}},
                    "forbidden_effects": {"type": ["array", "null"], "items": {}},
                },
                "required": ["required_effects"],
                "additionalProperties": False,
            },
        ),
        mcp_types.Tool(
            name="cptr_forge",
            description="Forge or evolve a tool inside this isolated Capability OS child task.",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "minLength": 1},
                    "payload_json": {"type": "string"},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        ),
        mcp_types.Tool(
            name="cptr_execute",
            description="Execute a task-visible Capability in this isolated child task.",
            input_schema={
                "type": "object",
                "properties": {
                    "capability_id": {"type": "string", "minLength": 1},
                    "inputs_json": {"type": "string"},
                },
                "required": ["capability_id"],
                "additionalProperties": False,
            },
        ),
        mcp_types.Tool(
            name="cptr_acquire",
            description="Discover, qualify, mount, invoke, or release MCP capabilities for this child task.",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "minLength": 1},
                    "payload_json": {"type": "string"},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        ),
        mcp_types.Tool(
            name="cptr_reflect",
            description="Record an evidence-backed observation for this isolated child task.",
            input_schema={
                "type": "object",
                "properties": {
                    "observation_type": {
                        "type": "string",
                        "enum": ["bug", "friction", "gap", "hypothesis", "success"],
                    },
                    "description": {"type": "string", "minLength": 1},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "low", "medium", "high", "critical"],
                    },
                    "context_json": {"type": "string"},
                },
                "required": ["observation_type", "description"],
                "additionalProperties": False,
            },
        ),
    ]


def _branch_prompt(index: int, total: int, objective: str, child_task_id: str) -> str:
    return (
        "You are an isolated parallel continuation of the calling ChatGPT. Work only on the assigned "
        "objective below. You already own an isolated Capability OS task; do not bootstrap another task "
        "and do not delegate to CPTR native/delegated agents or any external model worker. Use the bound "
        "Capability OS tools to inspect, resolve required effects, forge or acquire missing capabilities, "
        "execute them, and collect concrete evidence. Do not claim work you did not verify. Return a compact "
        "result with status, findings, evidence, changes, blockers, and next action.\n\n"
        f"CHILD TASK: {child_task_id}\n"
        f"BRANCH {index + 1}/{total} OBJECTIVE:\n{objective}"
    )


def _message_json(message: Any) -> dict:
    return message.model_dump(by_alias=True, mode="json", exclude_none=True)


def _sampling_request(messages: list[dict], max_tokens: int):
    return mcp_types.CreateMessageRequest(
        params=mcp_types.CreateMessageRequestParams(
            messages=[mcp_types.SamplingMessage.model_validate(item) for item in messages],
            max_tokens=max_tokens,
            tools=_sampling_tool_definitions(),
            tool_choice=mcp_types.ToolChoice(mode="auto"),
        )
    )


def _sampling_input_requests(state: dict) -> dict:
    return {
        branch["key"]: _sampling_request(branch["messages"], int(state["max_tokens"]))
        for branch in state["branches"]
        if branch["status"] == "pending"
    }


def _result_text(blocks: list[Any]) -> str:
    return "\n".join(
        block.text for block in blocks if isinstance(block, mcp_types.TextContent) and block.text
    ).strip()


async def _execute_sampling_tool(child_task_id: str, tool_use: Any) -> Any:
    tools = _bound_sampling_tools(child_task_id)
    tool = tools.get(tool_use.name)
    if tool is None:
        return {"error": f"unknown Capability OS sampling tool: {tool_use.name}"}
    raw_input = json.dumps(tool_use.input, sort_keys=True, separators=(",", ":"), default=str)
    if len(raw_input) > _MAX_TOOL_INPUT_CHARS:
        return {"error": "sampling tool input exceeds the bounded size limit"}
    try:
        return await tool(**dict(tool_use.input))
    except Exception as exc:
        return {
            "error": "Capability OS sampling tool execution failed",
            "error_type": type(exc).__name__,
        }


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
        "Atomically prepare 2-10 isolated Capability OS child contexts and request all client-model "
        "continuations in one MCP 2026 multi-round input batch. Every branch gets only the six Capability "
        "OS operations bound to its own child task. No delegated-agent or external-model fallback exists; "
        "the connected client must support sampling with tools."
    )
)
async def spawn_multiple_subagents(
    task_id: str,
    tasks: list[str],
    ctx: Context,
    max_tokens: int = 4096,
) -> Any:
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

    state_key = ctx.request_state
    if state_key is None:
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

        state_key = "parallel-subagents:" + secrets.token_urlsafe(18)
        branches = []
        for index, objective in enumerate(normalized):
            prompt = _branch_prompt(index, len(normalized), objective, child_ids[index])
            initial = mcp_types.SamplingMessage(
                role="user",
                content=mcp_types.TextContent(text=prompt),
            )
            branches.append(
                {
                    "key": f"branch-{index + 1}",
                    "index": index,
                    "task_id": child_ids[index],
                    "objective": objective,
                    "status": "pending",
                    "rounds": 0,
                    "tool_calls": 0,
                    "messages": [_message_json(initial)],
                    "output": "",
                    "error": "",
                }
            )
        state = {
            "parent_task_id": task_id,
            "objectives": normalized,
            "max_tokens": token_limit,
            "task": prepared.get("task"),
            "dispatch": dispatch,
            "branches": branches,
        }
        await ctx.set_state(state_key, state)
        return mcp_types.InputRequiredResult(
            input_requests=_sampling_input_requests(state),
            request_state=state_key,
        )

    state = await ctx.get_state(state_key)
    if not isinstance(state, dict):
        return {"error": "parallel subagent state expired or is unavailable; start a new fan-out"}
    if (
        state.get("parent_task_id") != task_id
        or state.get("objectives") != normalized
        or int(state.get("max_tokens") or 0) != token_limit
    ):
        await ctx.delete_state(state_key)
        return {"error": "parallel subagent retry arguments do not match the original fan-out"}

    responses = ctx.input_responses
    if not isinstance(responses, dict):
        return {"error": "parallel subagent retry did not include client sampling responses"}

    jobs: list[tuple[dict, Any]] = []
    for branch in state["branches"]:
        if branch["status"] != "pending":
            continue
        response = responses.get(branch["key"])
        if not isinstance(
            response,
            (mcp_types.CreateMessageResult, mcp_types.CreateMessageResultWithTools),
        ):
            branch["status"] = "client_sampling_failed"
            branch["error"] = "client did not return a sampling result for this branch"
            continue
        blocks = response.content if isinstance(response.content, list) else [response.content]
        assistant_message = mcp_types.SamplingMessage(role="assistant", content=blocks)
        branch["messages"].append(_message_json(assistant_message))
        tool_uses = [block for block in blocks if isinstance(block, mcp_types.ToolUseContent)]
        if not tool_uses:
            branch["status"] = "complete"
            branch["output"] = _result_text(blocks)
            continue
        branch["rounds"] += 1
        branch["tool_calls"] += len(tool_uses)
        if branch["rounds"] > _MAX_SAMPLING_ROUNDS:
            branch["status"] = "limit_exceeded"
            branch["error"] = "sampling round limit exceeded"
            continue
        if branch["tool_calls"] > _MAX_TOOL_CALLS_PER_BRANCH:
            branch["status"] = "limit_exceeded"
            branch["error"] = "sampling tool-call limit exceeded"
            continue
        jobs.extend((branch, tool_use) for tool_use in tool_uses)

    if jobs:
        tool_results = await asyncio.gather(
            *(_execute_sampling_tool(branch["task_id"], tool_use) for branch, tool_use in jobs)
        )
        by_branch: dict[str, list[Any]] = {}
        for (branch, tool_use), result in zip(jobs, tool_results):
            serialized = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
            text = serialized[:_MAX_TOOL_RESULT_CHARS]
            structured = (
                json.loads(serialized)
                if len(serialized) <= _MAX_TOOL_RESULT_CHARS
                else {"truncated": True, "text": text}
            )
            is_error = isinstance(result, dict) and ("error" in result or "detail" in result)
            by_branch.setdefault(branch["key"], []).append(
                mcp_types.ToolResultContent(
                    tool_use_id=tool_use.id,
                    content=[mcp_types.TextContent(text=text)],
                    structured_content=structured,
                    is_error=is_error,
                )
            )
        for branch in state["branches"]:
            results_for_branch = by_branch.get(branch["key"])
            if branch["status"] == "pending" and results_for_branch:
                branch["messages"].append(
                    _message_json(
                        mcp_types.SamplingMessage(role="user", content=results_for_branch)
                    )
                )

    await ctx.set_state(state_key, state)
    requests = _sampling_input_requests(state)
    if requests:
        return mcp_types.InputRequiredResult(
            input_requests=requests,
            request_state=state_key,
        )

    await ctx.delete_state(state_key)
    results = [
        {
            "index": branch["index"],
            "task_id": branch["task_id"],
            "objective": branch["objective"],
            "status": branch["status"],
            "output": branch["output"],
            **({"error": branch["error"]} if branch["error"] else {}),
        }
        for branch in state["branches"]
    ]
    completed = sum(item["status"] == "complete" for item in results)
    dispatch = state["dispatch"]
    return {
        "task": state.get("task"),
        "requested": len(normalized),
        "completed": completed,
        "failed": len(normalized) - completed,
        "dispatch": {
            **dispatch,
            "protocol": "MCP 2026 multi-round input_required sampling",
            "samplingRequestsBatchedInSingleRound": True,
            "hostParallelInferenceVerified": False,
            "nativeSubagentMapping": "client-defined",
            "fallback": "none",
        },
        "results": results,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8788"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
