"""Automation and AutomationRun models with data-access class methods."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Boolean, Column, Index, Text, select, update, delete, func
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base
from cptr.utils.db import get_db


def _uuid() -> str:
    return str(uuid.uuid4())


class Automation(Base):
    """A scheduled automation that runs a prompt on a recurring basis."""

    __tablename__ = "automations"

    id = Column(Text, primary_key=True, default=_uuid)
    user_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    prompt = Column(Text, nullable=False)
    model_id = Column(Text, nullable=False)
    workspace = Column(Text, nullable=False)
    rrule = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(BigInteger, nullable=True)
    next_run_at = Column(BigInteger, nullable=True)
    meta = Column(JSON, nullable=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_automation_next_run", "next_run_at"),)

    # ── Class methods ────────────────────────────────────────

    @staticmethod
    async def get_by_id(automation_id: str) -> Automation | None:
        async with await get_db() as db:
            result = await db.execute(select(Automation).where(Automation.id == automation_id))
            return result.scalar_one_or_none()

    @staticmethod
    async def get_all_by_user(user_id: str) -> list[Automation]:
        async with await get_db() as db:
            result = await db.execute(
                select(Automation)
                .where(Automation.user_id == user_id)
                .order_by(Automation.created_at.desc())
            )
            return list(result.scalars().all())

    @staticmethod
    async def get_by_workspace(
        user_id: str,
        workspace: str | None = None,
        status: str | None = None,
        query: str | None = None,
        skip: int = 0,
        limit: int = 30,
    ) -> tuple[list[Automation], int]:
        """List automations with filtering. Returns (items, total)."""
        async with await get_db() as db:
            stmt = select(Automation).where(
                Automation.user_id == user_id,
            )
            if workspace:
                stmt = stmt.where(Automation.workspace == workspace)
            if status == "active":
                stmt = stmt.where(Automation.is_active == True)  # noqa: E712
            elif status == "paused":
                stmt = stmt.where(Automation.is_active == False)  # noqa: E712
            if query:
                search = f"%{query}%"
                stmt = stmt.where(Automation.name.ilike(search))

            # Count
            count_result = await db.execute(
                select(func.count()).select_from(stmt.subquery())
            )
            total = count_result.scalar() or 0

            # Page
            stmt = stmt.order_by(Automation.created_at.desc()).offset(skip).limit(limit)
            result = await db.execute(stmt)
            return list(result.scalars().all()), total

    @staticmethod
    async def create(
        user_id: str,
        name: str,
        prompt: str,
        model_id: str,
        workspace: str,
        rrule: str,
        next_run_at: int | None,
        is_active: bool = True,
        meta: dict | None = None,
        created_at: int = 0,
    ) -> Automation:
        async with await get_db() as db:
            automation = Automation(
                user_id=user_id,
                name=name,
                prompt=prompt,
                model_id=model_id,
                workspace=workspace,
                rrule=rrule,
                is_active=is_active,
                next_run_at=next_run_at,
                meta=meta,
                created_at=created_at,
                updated_at=created_at,
            )
            db.add(automation)
            await db.commit()
            await db.refresh(automation)
            return automation

    @staticmethod
    async def update_by_id(automation_id: str, updated_at: int = 0, **kwargs) -> bool:
        if not kwargs:
            return False
        kwargs["updated_at"] = updated_at
        async with await get_db() as db:
            result = await db.execute(
                update(Automation).where(Automation.id == automation_id).values(**kwargs)
            )
            await db.commit()
            return result.rowcount > 0

    @staticmethod
    async def toggle(automation_id: str, next_run_at: int | None, updated_at: int = 0) -> Automation | None:
        async with await get_db() as db:
            result = await db.execute(select(Automation).where(Automation.id == automation_id))
            row = result.scalar_one_or_none()
            if not row:
                return None
            row.is_active = not row.is_active
            row.next_run_at = next_run_at if row.is_active else None
            row.updated_at = updated_at
            await db.commit()
            await db.refresh(row)
            return row

    @staticmethod
    async def delete(automation_id: str) -> bool:
        async with await get_db() as db:
            # Delete runs first
            await db.execute(delete(AutomationRun).where(AutomationRun.automation_id == automation_id))
            result = await db.execute(delete(Automation).where(Automation.id == automation_id))
            await db.commit()
            return result.rowcount > 0

    @staticmethod
    async def claim_due(now_ns: int, limit: int = 10) -> list[Automation]:
        """Atomically claim due automations for execution.

        Selects rows where next_run_at <= now and is_active=True,
        then advances next_run_at so they won't be double-claimed.
        """
        async with await get_db() as db:
            result = await db.execute(
                select(Automation)
                .where(
                    Automation.is_active == True,  # noqa: E712
                    Automation.next_run_at <= now_ns,
                )
                .order_by(Automation.next_run_at)
                .limit(limit)
            )
            rows = list(result.scalars().all())

            from cptr.utils.automations import next_run_ns

            for row in rows:
                row.last_run_at = now_ns
                row.next_run_at = next_run_ns(row.rrule)

            await db.commit()
            return rows

    @staticmethod
    async def count_by_user(user_id: str) -> int:
        async with await get_db() as db:
            result = await db.execute(
                select(func.count()).select_from(Automation).filter_by(user_id=user_id)
            )
            return result.scalar() or 0


class AutomationRun(Base):
    """A single execution record for an automation."""

    __tablename__ = "automation_runs"

    id = Column(Text, primary_key=True, default=_uuid)
    automation_id = Column(Text, nullable=False, index=True)
    chat_id = Column(Text, nullable=True)
    status = Column(Text, nullable=False)  # "success" | "error"
    error = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_automation_run_aid_created", "automation_id", "created_at"),
    )

    # ── Class methods ────────────────────────────────────────

    @staticmethod
    async def create(
        automation_id: str,
        status: str,
        chat_id: str | None = None,
        error: str | None = None,
        created_at: int = 0,
    ) -> AutomationRun:
        async with await get_db() as db:
            run = AutomationRun(
                automation_id=automation_id,
                chat_id=chat_id,
                status=status,
                error=error,
                created_at=created_at,
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
            return run

    @staticmethod
    async def get_latest(automation_id: str) -> AutomationRun | None:
        async with await get_db() as db:
            result = await db.execute(
                select(AutomationRun)
                .where(AutomationRun.automation_id == automation_id)
                .order_by(AutomationRun.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def get_latest_batch(automation_ids: list[str]) -> dict[str, AutomationRun]:
        """Fetch the latest run for each automation in a single query."""
        if not automation_ids:
            return {}
        async with await get_db() as db:
            subq = (
                select(
                    AutomationRun.automation_id,
                    func.max(AutomationRun.created_at).label("max_created"),
                )
                .where(AutomationRun.automation_id.in_(automation_ids))
                .group_by(AutomationRun.automation_id)
                .subquery()
            )
            result = await db.execute(
                select(AutomationRun).join(
                    subq,
                    (AutomationRun.automation_id == subq.c.automation_id)
                    & (AutomationRun.created_at == subq.c.max_created),
                )
            )
            rows = result.scalars().all()
            return {row.automation_id: row for row in rows}

    @staticmethod
    async def get_by_automation(
        automation_id: str,
        skip: int = 0,
        limit: int = 50,
    ) -> list[AutomationRun]:
        async with await get_db() as db:
            result = await db.execute(
                select(AutomationRun)
                .where(AutomationRun.automation_id == automation_id)
                .order_by(AutomationRun.created_at.desc())
                .offset(skip)
                .limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def delete_by_automation(automation_id: str) -> int:
        async with await get_db() as db:
            result = await db.execute(
                delete(AutomationRun).where(AutomationRun.automation_id == automation_id)
            )
            await db.commit()
            return result.rowcount
