# FlowDeck shadow regression matrix

The CPTR artifact did not ship a request-test suite when FlowDeck shadow mode
was introduced. `tests/test_flowdeck.py` therefore provides a deterministic
request-level oracle around the one supported integration boundary:

```text
native CPTR request → optional observe_request → native CPTR operation
```

The matrix covers:

| Pathway | Contract checked |
| --- | --- |
| Authenticated chat creation | Response and native task ownership |
| Queued follow-up | Queue response and native continuation ownership |
| Cancellation | Cancellation result and authoritative state |
| Tool approval | Approval/tool calls remain CPTR-owned |
| Socket.IO streaming | Authoritative `events:chat` remains unchanged |
| External-agent selection | Provider selection remains unchanged |
| Restart reconciliation | Reconciliation state remains unchanged |
| Files, terminals, Git | Filesystem and command effects remain unchanged |
| Browser | Browser/provider effects remain unchanged |
| MCP | MCP/tool effects remain unchanged |

Each pathway is executed with FlowDeck disabled and with
`CPTR_FLOWDECK_MODE=shadow`. The snapshots compare response data, provider
calls, tool calls, filesystem state, and authoritative events. A separate
failure test verifies that a diagnostic exception is discarded and cannot
change the native result.

## Baseline failures versus regressions

Run the matrix from the CPTR artifact directory:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Failures in the disabled configuration are baseline failures and must be
recorded separately before interpreting shadow results. A failure that appears
only in the shadow comparison is a FlowDeck regression. The matrix intentionally
does not turn known baseline failures into passing tests or silently skip them.