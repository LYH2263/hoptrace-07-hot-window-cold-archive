"""hot_window_cold_archive — 运单正式轴热窗口 / 冷归档分层。

题目要求逐项落槌的设计抉择：

1. 热窗口语义（择一：按最大保留点数；未实现按最大保留时长）
   每条运单可配置 hot_windows.max_points（>=1）。正式有效轴定义为
   channel='primary' AND retracted=0 的点，按 (event_ts, id) 升序排列，
   最新 max_points 点留在热段，其余正式有效点必须进入冷归档集合。
   未配置的运单使用 DEFAULT_MAX_POINTS。正式轴查询默认只含热段
   （timeline.list_primary 默认 WHERE is_cold=0）。

2. 落库形态（择一：同表不可变 cold 标记 + 归档哈希列；未采用热表+冷归档表）
   - hops.is_cold INTEGER NOT NULL DEFAULT 0  —— 不可变冷标记，落库
   - hops.archive_hash TEXT                    —— 该点所属冷段哈希，落库
   - cold_segments 台账：每次切窗一行（段序号、窗口参数、点清单、段哈希）
   - hot_windows 配置表：运单 -> max_points
   禁止只在查询时临时 truncate：热段是已落库的 is_cold=0 集合，不是读时截断。

3. 归档路径（择一：装载成功后同事务切窗；未采用独立归档任务）
   ingest.run_batch 与 PUT /api/waybills/{id}/hot-window 都在自身事务内调用
   archive_cut；本模块所有写函数均不自行 commit，失败由调用方整体回滚，
   不会留下「点已标冷但哈希未写」或「热轴仍含应归档点」的中间态。

4. 冷区不可变
   SQLite 触发器 hops_cold_no_update / hops_cold_no_delete 在库层拒绝任何
   对已标冷点的 UPDATE/DELETE；装载端 INSERT OR IGNORE + cold_skipped 计数，
   保证后写装载不改写冷区点字段。
"""

import hashlib
import json
from datetime import datetime, timezone

STUB = False

# 热窗口语义：最大保留点数（max retained points）。未配置运单的默认窗口。
DEFAULT_MAX_POINTS = 50

# 正式有效点：正式轴上未被撤回的点和超龄点都参与冷热分层；旁路/隔离点不参与。
_OFFICIAL = "channel='primary' AND retracted=0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hot_windows (
  waybill_id TEXT PRIMARY KEY REFERENCES waybills(id),
  max_points INTEGER NOT NULL CHECK (max_points >= 1),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cold_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  seg_seq INTEGER NOT NULL,
  max_points INTEGER NOT NULL,
  point_count INTEGER NOT NULL,
  point_ids_json TEXT NOT NULL,
  seg_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(waybill_id, seg_seq)
);

CREATE TRIGGER IF NOT EXISTS hops_cold_no_update
BEFORE UPDATE ON hops
WHEN OLD.is_cold = 1
BEGIN
  SELECT RAISE(ABORT, 'cold hop immutable: 冷归档点禁止改写');
END;

CREATE TRIGGER IF NOT EXISTS hops_cold_no_delete
BEFORE DELETE ON hops
WHEN OLD.is_cold = 1
BEGIN
  SELECT RAISE(ABORT, 'cold hop immutable: 冷归档点禁止删除');
END;
"""


def ensure_schema(conn) -> None:
    """落库迁移（幂等）：hops 增加 is_cold / archive_hash 列；建配置表、冷段台账与不可变触发器。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(hops)")}
    if "is_cold" not in cols:
        conn.execute("ALTER TABLE hops ADD COLUMN is_cold INTEGER NOT NULL DEFAULT 0")
    if "archive_hash" not in cols:
        conn.execute("ALTER TABLE hops ADD COLUMN archive_hash TEXT")
    conn.executescript(SCHEMA)


def get_window(conn, waybill_id: str) -> dict:
    """运单热窗口参数：运单配置优先，否则默认窗口。"""
    row = conn.execute(
        "SELECT max_points, updated_at FROM hot_windows WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()
    if row:
        return {
            "kind": "max_points",
            "max_points": row["max_points"],
            "source": "waybill",
            "updated_at": row["updated_at"],
        }
    return {
        "kind": "max_points",
        "max_points": DEFAULT_MAX_POINTS,
        "source": "default",
        "updated_at": None,
    }


def list_windows(conn) -> list[dict]:
    """全部运单的有效热窗口（配置值或默认值），供测绘板/排障使用。"""
    rows = conn.execute(
        """
        SELECT w.id AS waybill_id, hw.max_points AS cfg, hw.updated_at AS updated_at
        FROM waybills w
        LEFT JOIN hot_windows hw ON hw.waybill_id = w.id
        ORDER BY w.id
        """
    ).fetchall()
    out = []
    for r in rows:
        configured = r["cfg"] is not None
        out.append(
            {
                "waybill_id": r["waybill_id"],
                "kind": "max_points",
                "max_points": r["cfg"] if configured else DEFAULT_MAX_POINTS,
                "source": "waybill" if configured else "default",
                "updated_at": r["updated_at"] if configured else None,
            }
        )
    return out


def set_window(conn, waybill_id: str, max_points: int) -> dict:
    """配置热窗口并同事务切窗。

    失败闭：本函数不 commit；调用方事务内任一步失败整体回滚——
    配置与切窗同生共死，不会留下「窗口已调小但应归档点仍挂热轴」。
    """
    if not isinstance(max_points, int) or isinstance(max_points, bool) or max_points < 1:
        raise ValueError("max_points 必须为 >=1 的整数")
    conn.execute(
        """
        INSERT INTO hot_windows(waybill_id, max_points, updated_at) VALUES (?,?,?)
        ON CONFLICT(waybill_id) DO UPDATE
        SET max_points=excluded.max_points, updated_at=excluded.updated_at
        """,
        (waybill_id, max_points, _now()),
    )
    return archive_cut(conn, waybill_id)


def archive_cut(conn, waybill_id: str) -> dict:
    """切窗：热段只留最新 max_points 个正式有效点，溢出点标冷并写段哈希。

    同事务语义：台账行（cold_segments）与点标记（hops.is_cold/archive_hash）
    在同一事务内写入；任一点标冷未生效即抛错，由调用方回滚，
    不留「点已标冷但哈希未写」或「热轴仍含应归档点」。
    """
    win = get_window(conn, waybill_id)
    rows = conn.execute(
        f"""
        SELECT id, event_ts, location_code, event_code, seq
        FROM hops
        WHERE waybill_id=? AND {_OFFICIAL} AND is_cold=0
        ORDER BY event_ts ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    overflow = len(rows) - win["max_points"]
    if overflow <= 0:
        return {"waybill_id": waybill_id, "window": win, "archived": 0, "seg_hash": None}

    to_cold = rows[:overflow]
    ids = [r["id"] for r in to_cold]
    seg_hash = _segment_hash(to_cold)
    seg_seq = conn.execute(
        "SELECT COALESCE(MAX(seg_seq), 0) + 1 AS s FROM cold_segments WHERE waybill_id=?",
        (waybill_id,),
    ).fetchone()["s"]
    conn.execute(
        """
        INSERT INTO cold_segments(waybill_id, seg_seq, max_points, point_count, point_ids_json, seg_hash, created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (waybill_id, seg_seq, win["max_points"], len(ids), json.dumps(ids), seg_hash, _now()),
    )
    for hid in ids:
        cur = conn.execute(
            "UPDATE hops SET is_cold=1, archive_hash=? WHERE id=? AND is_cold=0",
            (seg_hash, hid),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"切窗失败：点 {hid} 标冷未生效，事务应回滚")
    return {
        "waybill_id": waybill_id,
        "window": win,
        "archived": len(ids),
        "seg_seq": seg_seq,
        "seg_hash": seg_hash,
        "point_ids": ids,
    }


def hotcold_proof(conn, waybill_id: str) -> dict:
    """冷热证明（只读）：运单、热窗口参数、热点数、冷点数、冷段哈希。

    cold_hash 为全部冷段哈希按段序聚合的摘要；consistent 校验台账点清单
    与 hops 表冷点集合及逐点 archive_hash 是否账实相符。
    """
    win = get_window(conn, waybill_id)
    hot = conn.execute(
        f"SELECT id FROM hops WHERE waybill_id=? AND {_OFFICIAL} AND is_cold=0 ORDER BY event_ts ASC, id ASC",
        (waybill_id,),
    ).fetchall()
    cold = conn.execute(
        f"SELECT id, archive_hash FROM hops WHERE waybill_id=? AND {_OFFICIAL} AND is_cold=1 ORDER BY event_ts ASC, id ASC",
        (waybill_id,),
    ).fetchall()
    segs = conn.execute(
        """
        SELECT seg_seq, max_points, point_count, point_ids_json, seg_hash, created_at
        FROM cold_segments WHERE waybill_id=? ORDER BY seg_seq ASC
        """,
        (waybill_id,),
    ).fetchall()
    segments = [
        {
            "seg_seq": s["seg_seq"],
            "max_points": s["max_points"],
            "point_count": s["point_count"],
            "point_ids": json.loads(s["point_ids_json"]),
            "seg_hash": s["seg_hash"],
            "created_at": s["created_at"],
        }
        for s in segs
    ]
    cold_hash = None
    if segments:
        agg = hashlib.sha256()
        for s in segments:
            agg.update(s["seg_hash"].encode("utf-8"))
            agg.update(b"\n")
        cold_hash = agg.hexdigest()

    seg_hash_by_id = {pid: s["seg_hash"] for s in segments for pid in s["point_ids"]}
    row_ids = [r["id"] for r in cold]
    consistent = sorted(seg_hash_by_id) == sorted(row_ids) and all(
        seg_hash_by_id.get(r["id"]) == r["archive_hash"] for r in cold
    )
    return {
        "waybill_id": waybill_id,
        "window": win,
        "hot_count": len(hot),
        "cold_count": len(cold),
        "cold_hash": cold_hash,
        "segments": segments,
        "hot_point_ids": [r["id"] for r in hot],
        "cold_point_ids": row_ids,
        "consistent": consistent,
    }


def _segment_hash(rows) -> str:
    """冷段哈希：按 (event_ts, id) 升序对点内容（id|ts|loc|code|seq）逐行摘要。"""
    h = hashlib.sha256()
    for r in rows:
        line = f"{r['id']}|{r['event_ts']}|{r['location_code']}|{r['event_code']}|{r['seq']}\n"
        h.update(line.encode("utf-8"))
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
