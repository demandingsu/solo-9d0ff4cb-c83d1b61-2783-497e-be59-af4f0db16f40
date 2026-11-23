"""机床工位量检规范下发 API。

- 检验项目规范:标称尺寸 + 上下限,整数微米。
- 工位登记终端代际(new/old)并领用项目;新终端接收标称值+上下限,旧终端只认标称值+对称公差。
- 所有修改都走「批次」:规范与工位配置可同批修改,先核对(verify)再发布(publish)。
- 核对后若所涉规范/工位配置/领用变过(即使改回原样),发布会被拒绝,需重新核对;无关改动不影响发布。
- 按工位取数:GET /workstations/{code}/spec 每次返回一整份生效的下发规范。
"""
from __future__ import annotations

import hashlib
import json

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, schemas, services
from .db import Base, engine, get_session
from .models import Batch, BatchOp, Claim, Item, Workstation, utcnow

Base.metadata.create_all(engine)

app = FastAPI(
    title="机床工位量检规范下发 API",
    version="1.0.0",
    description=(
        "量检规范(整数微米)按批次修改、核对、发布,并按工位整份下发。"
        "新终端接收标称值+上下限,旧终端只认标称值+对称公差。"
    ),
)


# ---------------------------------------------------------------- 工具


def _get_batch(session: Session, batch_id: int) -> Batch:
    batch = session.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(404, detail={"error": "batch_not_found", "batch_id": batch_id})
    return batch


def _get_item(session: Session, code: str) -> Item:
    item = session.scalar(select(Item).where(Item.code == code))
    if item is None:
        raise HTTPException(404, detail={"error": "item_not_found", "item_code": code})
    return item


def _get_workstation(session: Session, code: str) -> Workstation:
    ws = session.scalar(select(Workstation).where(Workstation.code == code))
    if ws is None:
        raise HTTPException(404, detail={"error": "workstation_not_found", "workstation_code": code})
    return ws


def _item_out(item: Item) -> dict:
    return {
        "code": item.code,
        "name": item.name,
        "nominal_um": item.nominal_um,
        "upper_um": item.upper_um,
        "lower_um": item.lower_um,
        "symmetric": item.upper_um - item.nominal_um == item.nominal_um - item.lower_um,
        "version": item.version,
        "claims_version": item.claims_version,
    }


# ---------------------------------------------------------------- 根


@app.get("/", include_in_schema=False)
def root():
    return {"service": "机床工位量检规范下发 API", "docs": "/docs", "openapi": "/openapi.json"}


# ---------------------------------------------------------------- 批次


@app.post("/batches", status_code=201, response_model=schemas.BatchOut, tags=["批次"])
def create_batch(payload: schemas.BatchCreate, session: Session = Depends(get_session)):
    """新建修改批次(可带初始操作)。规范与工位配置可放在同一批。"""
    batch = Batch(title=payload.title, status="draft")
    session.add(batch)
    session.flush()
    for i, op in enumerate(payload.ops):
        session.add(BatchOp(batch_id=batch.id, seq=i, payload=op.model_dump()))
    session.commit()
    return services.batch_to_out(batch)


@app.get("/batches", response_model=list[schemas.BatchOut], tags=["批次"])
def list_batches(status: str | None = None, session: Session = Depends(get_session)):
    stmt = select(Batch).order_by(Batch.id)
    if status:
        stmt = stmt.where(Batch.status == status)
    return [services.batch_to_out(b) for b in session.scalars(stmt).all()]


@app.get("/batches/{batch_id}", response_model=schemas.BatchOut, tags=["批次"])
def get_batch(batch_id: int, session: Session = Depends(get_session)):
    return services.batch_to_out(_get_batch(session, batch_id))


@app.put("/batches/{batch_id}/ops", response_model=schemas.BatchOut, tags=["批次"])
def replace_ops(batch_id: int, payload: schemas.OpsReplace, session: Session = Depends(get_session)):
    """整体替换批次操作。已核对的批次会被打回 draft,需重新核对。"""
    with services.STATE_LOCK:
        batch = _get_batch(session, batch_id)
        if batch.status == "published":
            raise HTTPException(409, detail={"error": "batch_published", "message": "已发布的批次不可修改"})
        for op in list(batch.ops):
            session.delete(op)
        session.flush()
        for i, op in enumerate(payload.ops):
            session.add(BatchOp(batch_id=batch.id, seq=i, payload=op.model_dump()))
        batch.status = "draft"
        batch.violations = None
        batch.fingerprint = None
        batch.verified_at = None
        session.commit()
        return services.batch_to_out(batch)


@app.delete("/batches/{batch_id}", status_code=204, tags=["批次"])
def delete_batch(batch_id: int, session: Session = Depends(get_session)):
    """作废未发布的批次。"""
    with services.STATE_LOCK:
        batch = _get_batch(session, batch_id)
        if batch.status == "published":
            raise HTTPException(409, detail={"error": "batch_published", "message": "已发布的批次不可删除"})
        session.delete(batch)
        session.commit()


@app.post("/batches/{batch_id}/verify", response_model=schemas.VerifyResponse, tags=["批次"])
def verify_batch(batch_id: int, session: Session = Depends(get_session)):
    """核对:模拟应用整批,检查相关工位将领用的规范。

    若改完后仍有旧终端领用非对称公差,列出工位和项目,整批退回(rejected);
    否则记录所涉实体版本指纹,批次进入 verified。
    """
    with services.STATE_LOCK:
        batch = _get_batch(session, batch_id)
        if batch.status == "published":
            raise HTTPException(409, detail={"error": "batch_published", "message": "批次已发布"})
        ops = [op.payload for op in batch.ops]
        state = services.load_state(session)
        try:
            sim = services.simulate(state, ops)
        except services.OpError as exc:
            raise HTTPException(422, detail={"error": "op_error", "message": str(exc)})
        violations = services.find_violations(sim)
        preview = services.build_preview(sim, ops)
        batch.verified_at = utcnow()
        if violations:
            batch.status = "rejected"
            batch.violations = violations
            batch.fingerprint = None
        else:
            batch.status = "verified"
            batch.violations = None
            batch.fingerprint = services.build_fingerprint(ops, state)
        session.commit()
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "violations": violations,
            "preview": preview,
            "verified_at": batch.verified_at,
        }


@app.post("/batches/{batch_id}/publish", response_model=schemas.PublishResponse, tags=["批次"])
def publish_batch(batch_id: int, session: Session = Depends(get_session)):
    """发布:要求批次已核对且核对后所涉实体未变(无关改动不影响发布)。

    发布时事务性落库并联动换版;生效状态永远满足「旧终端不领用非对称公差」。
    """
    with services.STATE_LOCK:
        batch = _get_batch(session, batch_id)
        if batch.status == "published":
            raise HTTPException(409, detail={"error": "batch_published", "message": "批次已发布"})
        if batch.status != "verified":
            raise HTTPException(
                409,
                detail={"error": "batch_not_verified", "message": "批次未核对通过,请先 verify"},
            )
        state = services.load_state(session)
        changed = services.fingerprint_changes(batch.fingerprint or {}, state)
        if changed:
            # 核对后所涉实体变过(即使改回原样):核对失效,打回 draft
            batch.status = "draft"
            batch.fingerprint = None
            batch.verified_at = None
            session.commit()
            raise HTTPException(
                409,
                detail={
                    "error": "verification_stale",
                    "message": "核对后相关规范/工位配置/领用已变化,需重新核对",
                    "changed_entities": changed,
                },
            )
        ops = [op.payload for op in batch.ops]
        try:
            sim = services.simulate(state, ops)
        except services.OpError as exc:
            raise HTTPException(422, detail={"error": "op_error", "message": str(exc)})
        violations = services.find_violations(sim)  # 安全网:发布前复检
        if violations:
            batch.status = "rejected"
            batch.violations = violations
            batch.fingerprint = None
            session.commit()
            raise HTTPException(
                422,
                detail={
                    "error": "asymmetric_tolerance_on_old_terminal",
                    "message": "改完后仍有旧终端领用非对称公差,整批退回",
                    "violations": violations,
                },
            )
        changes = services.diff_states(state, sim)
        enriched = services.apply_changes(session, changes, sim)
        batch.status = "published"
        batch.published_at = utcnow()
        batch.violations = None
        session.commit()
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "published_at": batch.published_at,
            "changes": enriched,
        }


# ---------------------------------------------------------------- 检验项目


@app.get("/items", response_model=list[schemas.ItemOut], tags=["查询"])
def list_items(session: Session = Depends(get_session)):
    return [_item_out(it) for it in session.scalars(select(Item).order_by(Item.code)).all()]


@app.get("/items/{code}", response_model=schemas.ItemDetailOut, tags=["查询"])
def get_item(code: str, session: Session = Depends(get_session)):
    item = _get_item(session, code)
    claimed_by = [
        ws.code
        for ws in session.scalars(
            select(Workstation)
            .join(Claim, Claim.workstation_id == Workstation.id)
            .where(Claim.item_id == item.id)
            .order_by(Workstation.code)
        ).all()
    ]
    return {**_item_out(item), "claimed_by": claimed_by}


# ---------------------------------------------------------------- 工位


@app.get("/workstations", response_model=list[schemas.WorkstationOut], tags=["查询"])
def list_workstations(session: Session = Depends(get_session)):
    workstations = session.scalars(select(Workstation).order_by(Workstation.code)).all()
    return [_workstation_out(session, ws) for ws in workstations]


def _workstation_out(session: Session, ws: Workstation) -> dict:
    claimed = [
        it.code
        for it in session.scalars(
            select(Item)
            .join(Claim, Claim.item_id == Item.id)
            .where(Claim.workstation_id == ws.id)
            .order_by(Item.code)
        ).all()
    ]
    return {
        "code": ws.code,
        "name": ws.name,
        "terminal_generation": ws.terminal_generation,
        "version": ws.version,
        "claims_version": ws.claims_version,
        "claimed_items": claimed,
    }


@app.get("/workstations/{code}", response_model=schemas.WorkstationOut, tags=["查询"])
def get_workstation(code: str, session: Session = Depends(get_session)):
    return _workstation_out(session, _get_workstation(session, code))


@app.get("/workstations/{code}/spec", response_model=schemas.WorkstationSpecOut, tags=["下发"])
def workstation_spec(code: str, session: Session = Depends(get_session)):
    """按工位取数:每次返回一整份生效的下发规范。

    新终端:标称值 + 上下限;旧终端:标称值 + 对称公差。
    revision 为内容指纹,内容不变则不变,便于终端比对换版。
    """
    ws = _get_workstation(session, code)
    items = session.scalars(
        select(Item)
        .join(Claim, Claim.item_id == Item.id)
        .where(Claim.workstation_id == ws.id)
        .order_by(Item.code)
    ).all()
    if ws.terminal_generation == "old":
        spec_items = [
            {
                "item_code": it.code,
                "name": it.name,
                "nominal_um": it.nominal_um,
                "tolerance_um": it.upper_um - it.nominal_um,
            }
            for it in items
        ]
    else:
        spec_items = [
            {
                "item_code": it.code,
                "name": it.name,
                "nominal_um": it.nominal_um,
                "upper_um": it.upper_um,
                "lower_um": it.lower_um,
            }
            for it in items
        ]
    revision = hashlib.sha256(
        json.dumps(
            {"terminal_generation": ws.terminal_generation, "items": spec_items},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "workstation_code": ws.code,
        "workstation_name": ws.name,
        "terminal_generation": ws.terminal_generation,
        "revision": revision,
        "generated_at": utcnow(),
        "items": spec_items,
    }
