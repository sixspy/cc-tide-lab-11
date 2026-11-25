"""多源串扰隔离：策略读写与只读隔离视图。"""
from __future__ import annotations
from app.db import 连接
from app.modules import 多源串扰隔离 as 隔离
from app.services import 目录


def 当前策略() -> str:
    return 隔离.规整体例(目录.窗规().get(隔离.窗规键, 隔离.默认形态))


def 设策略(值: str) -> str:
    v = 隔离.规整体例(值)  # 坏口径直接抛 ValueError，不落库
    目录.改窗规({隔离.窗规键: v})
    return v


def 归档列表(运单键: str | None = None) -> list[dict]:
    """只读源明细（仅合并口径产生）。可按运单过滤。"""
    conn = 连接()
    try:
        if 运单键:
            rows = conn.execute(
                "SELECT * FROM 多源归档 WHERE 运单键=? ORDER BY 运单键,源键,编号",
                (运单键,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM 多源归档 ORDER BY 运单键,源键,编号"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def 运单隔离视图() -> list[dict]:
    """只读接口：运单键、源列表、片行数、valid。

    复合口径按片上源键取源；合并口径按只读归档取源，缺归档即 invalid。
    """
    conn = 连接()
    try:
        片行 = conn.execute(
            "SELECT 切片编号,窗号,运单键,源键,隔离形态,事件数 FROM 窗切片账 ORDER BY 运单键,切片编号"
        ).fetchall()
        归档 = conn.execute(
            "SELECT 切片编号,运单键,源键,事件数 FROM 多源归档"
        ).fetchall()
    finally:
        conn.close()

    档按片: dict[str, list] = {}
    for a in 归档:
        档按片.setdefault(a["切片编号"], []).append(a)

    视图: dict[str, dict] = {}
    for p in 片行:
        v = 视图.setdefault(p["运单键"], {"运单键": p["运单键"], "源列表": [], "片行数": 0, "valid": True})
        v["片行数"] += 1
        if p["隔离形态"] == 隔离.复合隔离:
            if p["源键"]:
                v["源列表"].append(p["源键"])
            else:
                v["valid"] = False
        elif p["隔离形态"] == 隔离.运单合并:
            源档 = 档按片.get(p["切片编号"], [])
            if not 源档:
                v["valid"] = False  # 合并片无归档 = 混源
            for a in 源档:
                v["源列表"].append(a["源键"])
            if sum(int(a["事件数"]) for a in 源档) != int(p["事件数"]):
                v["valid"] = False
        else:
            v["valid"] = False

    out = []
    for v in 视图.values():
        # 去重保序
        seen: set[str] = set()
        v["源列表"] = [s for s in v["源列表"] if not (s in seen or seen.add(s))]
        out.append(v)
    return sorted(out, key=lambda x: x["运单键"])


def 概览() -> dict:
    conn = 连接()
    try:
        return {
            "策略": 当前策略(),
            "窗规键": 隔离.窗规键,
            "形态": list(隔离.形态),
            "归档笔数": conn.execute("SELECT COUNT(*) AS c FROM 多源归档").fetchone()["c"],
            "关窗锁持锁": conn.execute("SELECT COUNT(*) AS c FROM 关窗锁").fetchone()["c"],
        }
    finally:
        conn.close()
