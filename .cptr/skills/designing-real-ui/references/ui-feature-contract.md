# UI Feature Contract Ledger

Use this ledger before implementing substantial UI. One row represents one meaningful visible function or live presentation. Split rows when one visual element has independently failing behaviors.

## Ledger

| UI element | Class | User-visible purpose | Backend authority | Request/input | Response/state | Live transport | Lifecycle / failure states | Authorization | Verification evidence | Disposition |
|---|---|---|---|---|---|---|---|---|---|---|
| Example: Send prompt | interactive | Submit work to the configured chat runtime | `POST /api/chats` via the existing chat API client | content, model, workspace, chat/session parameters | chat/message IDs plus streamed assistant state | existing chat event transport | sending, queued, streaming, done, cancelled, error | existing authenticated chat boundary | focused API/interaction test + network trace + rendered smoke | implement only with truthful destination copy |

## Field rules

### UI element

Name the exact control, visualization, badge, graph, command field, task item, status indicator, or interactive surface the user will see.

### Class

Use one of:

- `interactive` — causes a real product action;
- `live` — represents changing runtime state;
- `derived` — computed from real state but does not perform actions;
- `decorative` — visual only and must not imply runtime truth.

### Backend authority

Record the exact route, MCP/tool action, API client function, store authority, service, or server-owned lifecycle that makes this behavior real. `unknown`, guessed names, README-only claims, or mock endpoints block implementation of interactive/live behavior.

### Request/input

Record the verified fields, types, target identity, task/session/workspace binding, idempotency data, cancellation identity, and other meaningful request semantics.

### Response/state

Record the authoritative response fields and which of them drive user-visible state. Distinguish server truth from local optimistic state.

### Live transport

Record the actual transport: SSE, WebSocket, Socket.IO, bounded polling, event replay, or `none`. Never write `live` as a transport.

For a live element include the event names or lifecycle states that drive the UI when they can be verified from source.

### Lifecycle / failure states

List the applicable states, for example:

`idle`, `loading`, `connecting`, `active`, `streaming`, `empty`, `stale`, `offline`, `reconnecting`, `approval-required`, `unauthorized`, `cancelled`, `released`, `completed`, `error`.

Do not add states the backend cannot actually distinguish unless they are explicitly local presentation states.

### Authorization

Record authentication, scope, lease, approval, epoch, confirmation, ownership, or other server-authoritative boundary. A redesign must not weaken it.

### Verification evidence

Name the concrete proof required before acceptance, such as:

- test file and test name;
- successful real API request/response;
- captured event/reconnect behavior;
- managed-browser interaction;
- console/network inspection;
- desktop/mobile screenshot inspection;
- reduced-motion inspection;
- authorization/error-path exercise.

### Disposition

Use exactly one decision:

- `implement` — contract is verified;
- `read-only` — information is real but no action is supported;
- `unavailable` — show truthful unavailable state when product value requires visibility;
- `omit` — no real contract exists or the element adds no value.

## Pre-implementation gate

For every `interactive` or `live` row:

- Backend authority is exact and verified.
- Request/input contract is verified.
- Response/state contract is verified.
- Live transport is verified when applicable.
- Failure/reconnect/cancel/release states are identified when applicable.
- Authorization boundary is identified.
- Verification evidence is executable or directly inspectable.
- Disposition is `implement` only when the preceding fields are complete.

If any item fails, do not fabricate a working control. Choose `read-only`, `unavailable`, or `omit`.

## Visual/theme companion check

For the whole view, record:

- theme root / token files inspected;
- surface/accent/foreground/divider/focus/error/success tokens reused;
- typography and spacing conventions preserved;
- desktop and mobile composition target;
- dark/light appearance behavior if supported;
- `prefers-reduced-motion` behavior;
- which animations are runtime-derived versus decorative;
- progressive-disclosure strategy replacing unnecessary prose.

## Final verification record

| Gate | Evidence | Result |
|---|---|---|
| Focused interaction/contract tests |  |  |
| Frontend tests |  |  |
| Typecheck |  |  |
| Production build |  |  |
| Managed browser — desktop |  |  |
| Managed browser — mobile |  |  |
| Console errors |  |  |
| Network/API contract |  |  |
| Realtime/reconnect/cancel/release |  |  |
| Authorization/error paths |  |  |
| Reduced motion |  |  |
| Theme coherence |  |  |

Do not mark a UI task complete while an applicable gate is unverified.
