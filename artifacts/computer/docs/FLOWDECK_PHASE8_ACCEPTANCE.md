# FlowDeck Phase 8 Browser Self-Testing Acceptance

Status: **qualified and frozen**
Score: **9.0/10**

Phase 8 extends the existing authenticated `browser-debugger` specialist. It
does not add an agent framework, model loop, or alternate execution authority.
CPTR remains the sole native browser/tool execution authority and Heidi/
FlowDeck remains the orchestration and lifecycle authority.

## Covered behavior

- Local-preview-only navigation with authenticated workspace context.
- Qualified accessibility-tree snapshots with replayable element references.
- Exact click/type interaction replay through the existing CDP client.
- DOM/accessibility inspection through bounded `browser_evaluate` expressions.
- Screenshot capture at explicit viewport dimensions.
- Performance-resource inspection for network evidence.
- Console-visible page-error inspection guidance in the native specialist
  protocol.
- Primary-flow smoke testing after builds.
- Failure tracing to the first incorrect state and bounded repair-loop guidance.
- UNKNOWN when browser, preview, or inspection evidence cannot be verified.
- Durable native child-run evidence, cancellation, reconnect, recovery, and
  transcript integration through existing FlowDeck coding lifecycle paths.
- Adversarial rejection of shell, filesystem mutation, Git, secrets, external
  navigation, storage/network JavaScript, delegation, install, deploy, and
  publish capabilities.

## Verification

- Full backend regression: **236 passed**, 43 subtests.
- Browser/coding/gateway/HTTP authority suite: passed.
- Ruff, Python compilation, and diff checks: passed.
- Frontend svelte-check: **0 errors** (existing warning set only).
- Frontend production build: passed.
- Visual regression: **16 passed** at desktop and narrow/mobile widths.
- Browser-tool policy tests verify local-only navigation, interaction
  allowance, bounded DOM evaluation, and network/storage expression rejection.
- Authenticated FlowDeck HTTP tests verify ownership, durable runs,
  cancellation, reconnect, native transcript paths, and stale/duplicate
  protections.
- API and web workflows restarted successfully and remain running.

No unrestricted shell, MCP, FDX, deployment, publishing, GitHub push,
credential rotation, DNS change, or destructive external operation was used.
Phase 9 has not been started.