"""Chain Capability OS evidence per task.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def _json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unsupported evidence migration value: {value.__class__.__name__}")


def _digest(payload) -> str:
    raw = json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def upgrade() -> None:
    op.add_column(
        "capability_os_evidence",
        sa.Column("sequence", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "capability_os_evidence",
        sa.Column("previous_digest", sa.Text(), nullable=True),
    )

    bind = op.get_bind()
    evidence = sa.table(
        "capability_os_evidence",
        sa.column("evidence_id", sa.Text()),
        sa.column("task_id", sa.Text()),
        sa.column("run_id", sa.Text()),
        sa.column("kind", sa.Text()),
        sa.column("producer_identity", sa.Text()),
        sa.column("claims", sa.JSON()),
        sa.column("blob_uri", sa.Text()),
        sa.column("artifact_digest", sa.Text()),
        sa.column("lease_id", sa.Text()),
        sa.column("created_at_ms", sa.BigInteger()),
        sa.column("sequence", sa.BigInteger()),
        sa.column("previous_digest", sa.Text()),
        sa.column("digest", sa.Text()),
    )
    task_ids = [
        row[0]
        for row in bind.execute(
            sa.select(evidence.c.task_id).distinct().order_by(evidence.c.task_id)
        ).all()
    ]
    for task_id in task_ids:
        previous_digest = None
        rows = bind.execute(
            sa.select(
                evidence.c.evidence_id,
                evidence.c.task_id,
                evidence.c.run_id,
                evidence.c.kind,
                evidence.c.producer_identity,
                evidence.c.claims,
                evidence.c.blob_uri,
                evidence.c.artifact_digest,
                evidence.c.lease_id,
                evidence.c.created_at_ms,
            )
            .where(evidence.c.task_id == task_id)
            .order_by(evidence.c.created_at_ms, evidence.c.evidence_id)
        ).mappings().all()
        for sequence, row in enumerate(rows, start=1):
            claims = row["claims"]
            if isinstance(claims, str):
                claims = json.loads(claims)
            claims = dict(claims or {})
            digest = _digest(
                {
                    "task_id": row["task_id"],
                    "sequence": sequence,
                    "previous_digest": previous_digest,
                    "run_id": row["run_id"],
                    "kind": row["kind"],
                    "producer_identity": row["producer_identity"],
                    "claims": claims,
                    "blob_uri": row["blob_uri"],
                    "artifact_digest": row["artifact_digest"],
                    "lease_id": row["lease_id"],
                    "created_at_ms": int(row["created_at_ms"]),
                }
            )
            bind.execute(
                evidence.update()
                .where(evidence.c.evidence_id == row["evidence_id"])
                .values(
                    sequence=sequence,
                    previous_digest=previous_digest,
                    digest=digest,
                )
            )
            previous_digest = digest

    with op.batch_alter_table("capability_os_evidence") as batch:
        batch.alter_column("sequence", existing_type=sa.BigInteger(), nullable=False)
        batch.create_unique_constraint(
            "uq_capability_os_evidence_task_sequence",
            ["task_id", "sequence"],
        )


def downgrade() -> None:
    with op.batch_alter_table("capability_os_evidence") as batch:
        batch.drop_constraint(
            "uq_capability_os_evidence_task_sequence",
            type_="unique",
        )
        batch.drop_column("previous_digest")
        batch.drop_column("sequence")
