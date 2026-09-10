---
name: designing-real-ui
description: Use when designing or redesigning a working product UI.
version: 0.1.0
---

# Designing Real Working UI

Design product UI from the real software contract outward. The result should be visually distinctive and polished, but every functional or live-looking affordance must be backed by verified product behavior. This skill does not permit decorative mock controls to masquerade as implemented features.

The existing product is the design-system authority. Preserve its theme, tokens, interaction language, and backend ownership unless the user explicitly requests a broader design-system change.

## When to Use

Use this skill for requests such as:

- "redesign this UI"
- "polish the whole dashboard"
- "make this page eye catching"
- "reduce all this text and make it visual"
- "add animations / live status / realtime activity"
- "modernize the plugin UI"
- "build UI for these backend features"
- "make every button actually work"

Use it whenever frontend work depends on an existing API, MCP action, backend route, SSE/WebSocket/Socket.IO stream, task lifecycle, browser/device connection, persisted state, approval boundary, or server-authoritative status.

Do not use it for a purely static illustration or throwaway visual concept that the user explicitly says does not need to work.

## Prerequisites

- Access to the real frontend and backend source involved in the feature.
- Ability to inspect existing API clients, routes, stores, events, authorization, and theme primitives.
- Ability to run relevant tests/typecheck/build and inspect a rendered UI.
- For realtime UI, access to the actual stream/event source or a deterministic test harness for it.

Do not invent missing credentials, endpoints, event names, request fields, response fields, status values, or backend capabilities.

## How to Run

1. Inspect before designing. Use `list_directory`, `search_files`, and `read_file` to localize the frontend component, API client, backend route/service, live transport, tests, and theme primitives.
2. Create a **feature-contract ledger** from `references/ui-feature-contract.md` before adding interactive UI.
3. Classify every proposed element as `interactive`, `live`, `derived`, or `decorative`.
4. For `interactive` and `live` elements, prove the real backend/API/live-state contract before implementation.
5. If the contract cannot be proved, **fail closed**: omit the control, make it explicitly read-only, or label it as unavailable. Never silently fake functionality.
6. Design within the **existing theme** first. Extend its tokens and components instead of replacing them with an unrelated visual language.
7. Implement behavior test-first, then visual hierarchy, motion, responsive behavior, and progressive disclosure.
8. Verify the rendered UI through the managed browser plus API/live-transport evidence before claiming completion.

## Quick Reference

| UI concern | Required source of truth |
|---|---|
| Button / form / command | Real backend route, action, or existing client function |
| Metric / badge / health | Real response field or deterministic derived state |
| Live / streaming indicator | Real SSE, WebSocket, Socket.IO, polling, or task lifecycle state |
| Connected / online | Authoritative connection/session/device state |
| Progress | Real lifecycle progress or explicitly indeterminate state |
| Prompt / message input | Verified transport and destination semantics |
| Runtime animation | Real live state if it implies activity |
| Decorative animation | May be local, but must not imply fake runtime activity |
| Theme | Existing product tokens, variables, components, dark/light behavior |
| Destructive/privileged action | Existing authorization, confirmation, approval, and error boundary |

## Procedure

### 1. Audit the current product contract

Find the exact files and symbols that currently own the feature. Trace both directions:

`UI → API client/store → route/action → service/runtime`

and for realtime state:

`service/runtime → event transport → client/store → UI`

Record exact route/action names and fields. Do not design from README claims when current code can be inspected.

For each visible function, answer:

- What triggers it?
- What exact backend authority executes it?
- What input is accepted?
- What state comes back?
- How does failure surface?
- Can it be cancelled/released/retried?
- What makes it stale or disconnected?
- What authorization or approval boundary applies?

### 2. Build the feature-contract ledger

Copy `references/ui-feature-contract.md` into working notes and fill one row per meaningful element.

No interactive or live element moves to implementation while `Backend authority` or `Verification evidence` is `unknown`.

A prompt box is not "working" merely because a text field and send icon exist. Its transport, destination, model/session semantics, streaming response path, cancellation, queueing, and errors must be verified.

### 3. Audit the existing theme

Before choosing visual treatment, inspect the actual theme implementation:

- CSS variables and design tokens;
- `app-theme` or equivalent root theme contract;
- surface, accent, foreground, divider, hover, focus, danger, and success states;
- typography scale and weight;
- radius, spacing, elevation, density, and breakpoint conventions;
- dark/light behavior;
- existing icon and component conventions.

Preserve those primitives. New styling should use token composition such as `color-mix`, existing variables, and established component states instead of hardcoded unrelated palettes where the codebase already provides semantic tokens.

### 4. Reduce text through information design

Prefer visual structure over explanatory prose:

- status rings and compact badges for state;
- spatial flows for causality/lifecycle;
- charts only when the data supports them;
- progressive disclosure and drawers for detail;
- concise labels plus tooltips where needed;
- grouped metrics instead of repeated sentences;
- timelines for event history;
- node/edge views for topology and relationships;
- skeletons and meaningful empty states instead of text-heavy placeholders.

Do not turn every card into the same box. Create hierarchy with composition, scale, whitespace, alignment, depth, and controlled asymmetry while staying inside the original theme.

### 5. Add motion with semantic intent

Motion may be eye catching, but it must communicate hierarchy or state.

Good uses:

- entrance transitions for newly available data;
- smooth value/graph transitions after real updates;
- activity pulses driven by real stream events;
- focus/hover/press feedback;
- spatial transitions between related views;
- subtle ambient decorative motion that is clearly decorative.

Bad uses:

- perpetual "live" pulses when no live source exists;
- fake progress loops for completed or disconnected work;
- random network/activity particles presented as telemetry;
- animation that obscures errors or stale state;
- excessive glow/neon that breaks the original backend UI themes.

Always support `prefers-reduced-motion`. Reduced motion must preserve information and operability.

### 6. Implement real lifecycle states

For every async or live feature, implement the applicable states explicitly:

`idle → loading/connecting → active/streaming → completed/released`

with branches for:

`empty`, `error`, `stale`, `offline`, `reconnecting`, `cancelled`, `unauthorized`, and `approval-required`.

The backend remains authoritative. Do not infer "online", "healthy", "complete", or "connected" solely from a local animation timer or optimistic UI unless the backend contract explicitly supports optimistic state.

Prevent stale responses from overwriting newer state. Preserve cancellation/release semantics. For reconnecting transports, recover from authoritative state rather than assuming the last browser state is still true.

### 7. Implement behavior before decoration

For behavior changes, write the failing interaction/contract test first and observe the expected failure. Then implement the smallest functional path that passes.

Only after the interaction is proven should visual polish be considered complete.

Never accept:

- a button with no working handler;
- a handler calling a guessed endpoint;
- a graph backed by fabricated numbers;
- a "live" label backed by static data;
- a prompt field whose destination semantics are false;
- a control that bypasses existing approval/authorization;
- a UI-only success state when the backend failed.

### 8. Verify in the rendered product

Use the managed browser to inspect the actual route at representative desktop and mobile sizes. Verify keyboard navigation, focus visibility, touch targets, overflow, empty/error/loading behavior, and reduced motion.

Inspect browser **console** and **network** behavior for the changed flow. Exercise the real API and, where applicable, the real live transport.

If authentication prevents browser inspection, verify the build/test/API contract and report the blocked visual gate explicitly rather than claiming visual completion.

## Pitfalls

- **Mockup drift:** A beautiful concept adds features the backend does not have. Fix by completing the ledger before implementation.
- **Backend-name drift:** UI labels imply broader semantics than the actual route/action. Use truthful product language.
- **Theme replacement:** A redesign looks like a different product. Reuse existing tokens and interaction states first.
- **Fake realtime:** Animation substitutes for live data. Separate decorative motion from runtime-derived motion.
- **Polling masquerading as streaming:** Name and design the behavior according to its actual transport.
- **Happy-path-only UI:** Loading/error/offline/reconnect/approval states are omitted. They are part of the feature.
- **Text-to-cards conversion:** Long prose is merely split into many cards. Replace prose with hierarchy, visuals, progressive disclosure, and concise labels.
- **Visual verification skipped:** Source looks plausible but rendered layout regresses. Browser inspection is mandatory for substantial UI changes.
- **Performance regression:** Expensive animation, graphs, or listeners stay active while hidden. Pause/dispose work according to visibility and lifecycle.

## Verification

A UI implementation passes this skill only when the applicable evidence is fresh:

1. Feature-contract ledger contains no unresolved interactive/live element.
2. Focused interaction/contract tests pass after a demonstrated RED failure for changed behavior.
3. Frontend tests pass.
4. Typecheck passes.
5. Production build passes.
6. Managed browser confirms the actual rendered route on desktop and mobile.
7. Browser console has no relevant new errors.
8. Network/API calls use the verified real contract.
9. Realtime features prove update, disconnect/reconnect, terminal/cancel/release behavior as applicable.
10. `prefers-reduced-motion` preserves usability and information.
11. Existing theme behavior remains coherent in supported appearance modes.
12. Authorization and failure paths remain server-authoritative.

**Acceptance rule:** eye-catching + nonfunctional = failed. Functional + visually disconnected from the product theme = failed. The accepted result is both real and designed.
