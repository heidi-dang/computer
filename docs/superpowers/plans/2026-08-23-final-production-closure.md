# Final Production Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the production closure by strengthening configuration-driven independent verification, splitting autonomous MCP operations into accurately annotated tools, verifying both repositories, disconnecting or precisely reporting the remaining external Vercel links, and pushing only verified feature-branch commits.

**Architecture:** `computer` remains the execution and supervision authority. Its verifier will consume a typed, category-aware argv plan while preserving bounded command evidence and the existing diagnose/repair/re-verify state machine. `chatgpt-computer-plugin` will keep monitor creation as a write tool and expose separate read, steering, cancellation, and approval tools that each route to the existing CPTR endpoints with operation-specific MCP annotations.

**Tech Stack:** Python 3.14, unittest/pytest, FastAPI CPTR Control API, TypeScript, Node MCP SDK, Zod, npm scripts, GitHub CLI, authenticated Vercel/GitHub integration APIs when available.

**Spec:** `/home/shacker/.codex/attachments/67fb8d35-7a88-495e-9de8-921f40000908/pasted-text.txt`

## Global Constraints

- Work only in `/home/shacker/Desktop/chatgpt-computer-plugin/computer` and `/home/shacker/Desktop/chatgpt-computer-plugin/chatgpt-computer-plugin`.
- Preserve `computer` branch `feat/chatgpt-control-plane` and plugin branch `feat/initial-mcp-adapter`.
- Do not merge `main`, recreate repositories, or add widgets, broad frontend cleanup, unrelated dependency upgrades, or baseline lint/type cleanup.
- Do not trust worker prose as completion proof; independent commands and durable evidence remain authoritative.
- Do not delete unrelated Vercel projects or account resources.

### Task 1: Verify the existing computer hardening and extend the verifier plan

**Files:**
- Modify: `computer/cptr/services/verification.py`
- Modify: `computer/docs/control-plane.md`
- Test: `computer/tests/test_verification.py`

**Interfaces:**
- `VerificationCommand` gains a configuration category from `focused_tests`, `broader_tests`, `lint`, `typecheck`, `build`, or `runtime_smoke` while retaining `name`, argv, timeout, bounded stdout/stderr, and pass/fail evidence.
- `DefaultIndependentVerifier` executes every configured command in order and reports each command independently; any nonzero, timeout, launch error, or invalid plan fails the verification result.

- [ ] **Step 1: Add failing tests for category validation and evidence.**

  Add tests that load a JSON plan containing one command for each supported category, assert all categories are retained in checks, and assert an unknown category produces a failed configuration check without executing commands.

- [ ] **Step 2: Run the focused verifier tests and confirm the new tests fail.**

  Run:

  ```bash
  cd /home/shacker/Desktop/chatgpt-computer-plugin/computer
  PYTHONPATH=. /home/shacker/.venvs/cptr/bin/python -m unittest tests.test_verification -v
  ```

- [ ] **Step 3: Implement the typed plan category and configuration validation.**

  Parse each item’s `category`, validate it against the six categories, preserve the category in each durable check, and keep subprocess execution argv-only through `asyncio.create_subprocess_exec` with the existing timeout and output bounds.

- [ ] **Step 4: Document the category-aware JSON configuration.**

  Document a plan such as:

  ```json
  [
    {"name":"focused_tests","category":"focused_tests","argv":["python","-m","pytest","tests/test_feature.py"]},
    {"name":"broader_tests","category":"broader_tests","argv":["python","-m","pytest"]},
    {"name":"lint","category":"lint","argv":["ruff","check","."]},
    {"name":"typecheck","category":"typecheck","argv":["mypy","."]},
    {"name":"build","category":"build","argv":["npm","run","build"]},
    {"name":"runtime_smoke","category":"runtime_smoke","argv":["python","-c","print('smoke')"]}
  ]
  ```

  State that commands are never shell-interpolated and that runtime smoke is optional because the plan controls which categories are present.

- [ ] **Step 5: Run the verifier and CPTR regression suites.**

  ```bash
  cd /home/shacker/Desktop/chatgpt-computer-plugin/computer
  PYTHONPATH=. /home/shacker/.venvs/cptr/bin/python -m unittest tests.test_verification -v
  PYTHONPATH=. /home/shacker/.venvs/cptr/bin/python -m unittest discover -s tests -v
  ```

### Task 2: Split autonomous MCP management operations

**Files:**
- Modify: `chatgpt-computer-plugin/server/client/computer-client.ts`
- Modify: `chatgpt-computer-plugin/server/mcp.ts`
- Modify: `chatgpt-computer-plugin/server/schemas/tools.ts`
- Modify: `chatgpt-computer-plugin/README.md`
- Test: `chatgpt-computer-plugin/tests/client.test.ts`
- Test: `chatgpt-computer-plugin/tests/mcp.test.ts`

**Interfaces:**
- Keep `cptr_monitor_autonomous` as creation-only, sending `POST /api/control/v1/autonomous`.
- Add `cptr_get_autonomous` for `GET /autonomous/{monitor_id}`.
- Add `cptr_get_autonomous_events` for `GET /autonomous/{monitor_id}/events`.
- Add `cptr_get_autonomous_evidence` for `GET /autonomous/{monitor_id}/evidence`.
- Add `cptr_steer_autonomous` for `POST /autonomous/{monitor_id}/messages`.
- Add `cptr_cancel_autonomous` for `POST /autonomous/{monitor_id}/cancel`.
- Add `cptr_approve_autonomous` for `POST /autonomous/{monitor_id}/approve`.

- [ ] **Step 1: Add failing client-routing and annotation tests.**

  Assert each dedicated method calls its exact endpoint and HTTP method. Start an MCP server in the test and assert `listTools()` exposes the seven dedicated names with these annotations: read tools read-only/non-destructive; steer write/non-destructive; cancel write/destructive; approve write and non-read-only with documented approval semantics; create write/non-destructive.

- [ ] **Step 2: Run the plugin tests and confirm the new expectations fail against the combined tool.**

  ```bash
  cd /home/shacker/Desktop/chatgpt-computer-plugin/chatgpt-computer-plugin
  npm test
  ```

- [ ] **Step 3: Implement dedicated client methods and Zod schemas.**

  Remove action routing from the public monitor-management client method. Use `monitorIdSchema` for read/cancel/approval identifiers, `content` for steering, and `{approval_id, approved}` for approval. Keep bounded errors and bearer forwarding unchanged.

- [ ] **Step 4: Register the dedicated MCP tools and remove the overloaded management actions.**

  Register each tool with its exact annotations and narrow output schema. `cptr_monitor_autonomous` must reject status/events/evidence/steer/cancel/approve inputs because it only creates.

- [ ] **Step 5: Update README tool documentation and run all plugin checks.**

  ```bash
  cd /home/shacker/Desktop/chatgpt-computer-plugin/chatgpt-computer-plugin
  npm test
  npm run typecheck
  npm run build
  npm audit --audit-level=high
  ```

### Task 3: Audit and handle external Vercel integrations

**Files:**
- Inspect only: both repository tracked files and authenticated integration APIs.
- Modify only if needed: repository-tracked Vercel material in either repository.

**Interfaces:**
- Repository-level GitHub checks/status contexts and Vercel project integration metadata are external state, not repository files.

- [ ] **Step 1: Confirm tracked Vercel material is absent.**

  Run `git ls-files` and targeted searches for Vercel configuration, workflows, dependencies, scripts, environment templates, badges, and deployment hooks in both repositories.

- [ ] **Step 2: Inspect authenticated GitHub repository hooks and checks.**

  Use `gh api repos/heidi-dang/computer/hooks` and repository check/status APIs, recording only hook IDs, names, URLs, and relevant non-secret configuration. Identify the three contexts `Vercel – computer`, `Vercel – computer-g0`, and `Vercel – computer-k4`.

- [ ] **Step 3: Inspect authenticated Vercel projects/integrations without deleting unrelated resources.**

  Use an authenticated Vercel API/CLI only if credentials are present. Disconnect only the repository links responsible for those three contexts. If Vercel authentication is unavailable, record the exact contexts and provide the exact dashboard action: open the matching Vercel project, Settings → Git, disconnect the `heidi-dang/computer` repository, and repeat only for the three named projects.

- [ ] **Step 4: Re-check GitHub status contexts after any permitted disconnection.**

  Do not claim closure unless the three contexts are absent or the exact external blocker/manual action is reported.

### Task 4: Run closure verification, real smoke, commit, and push

**Files:**
- No new product files beyond Tasks 1–2.

- [ ] **Step 1: Verify computer ancestry, diff scope, secrets, and clean state.**

  Confirm `a220f16` is reachable from `feat/chatgpt-control-plane`, inspect its parent/stat/full diff, search committed files for secrets and fixture/runtime artifacts, and require a clean tree before pushing.

- [ ] **Step 2: Run computer regression and focused acceptance checks.**

  Run the full unittest suite, verifier/director-focused tests, and the previously established real MCP → CPTR → AgentService acceptance plus restart-recovery smoke using disposable workspaces. Retrieve status/events/evidence through MCP and require durable `COMPLETE`.

- [ ] **Step 3: Run plugin unit/type/build/audit checks and MCP smoke.**

  Start the real plugin, initialize a real MCP client, inspect `tools/list`, assert dedicated tool names and annotations, then perform a focused real CPTR create/status/evidence/cancel or completion request with a scoped bearer token.

- [ ] **Step 4: Commit only the closure changes.**

  ```bash
  git add <verified computer files>
  git commit -m "feat: split autonomous MCP management tools"
  git add <verified plugin files>
  git commit -m "feat: expose safe autonomous monitor operations"
  ```

- [ ] **Step 5: Push both feature branches and verify remote SHAs.**

  ```bash
  git push origin feat/chatgpt-control-plane
  git push origin feat/initial-mcp-adapter
  git rev-parse HEAD origin/feat/chatgpt-control-plane
  git -C /home/shacker/Desktop/chatgpt-computer-plugin/chatgpt-computer-plugin rev-parse HEAD origin/feat/initial-mcp-adapter
  ```

  Report the exact pushed SHAs, clean-tree results, MCP annotation smoke evidence, real acceptance IDs, and either confirmed Vercel disconnection or the exact manual-action blocker.
