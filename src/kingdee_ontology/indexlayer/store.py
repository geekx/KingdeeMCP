"""Funnel 索引 —— 对象的本地物化与检索。

为什么需要这一层：对象台要「按单号找单」「跨类型搜」「看某个供应商下所有单」，
每次都打账套既慢又打不动（金蝶的 ExecuteBillQuery 不支持跨表检索）。
索引把加工好的对象存下来，检索走本地。

三条纪律，都是为了不让索引变成"看起来是真的假数据"：

  1. **每条记录都带出处与取数时间**。检索结果必须能回答"这是什么时候的"，
     陈旧数据当现状用是这类系统最常见的事故。
  2. **索引不是真相**。任何写操作之后，相关对象在索引里标记为 stale，
     检索时如实告知；要准确必须回源。
  3. **只索引加工过的行**（Dataset），不接受裸 dict——
     绕过标准化的数据进了索引，状态码和字段名就又乱了。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DB = "indexlayer/objects.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    noun        TEXT NOT NULL,
    obj_id      TEXT NOT NULL,
    obj_no      TEXT,
    state       TEXT,
    props       TEXT NOT NULL,          -- JSON
    source      TEXT NOT NULL,          -- provenance.source
    tenant      TEXT NOT NULL DEFAULT '',
    fetched_at  TEXT NOT NULL,
    stale       INTEGER NOT NULL DEFAULT 0,
    stale_reason TEXT,
    PRIMARY KEY (tenant, noun, obj_id)
);
CREATE INDEX IF NOT EXISTS ix_obj_no    ON objects(tenant, obj_no);
CREATE INDEX IF NOT EXISTS ix_obj_state ON objects(tenant, noun, state);
CREATE INDEX IF NOT EXISTS ix_obj_stale ON objects(tenant, stale);

CREATE TABLE IF NOT EXISTS sync_watermark (
    tenant TEXT NOT NULL, noun TEXT NOT NULL,
    last_run TEXT NOT NULL, rows INTEGER NOT NULL,
    filter_string TEXT, truncated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, noun)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ObjectIndex:
    def __init__(self, path: str | Path = DEFAULT_DB, tenant: str = ""):
        self.path = Path(path)
        self.tenant = tenant
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(_SCHEMA)
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    # ── 写入 ──────────────────────────────────────────────────
    def upsert(self, dataset) -> dict:
        """物化一张加工好的表。只接受 Dataset —— 裸 dict 会绕过标准化。"""
        if not hasattr(dataset, "provenance"):
            raise TypeError(
                "索引只接受 pipeline.Dataset。裸 dict 没经过标准化，"
                "状态码与字段名会不一致，进了索引就查不准了。")
        rows, skipped = 0, 0
        with closing(self._conn()) as c:
            for r in dataset.rows:
                oid = r.get("_id") or r.get("_no")
                if oid is None:
                    skipped += 1          # 没有标识就没法定位，不入库
                    continue
                c.execute(
                    "INSERT INTO objects (noun,obj_id,obj_no,state,props,source,tenant,"
                    "fetched_at,stale,stale_reason) VALUES (?,?,?,?,?,?,?,?,0,NULL) "
                    "ON CONFLICT(tenant,noun,obj_id) DO UPDATE SET "
                    "obj_no=excluded.obj_no, state=excluded.state, props=excluded.props,"
                    "source=excluded.source, fetched_at=excluded.fetched_at,"
                    "stale=0, stale_reason=NULL",
                    (dataset.noun, str(oid), _s(r.get("_no")), _s(r.get("_state")),
                     json.dumps(r, ensure_ascii=False), dataset.provenance.source,
                     self.tenant, dataset.provenance.fetched_at))
                rows += 1
            c.execute(
                "INSERT INTO sync_watermark (tenant,noun,last_run,rows,filter_string,truncated)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(tenant,noun) DO UPDATE SET "
                "last_run=excluded.last_run, rows=excluded.rows,"
                "filter_string=excluded.filter_string, truncated=excluded.truncated",
                (self.tenant, dataset.noun, dataset.provenance.fetched_at, rows,
                 dataset.provenance.filter_string,
                 1 if dataset.provenance.truncated else 0))
            c.commit()
        return {"noun": dataset.noun, "indexed": rows, "skipped_no_id": skipped,
                "truncated": dataset.provenance.truncated}

    def mark_stale(self, noun: str, ids: Iterable[str], reason: str) -> int:
        """写操作之后把相关对象标脏。索引不是真相，改过就得承认它可能不准。"""
        ids = [str(i) for i in ids if i]
        if not ids:
            return 0
        with closing(self._conn()) as c:
            cur = c.execute(
                f"UPDATE objects SET stale=1, stale_reason=? WHERE tenant=? AND noun=? "
                f"AND (obj_id IN ({','.join('?' * len(ids))}) "
                f"OR obj_no IN ({','.join('?' * len(ids))}))",
                [reason, self.tenant, noun, *ids, *ids])
            c.commit()
            return cur.rowcount

    # ── 检索 ──────────────────────────────────────────────────
    def get(self, noun: str, obj_id: str) -> Optional[dict]:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM objects WHERE tenant=? AND noun=? AND "
                          "(obj_id=? OR obj_no=?)",
                          (self.tenant, noun, str(obj_id), str(obj_id))).fetchone()
        return _row(r) if r else None

    def find_by_no(self, obj_no: str) -> list[dict]:
        """按单号跨类型找——不用先知道它是什么单。"""
        with closing(self._conn()) as c:
            rs = c.execute("SELECT * FROM objects WHERE tenant=? AND obj_no=?",
                           (self.tenant, str(obj_no))).fetchall()
        return [_row(r) for r in rs]

    def search(self, noun: str = "", state: str = "", contains: str = "",
               stale: Optional[bool] = None, limit: int = 50) -> dict:
        sql = ["SELECT * FROM objects WHERE tenant=?"]
        args: list[Any] = [self.tenant]
        if noun:
            sql.append("AND noun=?"); args.append(noun)
        if state:
            sql.append("AND state=?"); args.append(state)
        if contains:
            sql.append("AND props LIKE ?"); args.append(f"%{contains}%")
        if stale is not None:
            sql.append("AND stale=?"); args.append(1 if stale else 0)
        sql.append("ORDER BY fetched_at DESC LIMIT ?"); args.append(limit)
        with closing(self._conn()) as c:
            rs = c.execute(" ".join(sql), args).fetchall()
        hits = [_row(r) for r in rs]
        n_stale = sum(1 for h in hits if h["stale"])
        return {
            "count": len(hits), "hits": hits, "stale_hits": n_stale,
            "caveat": ("索引是快照，不是账套现状。"
                       + (f"其中 {n_stale} 条已被写操作标脏，要准确请回源 kd_object。"
                          if n_stale else "")),
        }

    def coverage(self) -> dict:
        """索引里有什么、多久没同步了。回答"我能相信它到什么程度"。"""
        with closing(self._conn()) as c:
            rows = c.execute(
                "SELECT noun, COUNT(*) n, SUM(stale) s, MAX(fetched_at) t "
                "FROM objects WHERE tenant=? GROUP BY noun ORDER BY n DESC",
                (self.tenant,)).fetchall()
            wm = {r["noun"]: dict(r) for r in c.execute(
                "SELECT * FROM sync_watermark WHERE tenant=?", (self.tenant,)).fetchall()}
        out = []
        for r in rows:
            w = wm.get(r["noun"], {})
            out.append({"noun": r["noun"], "objects": r["n"], "stale": r["s"] or 0,
                        "last_fetch": r["t"],
                        "truncated": bool(w.get("truncated")),
                        "filter": w.get("filter_string") or ""})
        return {"tenant": self.tenant or "(默认)", "nouns": len(out),
                "objects": sum(x["objects"] for x in out), "by_noun": out}


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _row(r: sqlite3.Row) -> dict:
    return {"noun": r["noun"], "id": r["obj_id"], "no": r["obj_no"],
            "state": r["state"], "props": json.loads(r["props"]),
            "source": r["source"], "fetched_at": r["fetched_at"],
            "stale": bool(r["stale"]), "stale_reason": r["stale_reason"]}
