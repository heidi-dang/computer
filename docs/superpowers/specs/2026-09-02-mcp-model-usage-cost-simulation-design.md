# MCP Model Usage and API-Equivalent Cost Simulation Design

**Date:** 2026-09-02

## Goal

Extend CPTR `/mcp` observability with a truthful, bounded simulation of the model-token usage attributable to MCP tool calls and the corresponding API-equivalent USD cost for the ChatGPT model that actually invoked each CPTR tool.

The feature must never present simulated usage as OpenAI billing. It must explicitly distinguish MCP-visible estimated tokens from ChatGPT's private full-context token accounting.

## Approved product definition

The UI calls this **API-equivalent simulated cost** and labels token counts as **Estimated · MCP-visible tokens**.

The feature does not claim to know the user's ChatGPT subscription charge, the complete prompt/context, reasoning token count, prompt-cache hits/writes, or final assistant-answer token count. Those values are not available to an MCP server and must not be fabricated.

## Model self-reporting contract

The MCP initialize envelope identifies the client but does not reliably expose the ChatGPT-selected model. CPTR will therefore ask ChatGPT to self-report its current model on every CPTR tool call.

### Server instructions

The ChatGPT Computer MCP server will add cross-tool server instructions equivalent to:

> When you are ChatGPT and invoke any CPTR tool, set `client_model` to the exact model identity you are currently running as for this tool call, for example `GPT-5.6 Sol`. Report the current value on every call; do not reuse or infer it from an earlier call. If the model identity is unavailable, omit the field rather than guessing.

MCP server instructions are a hint that clients may add to model context, so they are not sufficient by themselves. The same requirement will be visible in the shared input schema.

### Shared tool input field

Every registered CPTR MCP tool will expose one common optional field:

```ts
client_model?: string
```

Constraints:

- 1–120 characters when present;
- plain bounded text only;
- optional at protocol level so Claude, Gemini, MCP Inspector, older clients, and manual callers remain compatible;
- described as required-by-instruction for ChatGPT-originated calls;
- consumed by the MCP registration wrapper before the existing tool handler is called;
- never forwarded into CPTR backend business payloads;
- omitted from Activity `arguments_json` so operational input remains clean;
- included in the token estimate for the model-generated tool-call argument envelope because ChatGPT generated those characters.

If a ChatGPT request omits `client_model`, usage telemetry remains valid for token counts but its cost is `null` and the UI shows **Model not reported**. CPTR must not silently substitute the previous request's model.

## Model normalization

Self-reported model text is untrusted bounded metadata. A pure normalization function maps recognized names and aliases to canonical pricing IDs. Examples:

- `GPT-5.6 Sol`, `gpt-5.6-sol`, and `gpt-5.6` -> `gpt-5.6-sol`
- `GPT-5.6 Sol Pro` -> `gpt-5.6-sol-pro`
- `GPT-5.6 Terra` -> `gpt-5.6-terra`
- `GPT-5.6 Luna` -> `gpt-5.6-luna`
- current GPT-5.5 / GPT-5.4 / GPT-5.3-Codex / GPT-5.2 labels map to their rate-card entries when unambiguous.

Unknown model names remain visible as bounded self-reported text but receive `pricing_status: "unknown_model"` and no simulated cost. Fuzzy matching must never select a price for an ambiguous model.

## Token-accounting semantics

This design follows model billing orientation, not HTTP direction.

For one MCP tool call:

1. ChatGPT generates the tool name and tool-call arguments. CPTR estimates **model output tokens** from a deterministic canonical envelope `{"name": <tool-name>, "arguments": <original-arguments-including-client_model>}`.
2. CPTR returns the MCP tool result or safe error result. CPTR estimates **model input tokens** from the actual result/error envelope returned to the MCP client before Activity truncation.
3. MCP/JSON-RPC framing tokens are not reconstructed. The surrounding conversation/system/developer context, tool schema/token-selection overhead, hidden reasoning, cached context, subsequent final answer, and non-CPTR tool activity are not visible and are excluded.

The UI must explain this orientation. It must not relabel HTTP request bytes as model input tokens.

### Cached input

CPTR cannot observe ChatGPT cache accounting. It records `cached_input_tokens_estimated = null`, not zero, and does not apply the cached-input rate to usage. The pricing card may display the official cached-input rate for reference, but the simulated request cost uses only observable estimated uncached input and output token counts.

### Long context

The complete ChatGPT input token count is unavailable, so CPTR cannot know whether a hidden request crosses the >272K long-context threshold. The simulator therefore does not apply the long-context multiplier from invisible context. The UI states **Long-context multiplier not inferable from MCP-visible tokens**.

## Token estimator

Tokenization happens inside the MCP adapter before Activity result truncation/redaction so raw tool payloads never need to be sent to the Computer backend for counting.

The estimator interface is:

```ts
export type TokenEstimate = {
  tokens: number;
  method: string;
  exact_for_model: boolean;
};

export function estimateModelTokens(modelId: string | null, text: string): TokenEstimate;
```

Implementation uses `js-tiktoken` as a server-only dependency. The adapter constructs one reusable tokenizer instance rather than loading token data per call. GPT-5.6 calls use the closest supported OpenAI encoding available to `js-tiktoken`; because OpenAI's public GPT-5.6 model page does not specify a tokenizer encoding, `exact_for_model` remains `false` unless the installed tokenizer library explicitly maps the canonical model ID.

Requirements:

- deterministic for identical text/model inputs;
- Unicode safe;
- bounded CPU and memory;
- tokenization input is capped by a configurable bounded byte limit; if the actual MCP result exceeds the exact-tokenization cap, the event records a deterministic byte-based fallback estimate and `exact_for_model: false` rather than truncating the counted logical payload without disclosure;
- uses the closest supported tokenizer for the canonical reported model;
- records the estimator method in telemetry;
- `exact_for_model` is false unless the tokenizer library explicitly declares the reported model mapping;
- estimator failure never affects the MCP tool result; it produces missing usage telemetry instead.

The UI always says **estimated**, even when the tokenizer mapping is exact, because the overall ChatGPT request context remains only partially observable.

## Pricing registry

Pricing is owned by the Computer backend, not trusted from the plugin. The plugin sends model identity and estimated token counts only. The backend applies a versioned server-side registry.

Initial registry version: `openai-2026-08-21-promo`.

Official sources verified on 2026-09-02:

- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/models/compare
- https://help.openai.com/en/articles/20001415

At minimum, the initial registry includes these current GPT-5.6 rates per 1M text tokens:

| Canonical model | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| `gpt-5.6-sol` | $4.00 | $0.40 | $20.00 |
| `gpt-5.6-sol-pro` | $5.00 | $0.50 | $30.00 |
| `gpt-5.6-terra` | $2.00 | $0.20 | $12.00 |
| `gpt-5.6-luna` | $0.20 | $0.02 | $1.20 |
| `gpt-5.5` | $5.00 | $0.50 | $30.00 |
| `gpt-5.4` | $2.50 | $0.25 | $15.00 |
| `gpt-5.4-mini` | $0.75 | $0.075 | $4.50 |
| `gpt-5.3-codex` | $1.75 | $0.175 | $14.00 |
| `gpt-5.2` | $1.75 | $0.175 | $14.00 |

These are the only initial priced canonical IDs. Any other self-reported model, including restricted/specialized models not listed above, is `unknown_model` until an explicitly reviewed registry entry is added.

Each price entry exposes:

```text
model_id
input_usd_per_million
cached_input_usd_per_million
output_usd_per_million
pricing_version
verified_at
valid_through (nullable)
source_label
source_url
```

GPT-5.6 Sol promotional pricing is documented as available at least through 2026-11-21. A dated registry entry must not claim indefinite freshness. When a non-null `valid_through` is in the past, `pricing_status` becomes `stale` and the UI shows the last-known rate but does not label the calculated amount as current/exact.

No runtime scraping or external network dependency is added to CPTR request handling.

## Usage telemetry event

Usage extends the existing bounded Diagnostics channel with one terminal event per MCP tool call.

Proposed wire shape:

```json
{
  "kind": "usage",
  "version": 1,
  "event_id": "...",
  "timestamp_ms": 0,
  "request_id": "...",
  "correlation_id": "...",
  "session_id": "...",
  "client_id": "chatgpt",
  "model_reported": "GPT-5.6 Sol",
  "model_canonical": "gpt-5.6-sol",
  "model_source": "self_reported",
  "tool_name": "cptr_code_read_file",
  "input_tokens_estimated": 1200,
  "output_tokens_estimated": 120,
  "cached_input_tokens_estimated": null,
  "estimator_method": "...",
  "estimator_exact_for_model": false,
  "status": "complete"
}
```

`status` is `complete` or `error`. Failed tools still emit usage because the safe MCP error result is returned to the model and can consume input tokens.

The Diagnostics schema remains `extra="forbid"`; no raw prompts, tool arguments, tool results, authorization material, cookies, reasoning, or stack traces are accepted in usage telemetry.

### Server-side cost projection

On ingest/snapshot projection, the Computer backend resolves the price entry and computes:

```text
input_cost_usd  = input_tokens_estimated  / 1_000_000 * input_rate
output_cost_usd = output_tokens_estimated / 1_000_000 * output_rate
total_cost_usd  = input_cost_usd + output_cost_usd
```

Cached cost is not added because cached-token usage is unknown.

Cost math uses decimal-safe arithmetic internally and serializes bounded decimal numbers. The UI may format very small values with sufficient precision (for example `$0.000042`) instead of rounding everything to `$0.00`.

## Bounded state and aggregation

The Diagnostics store adds a bounded usage ring. Default capacity is 500 terminal usage events, configurable through a bounded environment variable.

Snapshot data includes:

- recent usage events;
- model state from the latest retained usage event; if that event omitted `client_model`, the current UI state is **Model not reported** rather than a previous sticky model;
- cumulative estimates over the retained window only: input tokens, output tokens, total tokens, simulated USD;
- totals grouped by canonical model;
- stream-health capacity/drop counters.

Incremental SSE uses `event: usage` and the existing independent Diagnostics stream/reconnect behavior.

A rolling frontend projection produces 5-second buckets for the latest 60 seconds, matching the existing request chart. It never accumulates unbounded browser history.

## `/mcp` UI

Topology remains the primary view. Add a compact **Model usage & simulated cost** analytics section adjacent to the existing Live request statistics without obscuring the topology.

### Summary cards

Show:

- **Current model** — latest self-reported canonical/display model and `Self-reported` badge;
- **Estimated input** — MCP tool-result tokens fed back to the model;
- **Estimated output** — model-generated CPTR tool-call argument tokens;
- **Estimated total**;
- **Simulated cost (USD)**;
- **Avg simulated cost/request**;
- **Pricing status** — current / stale / unknown model.

### Token chart

Responsive 60-second time-series chart:

- Input tokens;
- Output tokens;
- 5-second buckets;
- same bounded inline SVG approach as the existing request success/failure chart;
- accessible summary/labels;
- no new browser charting dependency.

### Cost chart

Responsive 60-second time-series chart:

- simulated USD per 5-second bucket;
- cumulative retained-window simulated cost shown alongside it;
- enough decimal precision to make low MCP cost visible;
- model changes remain individually priced by the model reported on each event.

### Pricing detail

A collapsible pricing block shows the active model's exact registry rates per million input/cached/output tokens, registry version, verification date, source, and freshness status.

Persistent copy directly below the analytics says:

> API-equivalent simulation from MCP-visible estimated tokens. Not your ChatGPT bill. Full prompt context, reasoning, cache usage, and final-answer tokens are not visible to MCP.

## Client/model behavior

- ChatGPT: instructed to self-report `client_model` on every call.
- Claude/Gemini/Inspector: field remains optional; if they voluntarily report a recognizable model, usage may be estimated and priced only when the server registry has an exact unambiguous rate.
- Missing model: token counts may still be shown; simulated cost is unavailable.
- Model changes within one MCP session are accepted and priced per event. No sticky model assumption.

## Privacy and security

The feature does not widen existing telemetry privacy boundaries.

- Raw arguments/results remain confined to the adapter's existing bounded handling.
- Only integer token estimates, bounded model metadata, tool/correlation identifiers, estimator metadata, and terminal status cross the usage channel.
- No hidden reasoning or chain-of-thought is requested or stored.
- `client_model` is untrusted input and is normalized/sanitized before display or lookup.
- Unknown model text can never choose a price by fuzzy substring matching.
- Usage telemetry delivery remains failure-isolated from actual MCP tool execution.

## Error behavior

Usage simulation must never break tool execution.

- tokenizer error -> tool still succeeds/fails normally; usage event may be absent and diagnostics records the telemetry-delivery/estimator issue only if safely representable;
- missing model -> token estimate retained, cost null;
- unknown model -> token estimate retained, cost null, `unknown_model`;
- stale price -> last-known rate displayed as stale; current-cost claim suppressed;
- diagnostics queue overflow -> bounded drop counter increments; no backpressure into MCP execution;
- malformed usage event -> strict backend validation rejects it without affecting Traffic or Activity.

## Testing

### Plugin

TDD must prove:

- server instructions include the per-call current-model reporting rule;
- every registered tool schema exposes optional bounded `client_model`;
- wrapper strips `client_model` before the existing handler/backend payload;
- Activity input remains free of the metadata field;
- token estimation counts full model-generated tool arguments and returned result/error independently;
- self-reported normalization is deterministic and does not guess unknown models;
- one terminal usage event is emitted per completed/failed tool call;
- correlation/request/session IDs match Traffic/Activity/Diagnostics;
- usage delivery failures never fail the tool call;
- existing action/tool count and host-classification contract are unchanged.

### Computer backend

TDD must prove:

- strict usage schema and bounded storage;
- pricing alias normalization;
- exact decimal cost formula at registry rates;
- unknown/stale pricing behavior;
- no cached-token cost is inferred;
- usage snapshot/SSE are admin-only while plugin ingestion uses the existing diagnostics writer scope;
- privacy model rejects raw payload fields;
- ring/subscriber bounds remain enforced.

### Frontend

Regression tests must prove:

- model/cost summary copy is explicitly simulated/estimated;
- input/output token semantics are correct;
- token and cost 60-second charts are bounded and responsive;
- current model and price-source/freshness details render;
- missing/unknown/stale model states are explicit;
- mobile 390x844 layout does not introduce horizontal overflow;
- existing request success/failure, topology, backend monitor, diagnostics, and Console behavior remain intact.

### Acceptance

A disposable real MCP SDK session must invoke a CPTR tool with `client_model: "GPT-5.6 Sol"` and prove one shared correlation produces Traffic, Activity, Diagnostics latency, and Usage events without raw secret payloads.

Final release gates include plugin unit/typecheck/build/live acceptance, Computer focused backend tests, complete MCP frontend regression suite, zero-warning `build:clean`, desktop browser inspection, and exact 390x844 browser inspection.

## Release strategy

This feature spans both repositories:

1. finish/review the already-isolated plugin diagnostics instrumentation lane rather than discarding verified work;
2. layer model reporting and usage estimation on that plugin contract;
3. add the usage/pricing projection and UI in the clean Computer worktree;
4. verify both repositories independently and together;
5. commit/push only after the combined acceptance gates are green;
6. preserve unrelated dirty primary checkouts and unrelated `heidi-cli-stage2` workers.

No production deployment is implied by this design unless separately requested.
