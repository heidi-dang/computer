---
name: Visual regression surface checks
description: Guidance for stable browser coverage of CPTR Tools and Shell UI surfaces.
---

Pixel snapshots are appropriate for static Tools, Tool Servers, and tool-call content, while the live xterm terminal should use layout and affordance assertions rather than a pixel baseline.

**Why:** PTY reconnects, cursor state, and renderer timing can change a small number of terminal pixels even when the layout is correct.

**How to apply:** Keep terminal desktop/narrow overflow and visible-status checks in the browser suite; reserve pixel snapshots for deterministic surrounding surfaces.