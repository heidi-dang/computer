# CPTR Execution-Plane Boundary

## Purpose

CPTR currently serves HTTP/control traffic and owns local execution resources in one process. That model is intentionally preserved for correctness: command process handles, PTYs, managed browser sessions, live subscribers, and some coordination state are process-local and cannot safely be load-balanced between independent Uvicorn workers.

The backend performance work introduces an explicit `CommandSessionRegistry` service boundary in `cptr/services/execution_manager.py`. The API and tool layers consume that boundary while the implementation remains in-process. This is the migration seam for a future external execution service; it is not permission to enable multiple web workers today.

## Current ownership model

```text
FastAPI / Control API
        |
        v
Execution-manager boundary
        |
        +-- command session registry
        +-- child process / PTY ownership
        +-- bounded output rings
        +-- log/event writer tasks

Live-event hub
        |
        +-- bounded subscriber queues
        +-- batched SQLite durability
```

A command is created, observed, cancelled, and drained by the process that owns its OS process handle. Shutdown first stops new command creation, terminates owned process groups, waits for capture/log/event writers, escalates remaining processes to a forced kill, then closes live-event durability.

On Linux, spawned command children also receive best-effort `PR_SET_PDEATHSIG(SIGTERM)` configuration so an unexpected CPTR parent-process death is less likely to leave an orphan process group.

## Why multiple Uvicorn workers remain unsupported

A second independent worker would have a different memory image. Without an external owner it could receive a follow-up request for a command/browser session that another worker started and would not possess the corresponding process handle, PTY, browser client, subscriber queue, or in-memory coordination state.

Therefore performance scaling must not use `--workers N` until these ownership surfaces are externalized. The current optimizations instead reduce work per request/event and remove blocking I/O from the event-loop hot path.

## Required externalization before multi-worker serving

A future execution service must provide durable or IPC-accessible operations for:

1. command create/status/output/input/resize/cancel and shutdown ownership;
2. managed browser session ownership and serialized browser control;
3. live-event fan-out/replay cursors across API workers;
4. command/task idempotency state that cannot diverge per worker;
5. bounded backpressure and per-user execution quotas;
6. lease/fencing semantics so two execution owners cannot control the same resource;
7. restart recovery that can distinguish a dead owner from an active one;
8. authenticated owner/workspace identity on every IPC operation.

The external service should return opaque resource IDs. API workers should never serialize raw process handles, PTY file descriptors, browser credentials, or private execution environment data into the database or public API.

## Scaling decision gate

Do not introduce the extra process/service merely to chase theoretical throughput. Consider externalizing the execution plane only when measured API/event-loop/SQLite metrics show that the optimized single-process owner is the limiting resource and the expected workload actually requires independent API-worker scaling.

Until then, one execution owner is the simpler and safer production architecture.
