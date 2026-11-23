"""ORM 模型。

版本号约定(均为单调递增计数器,即使改回原值也不会回退):
- Item.version            规范(标称值/上下限/名称)每被发布的批次改动一次 +1
- Item.claims_version     该项目的领用关系每被发布的批次增删一次 +1
- Workstation.version     工位配置(名称/终端代际)每被发布的批次改动一次 +1
- Workstation.claims_version  该工位的领用关系每被发布的批次增删一次 +1
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Item(Base):
    """检验项目规范:标称尺寸与上下限,整数微米。"""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    nominal_um: Mapped[int] = mapped_column(Integer)
    upper_um: Mapped[int] = mapped_column(Integer)
    lower_um: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    claims_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Workstation(Base):
    """机床工位:登记终端代际(new/old)与领用项目。"""

    __tablename__ = "workstations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    terminal_generation: Mapped[str] = mapped_column(String(8))  # "new" | "old"
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    claims_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Claim(Base):
    """领用关系:工位领用检验项目。"""

    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("workstation_id", "item_id", name="uq_claim_ws_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workstation_id: Mapped[int] = mapped_column(ForeignKey("workstations.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Batch(Base):
    """修改批次:规范与工位配置可放在同一批,先核对再发布。

    状态机: draft -> verified -> published
                 \\-> rejected (核对发现旧终端领用非对称公差,整批退回)
    """

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    violations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    fingerprint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    ops: Mapped[list["BatchOp"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="BatchOp.seq",
        lazy="selectin",
    )


class BatchOp(Base):
    """批次内的一条有序操作。"""

    __tablename__ = "batch_ops"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)

    batch: Mapped[Batch] = relationship(back_populates="ops")
