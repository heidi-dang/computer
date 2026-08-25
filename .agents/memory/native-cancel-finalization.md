---
name: Native cancellation finalization
description: Native CPTR cancellation must finalize durable messages even when cancellation wins before coroutine startup.
---

Native task cancellation has two cleanup cases: a started coroutine must re-raise cancellation after its durable cleanup, while a task cancelled before its first scheduling turn never enters that cleanup handler. The cancellation boundary must await the task and durably finalize any still-unfinished assistant message, including rejecting pending tool calls.

**Why:** Immediate authenticated cancellation exposed a nonterminal assistant message despite there being no live task, which could violate terminal-state and no-resurrection guarantees.

**How to apply:** Treat cancellation acknowledgment as incomplete until both the in-memory task and durable message are terminal; test both pre-start and active-loop cancellation.