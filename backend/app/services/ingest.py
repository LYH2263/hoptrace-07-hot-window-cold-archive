import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import hot_window_cold_archive as hotcold
from app.seed import map_status


def run_batch() -> dict:
    """基座装载：按夹具追加；乱序重排 / 旁路 / 重复隔离留给功能题空桩。

    hot_window_cold_archive：每批装载成功后，在同一事务内对受影响运单执行
    归档切窗（热段只留最新 max_points 点，溢出点标冷+写段哈希）；切窗失败
    整批回滚。同 id 后写命中冷归档点时不改写其任何字段，计入 cold_skipped。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        written = 0
        cold_skipped = 0
        affected = set()
        for hop in hops:
            wid = hop["waybill_id"]
            affected.add(wid)
            exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                    (wid, hop.get("waybill_title") or wid, "未知", _now()),
                )
            prior = conn.execute("SELECT is_cold FROM hops WHERE id=?", (hop["id"],)).fetchone()
            conn.execute(
                """
                INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hop["id"],
                    wid,
                    hop["event_ts"],
                    hop["location_code"],
                    hop["event_code"],
                    hop.get("seq"),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
                proj = map_status(hop["event_code"])
                conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, wid))
                conn.execute(
                    "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,?)",
                    (wid, hop["id"], hop["event_code"], proj, _now()),
                )
            elif prior and prior["is_cold"]:
                # 冷归档点禁止被后写装载改写：INSERT OR IGNORE 未触碰该行，显式计数曝光
                cold_skipped += 1
        archive = {}
        for wid in affected:
            cut = hotcold.archive_cut(conn, wid)
            if cut["archived"]:
                archive[wid] = {"archived": cut["archived"], "seg_hash": cut["seg_hash"]}
        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "written": written,
            "input": len(hops),
            "cold_skipped": cold_skipped,
            "archive": archive,
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
