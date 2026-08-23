"""File metadata model: tracks uploaded blobs in the DB."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, ForeignKey, Text, select, delete
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base
from cptr.utils.db import get_db


def _uuid() -> str:
    return str(uuid.uuid4())


class File(Base):
    """One row per uploaded blob. Storage key == id (UUID)."""

    __tablename__ = "files"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, ForeignKey("users.id"), nullable=True)
    filename = Column(Text, nullable=False)
    path = Column(Text, nullable=False, default="")
    hash = Column(Text, nullable=False, default="")
    meta = Column(JSON, nullable=False, default=dict)  # content_type, size, etc.
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=True)

    # ── Class methods ────────────────────────────────────────

    @staticmethod
    async def create(*, user_id: str | None, filename: str, meta: dict, created_at: int) -> File:
        """Insert a new file record and return it."""
        async with await get_db() as db:
            f = File(
                user_id=user_id,
                filename=filename,
                meta=meta,
                created_at=created_at,
            )
            db.add(f)
            await db.commit()
            await db.refresh(f)
            return f

    @staticmethod
    async def get_by_id(file_id: str) -> File | None:
        async with await get_db() as db:
            result = await db.execute(select(File).where(File.id == file_id))
            return result.scalar_one_or_none()

    @staticmethod
    async def delete_by_id(file_id: str) -> bool:
        """Delete a file record. Returns True if it existed."""
        async with await get_db() as db:
            result = await db.execute(delete(File).where(File.id == file_id))
            await db.commit()
            return result.rowcount > 0
