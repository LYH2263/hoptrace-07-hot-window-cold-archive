from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect
from app.modules import hot_window_cold_archive as hotcold

router = APIRouter(prefix="/api")


class HotWindowIn(BaseModel):
    # 热窗口语义：最大保留点数（max retained points），>=1
    max_points: int = Field(ge=1, le=10000)


@router.get("/hot-windows")
def list_hot_windows():
    """全部运单的有效热窗口（运单配置或默认值）。"""
    conn = connect()
    try:
        return hotcold.list_windows(conn)
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/hotcold-proof")
def hotcold_proof(waybill_id: str):
    """冷热证明（只读）：运单、热窗口参数、热点数、冷点数、冷段哈希。"""
    conn = connect()
    try:
        wb = conn.execute("SELECT 1 FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        return hotcold.hotcold_proof(conn, waybill_id)
    finally:
        conn.close()


@router.put("/waybills/{waybill_id}/hot-window")
def put_hot_window(waybill_id: str, body: HotWindowIn):
    """配置热窗口并同事务切窗（失败闭：切窗失败则配置一并回滚）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        wb = conn.execute("SELECT 1 FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            conn.rollback()
            raise HTTPException(404, "运单不存在")
        cut = hotcold.set_window(conn, waybill_id, body.max_points)
        proof = hotcold.hotcold_proof(conn, waybill_id)
        conn.commit()
        return {"ok": True, "cut": cut, "proof": proof}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"切窗失败，已整体回滚: {e}")
    finally:
        conn.close()
