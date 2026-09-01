# MCP Model Usage and API-Equivalent Cost Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-call ChatGPT model self-reporting, bounded MCP-visible token estimation, server-owned OpenAI pricing projection, and responsive `/mcp` token/cost analytics.

**Architecture:** The MCP adapter adds optional `client_model` metadata to every CPTR tool schema and server instructions ask ChatGPT to populate it on every call. The adapter removes that field before existing handlers, estimates tokens only from the MCP-visible tool-call/result envelopes, and emits one terminal Usage diagnostic. The Computer backend owns the versioned price registry and cost arithmetic; the frontend derives bounded 60-second charts from the existing Diagnostics SSE stream.

**Tech Stack:** TypeScript, MCP SDK, Zod, js-tiktoken, Python/Pydantic/FastAPI/Decimal, Svelte 5, Node tests, pytest/Ruff, Vite.

**Spec:** `docs/superpowers/specs/2026-09-02-mcp-model-usage-cost-simulation-design.md`

## Global constraints

- Display **API-equivalent simulated cost** and **Estimated · MCP-visible tokens**, never actual billing.
- `client_model` is optional, 1–120 characters when present, and never reaches existing CPTR business payloads or Activity arguments.
- Tool call name/arguments are estimated model output tokens; returned tool result/error is estimated model input tokens.
- Cached input remains unknown and is not charged in the simulation.
- Pricing registry version is `openai-2026-08-21-promo`; GPT-5.6 Sol is $4/M input, $0.40/M cached input, $20/M output, verified 2026-09-02.
- Usage state is bounded; default backend capacity 500 events and UI timeline 60 seconds in 5-second buckets.
- Usage telemetry contains counts and bounded metadata only, never raw tool payloads.
- Estimation/telemetry failures cannot change MCP tool results.
- Preserve current Traffic, Activity, topology, Console, action count, host classification, and mobile behavior.

---

### Task 0: Correlated Diagnostics prerequisite

**Status:** completed and verified before this plan.

- [x] Integrated the isolated diagnostics lane.
- [x] Passed 117/117 plugin tests, typecheck, build, and disposable MCP SDK acceptance.
- [x] Committed `feat: add correlated MCP diagnostics telemetry`.

### Task 1: Per-call model self-reporting

**Files:**
- Modify: `server/mcp.ts`
- Create/Test: `tests/mcp-usage.test.ts`
- Modify/Test: `tests/mcp.test.ts`

**Produces:**

```ts
export const CLIENT_MODEL_INSTRUCTION: string;
export function extractClientModel(input: unknown): {
  reported: string | null;
  handlerInput: unknown;
};
```

- [ ] Write tests proving server instructions request the current model on every CPTR call.
- [ ] Write tests proving every registered tool exposes optional `client_model` with max length 120.
- [ ] Write a failing invocation test proving `client_model` must not reach the mocked ComputerClient.
- [ ] Run `npx tsx --test tests/mcp-usage.test.ts tests/mcp.test.ts` and confirm RED.
- [ ] Add the MCP SDK server instruction and shared schema field at the registration wrapper.
- [ ] Strip `client_model` before Activity serialization, worker helpers, and the existing tool handler.
- [ ] Re-run focused tests and commit `feat: request current model metadata on CPTR calls`.

### Task 2: Token estimator and Usage diagnostics

**Files:**
- Create: `server/mcp-usage.ts`
- Modify: `server/mcp-diagnostics.ts`
- Modify: `server/mcp.ts`
- Modify: `package.json`, `package-lock.json`
- Test: `tests/mcp-usage.test.ts`, `tests/mcp-diagnostics.test.ts`
- Modify/Test: `scripts/check-mcp-live-activity-integration.mjs`

**Produces:**

```ts
export type TokenEstimate = { tokens: number; method: string; exact_for_model: boolean };
export function normalizeReportedModel(value: unknown): { reported: string | null; canonical: string | null };
export function estimateModelTokens(modelId: string | null, text: string): TokenEstimate;
export function canonicalToolCallEnvelope(toolName: string, args: unknown): string;
```

- [ ] Add RED tests for exact reviewed model aliases and unknown-model non-matching.
- [ ] Add RED tests for deterministic positive token estimates and bounded fallback estimation.
- [ ] Add `js-tiktoken` as a server dependency and update the lockfile.
- [ ] Implement reusable tokenizer selection and byte-bounded fallback estimation.
- [ ] Add strict `kind: "usage"` Diagnostics event with model, token, correlation, estimator, tool, and status metadata only.
- [ ] Estimate tool-call envelope before handler execution and returned result/error after terminal execution.
- [ ] Emit exactly one Usage event for each completed/failed registered tool call.
- [ ] Prove Activity/backend inputs exclude `client_model` while Usage output-token estimation includes the original model-generated argument envelope.
- [ ] Extend disposable MCP SDK acceptance with `client_model: "GPT-5.6 Sol"` and one correlated Usage event.
- [ ] Run `npm test`, `npm run typecheck`, `npm run build`, live acceptance, and `git diff --check`.
- [ ] Commit `feat: estimate MCP-visible model token usage`.

### Task 3: Backend pricing and bounded usage projection

**Files:**
- Create: `cptr/services/mcp_pricing.py`
- Modify: `cptr/services/mcp_diagnostics.py`
- Create/Test: `tests/test_mcp_usage_pricing.py`
- Modify/Test: `tests/test_mcp_diagnostics.py`, `tests/test_mcp_diagnostics_api.py`

**Produces:**

```python
class McpUsageDiagnostic(BaseModel): ...

def normalize_pricing_model(model_reported: str | None, model_canonical: str | None) -> str | None: ...
def project_usage_cost(event: McpUsageDiagnostic, *, today=None): ...
```

- [ ] Write RED tests for GPT-5.6 Sol cost math: 1M input + 1M output = $24.00.
- [ ] Test missing model, unknown model, and stale promotional price states.
- [ ] Implement immutable Decimal-based registry from the approved spec.
- [ ] Add strict Usage Pydantic schema and include it in `McpDiagnosticsBatch`.
- [ ] Add bounded usage deque with default capacity 500 and dedupe by event ID.
- [ ] Project cost on ingest, retain usage, emit `event: usage`, and expose usage capacity in stream health.
- [ ] Add retained-window token/cost totals and totals grouped by model.
- [ ] Ensure latest model comes only from the latest retained usage event.
- [ ] Run focused pytest/Ruff/format gates and commit `feat: price bounded MCP usage estimates`.

### Task 4: Typed frontend Usage state

**Files:**
- Modify: `cptr/frontend/src/lib/apis/mcp.ts`
- Modify: `cptr/frontend/src/lib/stores/mcp-diagnostics.ts`
- Modify/Test: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

- [ ] Write RED hydration/incremental/timeline tests.
- [ ] Add Usage API types, snapshot totals, `onUsage`, and existing Diagnostics EventSource listener.
- [ ] Add bounded Usage reducer state and current-model projection.
- [ ] Add deterministic 60-second, 5-second `usageTimeline()` buckets for input/output tokens and simulated USD.
- [ ] Run focused Node tests and commit `feat: project MCP token and cost usage state`.

### Task 5: `/mcp` model usage and cost UI

**Files:**
- Create: `cptr/frontend/src/lib/components/mcp/McpUsageCostPanel.svelte`
- Modify: `cptr/frontend/src/lib/components/mcp/McpTopology.svelte`
- Modify/Test: `cptr/frontend/tests/mcp-traffic-topology.test.mjs`

- [ ] Write RED UI tests for Current model, Estimated input/output/total, Simulated cost, average cost/request, pricing status, and required disclaimer.
- [ ] Render missing/unknown/stale pricing states explicitly.
- [ ] Add responsive inline-SVG input/output token chart for the last 60 seconds.
- [ ] Add responsive inline-SVG simulated USD chart for the same buckets.
- [ ] Add collapsible pricing detail with input/cached/output rates, registry version, verified date, source, and freshness.
- [ ] Wire `onUsage: applyDiagnosticEvent` into the existing Diagnostics stream; do not open another stream.
- [ ] Keep layout `grid-cols-1` on narrow screens and preserve existing topology/Console behavior.
- [ ] Run all frontend Node regressions, zero-warning `build:clean`, touched-file Prettier, and `git diff --check`.
- [ ] Commit `feat: visualize MCP token and simulated cost usage`.

### Task 6: Combined acceptance

- [ ] Plugin: `npm test && npm run typecheck && npm run build && node scripts/check-mcp-live-activity-integration.mjs`.
- [ ] Backend: run Traffic/Activity/Diagnostics/topology/system/usage pytest suites.
- [ ] Frontend: run all MCP Node regressions and `npm --prefix cptr/frontend run build:clean`.
- [ ] Desktop `/mcp` browser audit: request chart, token chart, cost chart, pricing detail, topology, Recent Requests, backend monitor, Console.
- [ ] Exact 390×844 audit: no horizontal overflow; charts and controls remain usable.
- [ ] Mechanically verify Usage telemetry has no raw tool payload fields and UI terminology always says simulated/estimated.
- [ ] Report exact commits, test counts, registry version/date, visual acceptance, and the limitation that complete ChatGPT context/cache/reasoning/final-answer usage is not visible to MCP.

No production deployment occurs unless separately requested.
