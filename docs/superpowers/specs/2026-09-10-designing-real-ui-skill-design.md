# Designing Real UI Skill Design

## Goal

Create a reusable CPTR workspace skill that makes UI work begin from the real product contract: implemented backend routes, action APIs, streaming transports, authorization, lifecycle state, error behavior, and existing theme primitives. Visual ambition is required, but no interaction may be invented merely to make a mockup look complete.

## Baseline failure

Before this skill existed, the Capability OS Design A concept included an attractive bottom prompt field that visually implied it could send a message into the current ChatGPT Official conversation. Repository verification later showed that CPTR has a real `/api/chats` model transport but no native MCP mechanism for injecting a new user turn into the current ChatGPT Official conversation. The visual concept therefore outran the actual transport contract.

This skill exists to prevent that class of failure.

## Scope

The skill applies when creating, redesigning, polishing, modernizing, or auditing a product UI whose behavior depends on an existing backend, plugin, MCP action surface, API, SSE/WebSocket/Socket.IO stream, browser/device transport, task lifecycle, or persisted state.

It is a workspace skill at `.cptr/skills/designing-real-ui/` so CPTR can discover it before UI implementation in this repository.

## Core contract

Before designing interactive UI, the agent must build a feature-contract ledger. Every visible interactive or live element maps to:

1. user-visible purpose;
2. authoritative backend route/tool/function;
3. request/input contract;
4. response/state contract;
5. live transport if any;
6. loading/empty/error/stale/offline/reconnect/cancel behavior;
7. authorization or approval boundary;
8. verification method.

If a backend capability cannot be verified, the UI must either omit that affordance or render it explicitly as non-interactive/read-only with truthful copy. A visual prototype cannot silently promote an unimplemented idea into a functional control.

## Theme contract

The UI must preserve the existing product visual language. For CPTR this means inspecting the active `app-theme` primitives, CSS variables, surface/accent tokens, typography, spacing, border radii, interaction states, dark/light behavior, and existing component conventions before adding local styling. New effects extend those primitives rather than replacing them with an unrelated design system.

Eye-catching treatment should come from hierarchy, spatial composition, data visualization, motion, depth, progressive disclosure, and micro-interaction—not large blocks of explanatory prose or arbitrary neon styling.

## Live-state contract

Anything described as live, real-time, streaming, connected, healthy, running, active, mounted, leased, online, or operational must be driven by authoritative runtime state. It must define initial connection, streaming updates, reconnect, stale/offline, terminal completion, cancellation/release, and recovery behavior where applicable.

Decorative animation may never masquerade as runtime activity. Motion representing runtime changes must derive from runtime data.

## Implementation discipline

Behavior changes follow test-driven development. UI work must inspect current frontend and backend implementation before writing production code. Prefer existing routes and stores over duplicate endpoints. Preserve server authority and existing authorization boundaries.

For substantial redesigns, verify desktop and mobile layouts and `prefers-reduced-motion`. Keyboard focus, labels, contrast, touch targets, empty/error/loading states, and progressive disclosure are required.

## Verification gate

A UI change is not complete until evidence covers the relevant subset of:

- contract/interaction tests;
- frontend tests;
- typecheck;
- production build;
- managed-browser rendering inspection;
- actual API request/response behavior;
- SSE/WebSocket/Socket.IO event behavior and recovery;
- browser console/network errors;
- responsive/mobile behavior;
- reduced-motion behavior;
- authorization and error paths.

A visually successful but nonfunctional UI fails the gate.

## Skill assets

- `.cptr/skills/designing-real-ui/SKILL.md`: concise activation and workflow rules.
- `.cptr/skills/designing-real-ui/references/ui-feature-contract.md`: reusable feature-contract ledger and verification template.
- `tests/test_designing_real_ui_skill.py`: repository regression test for discoverability and mandatory contract clauses.
