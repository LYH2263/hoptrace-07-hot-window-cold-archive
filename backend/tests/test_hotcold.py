"""hot_window_cold_archive 验收：调小窗口使夹具点溢出，冷段不可变，热段仍可追加。"""

import json
import sqlite3

import pytest

from app import seed
from app.db import DB_PATH, connect
from app.modules import hot_window_cold_archive as hotcold
from app.services import ingest as ingest_svc
from app.services import timeline as timeline_svc


@pytest.fixture(autouse=True)
def fresh_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    seed.init_db()
    yield


def test_ingest_same_tx_archive_cut():
    # 夹具：WB-1001 热窗口=3，种子 3 点全热；装载 H-5 后 4 点 → 最老点 H-1 同事务入冷
    res = ingest_svc.run_batch()
    assert res["ok"], res
    assert res["report"]["archive"]["WB-1001"]["archived"] == 1

    conn = connect()
    try:
        proof = hotcold.hotcold_proof(conn, "WB-1001")
        assert proof["window"] == {**proof["window"], "kind": "max_points", "max_points": 3, "source": "waybill"}
        assert proof["hot_count"] == 3
        assert proof["cold_count"] == 1
        assert proof["cold_point_ids"] == ["H-1"]
        assert proof["cold_hash"]
        assert proof["consistent"]

        # 点已标冷则哈希必已写（同事务，无中间态）
        row = conn.execute("SELECT is_cold, archive_hash FROM hops WHERE id='H-1'").fetchone()
        assert row["is_cold"] == 1
        assert row["archive_hash"] == proof["segments"][0]["seg_hash"]

        # 正式轴查询默认只含热段；显式 include_cold 才能看到冷点
        hot = timeline_svc.list_primary(conn, "WB-1001")
        assert [h["id"] for h in hot] == ["H-2", "H-3", "H-5"]
        all_pts = timeline_svc.list_primary(conn, "WB-1001", include_cold=True)
        assert [h["id"] for h in all_pts] == ["H-1", "H-2", "H-3", "H-5"]
    finally:
        conn.close()


def test_shrink_window_overflow_cold_immutable_hot_appendable():
    ingest_svc.run_batch()

    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    cut = hotcold.set_window(conn, "WB-1001", 2)  # 调小窗口 → 夹具点溢出
    conn.commit()
    assert cut["archived"] == 1  # H-2 溢出

    proof = hotcold.hotcold_proof(conn, "WB-1001")
    assert proof["hot_count"] == 2
    assert proof["cold_count"] == 2
    assert proof["cold_point_ids"] == ["H-1", "H-2"]
    assert proof["consistent"]
    assert len(proof["segments"]) == 2  # 两次切窗各一段

    # 冷段不可变：库层触发器拒绝改写与删除
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE hops SET location_code='HACKED' WHERE id='H-1'")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM hops WHERE id='H-2'")
    conn.rollback()

    # 热段仍可追加：新点入轴后切窗，最新点留热，次老点继续入冷
    conn.execute(
        """
        INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
        VALUES ('H-7', 'WB-1001', '2026-11-17T21:00:00Z', 'HZ-STATION', 'DELIVERED', 5, 'primary')
        """
    )
    cut2 = hotcold.archive_cut(conn, "WB-1001")
    conn.commit()
    assert cut2["archived"] == 1  # H-3 溢出

    proof2 = hotcold.hotcold_proof(conn, "WB-1001")
    assert proof2["hot_point_ids"] == ["H-5", "H-7"]
    assert proof2["cold_point_ids"] == ["H-1", "H-2", "H-3"]
    assert proof2["consistent"]
    conn.close()


def test_reingest_does_not_rewrite_cold(tmp_path, monkeypatch):
    ingest_svc.run_batch()
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    hotcold.set_window(conn, "WB-1001", 1)  # 只留最新 1 点：H-1/H-2/H-3 全冷
    conn.commit()
    before = conn.execute(
        "SELECT location_code, event_code, archive_hash FROM hops WHERE id='H-2'"
    ).fetchone()
    conn.close()
    assert before["archive_hash"]

    # 后写装载以同 id 不同字段冲击冷区点 → 必须原样保留并计数
    payload = {
        "batch_id": "B-REWRITE",
        "hops": [
            {
                "id": "H-2",
                "waybill_id": "WB-1001",
                "event_ts": "2026-11-17T12:40:00Z",
                "location_code": "HACKED",
                "event_code": "DELIVERED",
                "seq": 99,
            }
        ],
    }
    (tmp_path / "ingest_batch.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ingest_svc, "FIXTURES_DIR", tmp_path)
    res = ingest_svc.run_batch()
    assert res["ok"], res
    assert res["report"]["written"] == 0
    assert res["report"]["cold_skipped"] == 1

    conn = connect()
    after = conn.execute(
        "SELECT location_code, event_code, is_cold, archive_hash FROM hops WHERE id='H-2'"
    ).fetchone()
    conn.close()
    assert after["location_code"] == before["location_code"]
    assert after["event_code"] == before["event_code"]
    assert after["is_cold"] == 1
    assert after["archive_hash"] == before["archive_hash"]


def test_routes_proof_window_and_timeline():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/waybills/WB-1001/hotcold-proof")
        assert r.status_code == 200
        body = r.json()
        assert body["waybill_id"] == "WB-1001"
        assert body["window"]["max_points"] == 3
        assert body["hot_count"] == 3
        assert body["cold_count"] == 0
        assert body["cold_hash"] is None

        # 调小窗口 → 夹具点溢出，证明接口返回冷段哈希
        r = client.put("/api/waybills/WB-1001/hot-window", json={"max_points": 2})
        assert r.status_code == 200
        proof = r.json()["proof"]
        assert proof["hot_count"] == 2
        assert proof["cold_count"] == 1
        assert proof["cold_hash"]
        assert proof["consistent"]

        # 时间线默认只含热段；include_cold 只读带出冷点
        r = client.get("/api/waybills/WB-1001/timeline")
        assert [h["id"] for h in r.json()["hops"]] == ["H-2", "H-3"]
        r = client.get("/api/waybills/WB-1001/timeline", params={"include_cold": "true"})
        assert [h["id"] for h in r.json()["hops"]] == ["H-1", "H-2", "H-3"]

        r = client.get("/api/hot-windows")
        wins = {w["waybill_id"]: w for w in r.json()}
        assert wins["WB-1001"]["max_points"] == 2
        assert wins["WB-1001"]["source"] == "waybill"
        assert wins["WB-1002"]["source"] == "default"

        # 非法窗口参数被拒
        r = client.put("/api/waybills/WB-1001/hot-window", json={"max_points": 0})
        assert r.status_code == 422
