# Designing Real UI Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a discoverable CPTR workspace skill that forces real backend/API/live-state verification before visual UI implementation while preserving the existing product theme.

**Architecture:** Store the skill under CPTR's existing `.cptr/skills` discovery path. Keep the activation workflow concise in `SKILL.md`; put the reusable UI-to-backend ledger in one reference file. Add a repository test that validates discovery metadata and the non-negotiable real-functionality clauses.

**Tech Stack:** CPTR managed Skills format, Markdown/YAML frontmatter, Python/pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-designing-real-ui-skill-design.md`

## Global Constraints

- No visible functional affordance without a verified backend/API/live-state contract.
- Preserve and extend the existing backend UI theme and design tokens.
- Runtime-looking animation must be driven by runtime state; decorative motion must remain clearly decorative.
- Loading, empty, error, stale/offline, reconnect, cancellation/release, and recovery states are part of the feature contract when applicable.
- Verify responsive, accessibility, reduced-motion, API behavior, live transports, console/network health, tests, typecheck, and production build as relevant.

---

### Task 1: Regression contract

**Files:**
- Create: `tests/test_designing_real_ui_skill.py`

**Interfaces:**
- Consumes: CPTR skill discovery conventions from `cptr/utils/skills.py`.
- Produces: executable assertions for frontmatter, reference asset, and mandatory UI/backend contract language.

- [ ] **Step 1: Write the failing test**

Create a test that expects `.cptr/skills/designing-real-ui/SKILL.md` and `references/ui-feature-contract.md`, validates frontmatter name/description/version, and asserts the skill contains requirements for feature-contract mapping, verified backend behavior, existing theme preservation, live-state truthfulness, reduced motion, and browser/API verification.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_designing_real_ui_skill.py`

Expected: FAIL because the skill does not exist yet.

### Task 2: Skill implementation

**Files:**
- Create: `.cptr/skills/designing-real-ui/SKILL.md`
- Create: `.cptr/skills/designing-real-ui/references/ui-feature-contract.md`

**Interfaces:**
- Consumes: CPTR skill discovery/parser and actual frontend/backend contracts found during UI work.
- Produces: reusable UI design procedure and feature-contract verification ledger.

- [ ] **Step 1: Write minimal skill content**

Follow CPTR frontmatter requirements and the approved design. Require repository/API/theme inspection before visual implementation and fail closed on unverified controls.

- [ ] **Step 2: Add the reusable feature-contract ledger**

Include fields for UI element, backend authority, request/response, transport, lifecycle states, authorization, verification evidence, and disposition.

- [ ] **Step 3: Run the regression test**

Run: `pytest -q tests/test_designing_real_ui_skill.py`

Expected: PASS.

### Task 3: Discovery and quality verification

**Files:**
- Verify only; modify prior files only if evidence identifies a defect.

**Interfaces:**
- Consumes: `cptr.utils.skills.discover_skills`, `load_skill`, and `format_skill_content`.
- Produces: evidence that CPTR can discover and activate the skill.

- [ ] **Step 1: Run focused skill-system tests**

Run: `pytest -q tests/test_designing_real_ui_skill.py tests/test_control_api.py -q`

- [ ] **Step 2: Verify discovery using production code**

Run a Python check through the repository environment that resolves the repository root, calls `discover_skills`, loads `designing-real-ui`, checks its reference asset, and formats the activation context.

- [ ] **Step 3: Inspect git diff and validate no unrelated changes**

Check status/diff for only the design spec, plan, test, skill, and reference asset.

- [ ] **Step 4: Commit and push**

Commit the verified skill implementation on a feature branch and push it. Open/update a PR if repository tooling and credentials permit. Do not merge or deploy without explicit user authorization.
