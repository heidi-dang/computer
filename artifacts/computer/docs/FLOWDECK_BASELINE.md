# FlowDeck-C-PTR Phase 0 baseline audit

Date: 2026-08-23 (Australia/Sydney)

## Frozen references

- Requested CPTR source: `f9d1d8cdaf7e034b8fa19619063a6be931b9ee97`
- Requested FlowDeck v2.4.1 source: `b3c8ffa799ef6cbd41861565dae0c3c01c493db0`

The requested CPTR object was not available in the workspace Git metadata, and
`artifacts/computer` has no repository metadata. The requested FlowDeck source
was also not present locally. This audit therefore records the observed CPTR
artifact and does not claim byte-for-byte provenance or parity with an
unavailable checkout.

## Existing CPTR ownership map

| Boundary | Existing owner and evidence |
| --- | --- |
| HTTP chat entry | `cptr.routers.chat.send_message` (`POST /api/chats`) resolves the model, persists messages, exports the chat, and starts CPTR work. |
| Native execution primitive | `cptr.utils.chat_task.run_chat_task` is the sole native async model/tool loop. |
| Task creation | `cptr.utils.chat_task.start_task` creates the asyncio task that runs `run_chat_task`. |
| Async subagents | `cptr.utils.tools._run_existing_subagent_chat` re-enters `run_chat_task`; `cptr.utils.async_subagents` tracks process-local background work. |
| External agents | `cptr.utils.chat_task._run_agent_target` selects existing CPTR adapters, including optional OpenCode. |
| Model resolution | `cptr.utils.model_targets.resolve_model_target` and existing chat connection rules. |
| Tools and approvals | `cptr.utils.tools.execute_tool`; pending tool calls and resolution are handled by existing chat router endpoints. |
| Persistence | `cptr.models.chats.Chat` and `ChatMessage` use the existing SQLAlchemy/SQLite layer. |
| Realtime | `cptr.socket.main.emit_to_user` sends the existing `events:chat` Socket.IO event. |
| Restart/cancellation | `cancel_task` cancels native tasks; `reconcile_chat_state` repairs unfinished assistant messages after restart. |
| Identity/runtime | `cptr.utils.identity` and `cptr.utils.runtime.Runtime` mediate internal identity and workspace access. |

## Baseline checks

The imported CPTR artifact had no test suite or pytest configuration. The
existing verification before this milestone covered clean frontend install and
build, Python compilation, startup, health/config, authentication, persistence,
workspace files, Git, terminals, Socket.IO polling/WebSocket behavior, and
agent configuration. Those checks are treated as baseline evidence rather than
replaced by FlowDeck tests.

## Non-regression boundary

FlowDeck must remain advisory. The intended path is:

```text
FlowDeck shadow observation
        ↓ discard diagnostic
existing CPTR start_task
        ↓
cptr.utils.chat_task.run_chat_task
```

No FlowDeck code may create a model client, execute a tool, spawn a process,
start FDX, write orchestration state, emit an orchestration event, or change
the authoritative CPTR response in this milestone.