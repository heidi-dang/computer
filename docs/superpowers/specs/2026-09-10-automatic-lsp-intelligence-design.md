# Automatic LSP Intelligence Design

## Goal

Make LSP a transparent CPTR backend capability rather than a model-visible MCP tool. Existing coding operations should automatically use warm language servers when useful, so ChatGPT receives compiler-grade context and post-edit diagnostics without extra MCP round trips.

## Scope

- Backend repository `computer`: add a workspace-scoped automatic LSP intelligence service built on the existing `LspManager`.
- Existing direct-coding file operations remain the public contract. Their responses may include bounded `intelligence` metadata; callers do not need to start/request/stop LSP sessions.
- Existing backend LSP Control API routes remain available for operator/debug compatibility, but normal coding paths must not depend on them.
- Plugin repository `chatgpt-computer-plugin`: remove LSP from the MCP-exposed compact and legacy tool surfaces so it no longer consumes model tool-selection or round-trip budget.
- FDX remains the repository/build/semantic intelligence layer. LSP is the live language-service layer. Neither replaces the other.

## Architecture

### AutomaticLspIntelligenceService

Create `cptr/services/automatic_lsp_intelligence.py` with a singleton service. It maps supported extensions to administrator-controlled server IDs, caches one automatic session per `(user_id, workspace_id, resolved_root, server_id)`, and reuses the existing `LspManager` for process ownership and JSON-RPC safety.

Automatic sessions are best-effort and latency-bounded. A coding operation must never fail because LSP is unavailable, warming, or slow. Cold startup is initiated automatically; expensive enrichment is bounded by a small per-operation latency budget. Warm sessions are reused, idle sessions are reaped, and all sessions are stopped during backend shutdown through the existing `LspManager.shutdown_all()` path.

### Document synchronization

The service tracks document versions per automatic session. For supported text files it sends `textDocument/didOpen` once and `textDocument/didChange` on later versions. Language IDs are derived from file extension; callers never choose an executable or arbitrary language server.

`LspManager` is extended to capture bounded `textDocument/publishDiagnostics` notifications by URI. Server-initiated edits continue to be rejected.

### Read enrichment

`coding/read` and `coding/read-many` automatically attempt warm LSP enrichment for supported source files. Returned metadata is bounded and may contain:

- provider/server/status
- document symbols
- latest diagnostics

For a narrow selected line range, the service may also request hover at the first non-whitespace position. Broad workspace symbol scans are deliberately excluded from automatic reads because they can exceed the latency budget.

### Edit/write enrichment

`coding/write`, `coding/edit`, and `coding/apply-edits` synchronize the updated document automatically. They return bounded post-edit diagnostics/symbol metadata when available inside the latency budget. LSP failure never rolls back an otherwise successful filesystem mutation.

For edits with a unique target, the service may capture bounded pre-edit hover/definition/reference context at the target position when the session is already warm. It must not force a cold startup on the mutation critical path.

### Latency and failure behavior

- Automatic intelligence is controlled by backend configuration and defaults enabled.
- Warm-path requests use a strict bounded budget; timeouts degrade to status metadata instead of HTTP failure.
- Cold starts are scheduled automatically and may return `warming` on the first operation.
- No arbitrary executable, path escape, or server command is accepted from the coding request.
- Response intelligence is size/count bounded.

### MCP surface

The plugin removes `cptr_lsp` and legacy `cptr_lsp_discover/start/request/stop` registrations from its advertised MCP tools and deployed-contract expectations. The backend Control API remains for internal/operator compatibility, but ChatGPT should no longer need or see an LSP MCP tool.

## Success criteria

1. Normal `cptr_code` read/edit/write calls automatically use LSP when the file language is supported.
2. Warm LSP sessions are reused across calls and documents remain synchronized after writes.
3. Post-edit diagnostics can be returned without a separate LSP MCP call.
4. LSP unavailable/timeout never makes a valid coding read/write fail.
5. No new MCP tool is added; existing LSP MCP tool exposure is removed from the plugin surface.
6. FDX behavior is unchanged and remains compatible.
7. Backend targeted tests, full backend suite, plugin tests/typecheck/build, and deployed-contract checks pass.
8. Tool-count reduction is verified from the plugin MCP server contract.
