# Automatic LSP Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LSP transparent, warm, and automatic inside CPTR coding operations while removing LSP from the model-visible MCP tool surface.

**Architecture:** Add a backend `AutomaticLspIntelligenceService` that reuses `LspManager`, keeps bounded warm sessions per workspace/root/language, synchronizes documents, and enriches existing read/write/edit responses. Extend `LspManager` only for bounded diagnostic-notification capture. Then remove LSP registrations from the plugin MCP surface while retaining backend Control API compatibility.

**Tech Stack:** Python 3.10+, FastAPI, asyncio, LSP JSON-RPC, pytest/unittest; TypeScript/Node MCP server, Zod, Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-10-automatic-lsp-intelligence-design.md`

## Global Constraints

- Do not add an MCP-exposed LSP tool.
- Automatic LSP failure/timeout must never fail a valid coding filesystem operation.
- Use only administrator-controlled server IDs and workspace-confined roots.
- Keep response payloads and latency bounded.
- Preserve FDX behavior and backend LSP Control API compatibility.
- No new runtime dependency is required.

---

### Task 1: Capture LSP diagnostics safely

**Files:**
- Modify: `cptr/services/lsp_manager.py`
- Test: `tests/test_terminal_parity.py`

**Interfaces:**
- Produce: `LspManager.latest_diagnostics(lsp_id: str, user_id: str, uri: str) -> list[dict[str, Any]]`
- Produce: bounded `textDocument/publishDiagnostics` capture inside `_reader_loop`.

- [ ] Add a failing test using a fake server that emits `publishDiagnostics` after `didOpen` and assert the manager captures the bounded diagnostics.
- [ ] Run the focused test and confirm RED.
- [ ] Add per-session diagnostic storage, notification handling, count bounds, and `latest_diagnostics`.
- [ ] Run focused tests and confirm GREEN.

### Task 2: Add automatic warm-session intelligence service

**Files:**
- Create: `cptr/services/automatic_lsp_intelligence.py`
- Test: `tests/test_automatic_lsp_intelligence.py`

**Interfaces:**
- Produce: `service.enrich_read(...) -> dict[str, Any] | None`
- Produce: `service.enrich_after_write(...) -> dict[str, Any] | None`
- Produce: `service.prefetch(...) -> None`
- Produce: `service.close_all() -> None`

- [ ] Write tests for extension→server mapping, warm-session reuse, didOpen→didChange versioning, unavailable server degradation, timeout degradation, and bounded symbols/diagnostics.
- [ ] Run tests and confirm RED.
- [ ] Implement the service with automatic session caching keyed by user/workspace/root/server, a short warm-path budget, non-fatal cold startup, document version state, idle reaping, and bounded structured results.
- [ ] Run tests and confirm GREEN.

### Task 3: Integrate automatic intelligence into existing coding operations

**Files:**
- Modify: `cptr/routers/coding.py`
- Test: `tests/test_direct_coding.py`
- Test: `tests/test_backend_performance.py`

**Interfaces:**
- Existing `coding/read`, `coding/read-many`, `coding/write`, `coding/edit`, and `coding/apply-edits` responses gain optional `intelligence` metadata.

- [ ] Add failing route tests proving read and edit/write automatically call the service while preserving normal responses when the service returns unavailable/timeout metadata or raises a handled LSP degradation.
- [ ] Add a performance regression test proving disabled/unavailable automatic intelligence does not add an extra external tool call and remains bounded.
- [ ] Implement identity/env resolution helper and automatic enrichment hooks after successful reads/writes; do not place LSP on filesystem correctness critical paths.
- [ ] Run focused tests and confirm GREEN.

### Task 4: Backend lifecycle and configuration

**Files:**
- Modify: `cptr/env.py`
- Modify: `cptr/app.py`
- Test: `tests/test_execution_environment.py`
- Test: `tests/test_automatic_lsp_intelligence.py`

**Interfaces:**
- Add backend configuration for enable flag, warm latency budget, idle TTL, result bounds with safe defaults.
- Backend shutdown closes automatic service state before/with `LspManager.shutdown_all()`.

- [ ] Add failing configuration/lifecycle tests.
- [ ] Implement environment parsing with bounded numeric values and enabled-by-default behavior.
- [ ] Add shutdown cleanup.
- [ ] Run focused tests and confirm GREEN.

### Task 5: Remove LSP from MCP tool surface

**Files:**
- Modify plugin: `server/mcp.ts`
- Modify plugin: `server/index.ts` if special routing remains.
- Modify plugin: `server/schemas/tools.ts` only if schemas become unused.
- Modify plugin: `tests/mcp.test.ts`
- Modify plugin: `tests/mcp-compact.test.ts`
- Modify plugin: `tests/deployed-contract-script.test.ts`
- Modify plugin: `scripts/check-deployed-contract.mjs`
- Modify plugin terminal-parity tests only where they assert MCP-exposed LSP.

**Interfaces:**
- `cptr_lsp` and legacy `cptr_lsp_discover/start/request/stop` are absent from advertised MCP tools.
- `ComputerClient` backend LSP methods may remain as non-advertised compatibility helpers unless dead-code checks require removal.

- [ ] Update tests first to assert LSP tools are absent and compact tool count decreases.
- [ ] Run plugin tests and confirm RED.
- [ ] Remove compact and legacy LSP registrations/help/dispatch entries from MCP registration while preserving unrelated tools.
- [ ] Run plugin unit/type/build/deployed-contract tests and confirm GREEN.

### Task 6: Exact final qualification

**Files:** no feature changes expected.

- [ ] Backend Ruff + format + `git diff --check`.
- [ ] Backend focused automatic-LSP/LSP/direct-coding tests.
- [ ] Backend full pytest suite with isolated initialized `CPTR_DATA_DIR`.
- [ ] Frontend production check/build if backend release packaging requires it.
- [ ] Plugin lint/typecheck/tests/build/deployed-contract checks.
- [ ] Verify plugin tool listing contains no LSP tool and backend existing `cptr_code` operations return automatic intelligence with real Pyright/TypeScript servers.
- [ ] Verify FDX index/build/semantic health is not regressed.
- [ ] Commit scoped backend and plugin changes on isolated branches, push, and open PRs. Do not merge or deploy without explicit authorization.
