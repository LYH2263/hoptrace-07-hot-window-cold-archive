def list_primary(conn, waybill_id: str, include_cold: bool = False) -> list[dict]:
    """正式轴查询：默认只含热段（is_cold=0，已落库的热集合，非查询时临时截断）。

    include_cold=True 时连同冷归档点一并返回（只读展示用，冷点带 is_cold/archive_hash）。
    """
    sql = """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted, is_cold, archive_hash
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        """
    if not include_cold:
        sql += " AND is_cold=0"
    sql += " ORDER BY event_ts ASC, id ASC"
    rows = conn.execute(sql, (waybill_id,)).fetchall()
    return [dict(r) for r in rows]
