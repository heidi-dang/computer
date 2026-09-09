"""Raw sqlite3 schema tests for 0035 Capability OS experiments/promotions/lineage tables."""

import sqlite3
import time
import uuid
import pytest


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now():
    return time.time()


def _id():
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Schema DDL (mirrors migration 0035)
# ---------------------------------------------------------------------------

EXPERIMENTS_DDL = """
CREATE TABLE capability_os_experiments (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    artifact_id TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    status TEXT NOT NULL,
    traffic_split REAL NOT NULL DEFAULT 0.5,
    owner TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL,
    concluded_at REAL
)
"""

EXPERIMENT_RUNS_DDL = """
CREATE TABLE capability_os_experiment_runs (
    id TEXT PRIMARY KEY NOT NULL,
    experiment_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    latency_ms REAL,
    success INTEGER,
    error_message TEXT,
    created_at REAL NOT NULL,
    completed_at REAL
)
"""

PROMOTIONS_DDL = """
CREATE TABLE capability_os_promotions (
    id TEXT PRIMARY KEY NOT NULL,
    artifact_id TEXT NOT NULL,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    from_stage TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    experiment_id TEXT,
    promoted_by TEXT NOT NULL,
    reason TEXT,
    policy_decision_id TEXT,
    created_at REAL NOT NULL
)
"""

LINEAGE_EDGES_DDL = """
CREATE TABLE capability_os_lineage_edges (
    id TEXT PRIMARY KEY NOT NULL,
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    metadata_json TEXT,
    created_at REAL NOT NULL
)
"""

POLICY_DECISIONS_DDL = """
CREATE TABLE capability_os_policy_decisions (
    id TEXT PRIMARY KEY NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    subject TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    context_json TEXT,
    latency_ms REAL,
    created_at REAL NOT NULL
)
"""

OBSERVATIONS_DDL = """
CREATE TABLE capability_os_observations (
    id TEXT PRIMARY KEY NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_version TEXT NOT NULL,
    task_id TEXT,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    unit TEXT,
    tags_json TEXT,
    source TEXT,
    count INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_capability_os_experiments():
    conn = _db()
    conn.execute(EXPERIMENTS_DDL)
    row_id = _id()
    conn.execute(
        "INSERT INTO capability_os_experiments "
        "(id, name, artifact_id, baseline_version, candidate_version, status, owner, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, "exp-1", "art-abc", "1.0", "1.1", "running", "alice", _now()),
    )
    rows = conn.execute("SELECT id, name, status FROM capability_os_experiments").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == row_id
    assert rows[0][1] == "exp-1"
    assert rows[0][2] == "running"
    conn.close()


def test_capability_os_experiment_runs():
    conn = _db()
    conn.execute(EXPERIMENT_RUNS_DDL)
    row_id = _id()
    conn.execute(
        "INSERT INTO capability_os_experiment_runs "
        "(id, experiment_id, variant, status, created_at, latency_ms, success) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row_id, "exp-1", "candidate", "completed", _now(), 42.5, 1),
    )
    rows = conn.execute(
        "SELECT id, variant, latency_ms, success FROM capability_os_experiment_runs"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == row_id
    assert rows[0][1] == "candidate"
    assert rows[0][2] == pytest.approx(42.5)
    assert rows[0][3] == 1
    conn.close()


def test_capability_os_promotions():
    conn = _db()
    conn.execute(PROMOTIONS_DDL)
    row_id = _id()
    conn.execute(
        "INSERT INTO capability_os_promotions "
        "(id, artifact_id, from_version, to_version, from_stage, to_stage, promoted_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, "art-abc", "1.0", "1.1", "staging", "prod", "bob", _now()),
    )
    rows = conn.execute(
        "SELECT id, from_stage, to_stage FROM capability_os_promotions"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == row_id
    assert rows[0][1] == "staging"
    assert rows[0][2] == "prod"
    conn.close()


def test_capability_os_lineage_edges():
    conn = _db()
    conn.execute(LINEAGE_EDGES_DDL)
    row_id = _id()
    conn.execute(
        "INSERT INTO capability_os_lineage_edges "
        "(id, parent_id, child_id, edge_type, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (row_id, "art-parent", "art-child", "derived", _now()),
    )
    rows = conn.execute(
        "SELECT id, parent_id, child_id, edge_type FROM capability_os_lineage_edges"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == row_id
    assert rows[0][1] == "art-parent"
    assert rows[0][2] == "art-child"
    assert rows[0][3] == "derived"
    conn.close()


def test_capability_os_policy_decisions():
    conn = _db()
    conn.execute(POLICY_DECISIONS_DDL)
    row_id = _id()
    conn.execute(
        "INSERT INTO capability_os_policy_decisions "
        "(id, policy_id, policy_version, subject, action, decision, created_at, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, "pol-1", "v2", "user:alice", "promote", "allow", _now(), 3.2),
    )
    rows = conn.execute(
        "SELECT id, decision, latency_ms FROM capability_os_policy_decisions"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == row_id
    assert rows[0][1] == "allow"
    assert rows[0][2] == pytest.approx(3.2)
    conn.close()


def test_capability_os_observations():
    conn = _db()
    conn.execute(OBSERVATIONS_DDL)
    row_id = _id()
    conn.execute(
        "INSERT INTO capability_os_observations "
        "(id, artifact_id, artifact_version, metric_name, metric_value, created_at, count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row_id, "art-abc", "1.1", "p99_latency_ms", 120.5, _now(), 5),
    )
    rows = conn.execute(
        "SELECT id, metric_name, metric_value, count FROM capability_os_observations"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == row_id
    assert rows[0][1] == "p99_latency_ms"
    assert rows[0][2] == pytest.approx(120.5)
    assert rows[0][3] == 5
    conn.close()
