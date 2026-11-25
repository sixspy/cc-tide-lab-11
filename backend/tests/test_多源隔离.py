"""多源串扰隔离：两形态切片、只读归档、关窗互斥与贯验附加检查。"""
from pathlib import Path
import os
import tempfile
import threading

import pytest
from starlette.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="tide-multi-")
os.environ["数据目录"] = _tmp
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

from app import seed
from app.db import 连接, 库路径
from app.main import app
from app.modules import 多源串扰隔离 as 隔离
from app.services import 装载, 关窗, 贯验, 目录, 夹具, 多源

双源同运单 = {
    "批次编号": "双源批",
    "事件": [
        {"事件编号": "M-1", "运单键": "运单-M", "网点键": "网点-东站",
         "事件时": "2026-11-24T00:05:00Z"},
        {"事件编号": "M-2", "运单键": "运单-M", "网点键": "网点-西港",
         "事件时": "2026-11-24T00:06:00Z"},
    ],
}
单源对照 = {
    "批次编号": "单源批",
    "事件": [
        {"事件编号": "S-1", "运单键": "运单-S", "网点键": "网点-东站",
         "事件时": "2026-11-24T00:07:00Z"},
    ],
}


@pytest.fixture(autouse=True)
def _干净库():
    if 库路径.exists():
        库路径.unlink()
    seed.初始化库()
    yield


def _切片(运单: str) -> list[dict]:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 窗切片账 WHERE 运单键=? ORDER BY 切片编号", (运单,)
        ).fetchall()]
    finally:
        conn.close()


def _归档(运单: str) -> list[dict]:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 多源归档 WHERE 运单键=? ORDER BY 源键", (运单,)
        ).fetchall()]
    finally:
        conn.close()


def test_模块已接线并声明两形态():
    d = 隔离.describe()
    assert d["已接线"] is True
    assert 隔离.复合隔离 in d["形态"] and 隔离.运单合并 in d["形态"]
    assert d["默认形态"] == 隔离.复合隔离
    assert 隔离.规整体例("复合隔离") == 隔离.复合隔离
    with pytest.raises(ValueError):
        隔离.规整体例("随便拼")


def test_复合口径_两源同运单物化成两行片():
    assert 多源.当前策略() == 隔离.复合隔离
    装载.装一批(双源同运单)
    关 = 关窗.关当前窗()
    assert 关["隔离形态"] == 隔离.复合隔离
    片 = _切片("运单-M")
    assert len(片) == 2, "复合口径：切片键=源键+运单键，两源必须两行片"
    assert {p["源键"] for p in 片} == {"网点-东站", "网点-西港"}
    assert all(p["隔离形态"] == 隔离.复合隔离 for p in 片)
    assert all("网点-东站" in p["切片编号"] or "网点-西港" in p["切片编号"] for p in 片)
    # 每片只含本来源事件，源不串片
    assert {p["事件数"] for p in 片} == {1}
    assert 贯验.跑一次()["是否对齐"] is True


def test_合并口径_一行片且归档保留两源明细():
    多源.设策略(隔离.运单合并)
    装载.装一批(双源同运单)
    关 = 关窗.关当前窗()
    assert 关["隔离形态"] == 隔离.运单合并
    片 = _切片("运单-M")
    assert len(片) == 1, "合并口径：同运单只准现行一片"
    assert 片[0]["事件数"] == 2
    档 = _归档("运单-M")
    assert len(档) == 2, "合并口径缺只读源归档即混源"
    assert {a["源键"] for a in 档} == {"网点-东站", "网点-西港"}
    assert sum(a["事件数"] for a in 档) == 片[0]["事件数"]
    assert all(a["切片编号"] == 片[0]["切片编号"] for a in 档)
    assert 贯验.跑一次()["是否对齐"] is True


def test_单源对照_两种形态都只出一行片():
    装载.装一批(单源对照)
    关窗.关当前窗()
    assert len(_切片("运单-S")) == 1
    assert _归档("运单-S") == []

    多源.设策略(隔离.运单合并)
    装载.装一批({
        "批次编号": "单源批2",
        "事件": [{"事件编号": "S-2", "运单键": "运单-S2", "网点键": "网点-西港",
                  "事件时": "2026-11-24T00:08:00Z"}],
    })
    关窗.关当前窗()
    assert len(_切片("运单-S2")) == 1


def test_只读接口_运单键源列表片行数valid():
    装载.装一批(双源同运单)
    装载.装一批(单源对照)
    关窗.关当前窗()
    视图 = 多源.运单隔离视图()
    行 = {v["运单键"]: v for v in 视图}
    assert set(行["运单-M"].keys()) >= {"运单键", "源列表", "片行数", "valid"}
    m = 行["运单-M"]
    assert m["片行数"] == 2
    assert sorted(m["源列表"]) == ["网点-东站", "网点-西港"]
    assert m["valid"] is True
    assert 行["运单-S"]["片行数"] == 1 and 行["运单-S"]["valid"] is True
    # 归档只读接口
    档 = 多源.归档列表("运单-M")
    assert 档 == []  # 复合口径不写归档
    assert 多源.归档列表() == []


def test_合并口径只读接口带归档且可按运单过滤():
    多源.设策略(隔离.运单合并)
    装载.装一批(双源同运单)
    关窗.关当前窗()
    档 = 多源.归档列表("运单-M")
    assert len(档) == 2
    assert all(set(a.keys()) >= {"切片编号", "运单键", "源键", "事件数", "物化时"} for a in 档)
    行 = {v["运单键"]: v for v in 多源.运单隔离视图()}["运单-M"]
    assert 行["片行数"] == 1 and 行["valid"] is True
    assert sorted(行["源列表"]) == ["网点-东站", "网点-西港"]


def test_关窗锁_缺互斥直接双成功会被拒():
    装载.装一批(双源同运单)
    conn = 连接()
    try:
        conn.execute(
            "INSERT INTO 关窗锁(编号,窗号,加锁时,批次) VALUES(?,?,?,?)",
            ("旧锁", "W-demo", "2026-11-24T00:00:00Z", "遗留"),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(关窗.关窗互斥错误):
        关窗.关当前窗()
    # 被拒后缓冲原样保留，没有产生片
    assert _切片("运单-M") == []
    assert 目录.缓冲列表() and all(r["命运"] == "在缓冲" for r in 目录.缓冲列表())


def test_双源并发_不得混成无归档的单行():
    多源.设策略(隔离.运单合并)
    装载.装一批(双源同运单)
    就绪 = threading.Barrier(2)
    结局 = []

    def 抢关():
        就绪.wait()
        try:
            结局.append(("ok", 关窗.关当前窗()))
        except Exception as e:  # 互斥拒或写锁忙都算没双成功
            结局.append(("err", type(e).__name__))

    t1 = threading.Thread(target=抢关)
    t2 = threading.Thread(target=抢关)
    t1.start(); t2.start(); t1.join(15); t2.join(15)
    片 = _切片("运单-M")
    档 = _归档("运单-M")
    assert len(片) == 1, "并发关窗不得重复物化"
    assert len(档) == 2, "禁止混成无归档的单行片"
    assert sum(a["事件数"] for a in 档) == 2
    assert 贯验.跑一次()["是否对齐"] is True


def test_双源并发_复合口径仍恰好两行片():
    assert 多源.当前策略() == 隔离.复合隔离
    装载.装一批(双源同运单)
    就绪 = threading.Barrier(2)
    结局 = []

    def 抢关():
        就绪.wait()
        try:
            结局.append(("ok", 关窗.关当前窗()))
        except Exception as e:
            结局.append(("err", type(e).__name__))

    t1 = threading.Thread(target=抢关)
    t2 = threading.Thread(target=抢关)
    t1.start(); t2.start(); t1.join(15); t2.join(15)
    片 = _切片("运单-M")
    assert len(片) == 2
    assert {p["源键"] for p in 片} == {"网点-东站", "网点-西港"}


def test_按源汇总物化同成同败_坏策略整笔不落地():
    装载.装一批(双源同运单)
    目录.改窗规({隔离.窗规键: "坏口径"})
    with pytest.raises(ValueError):
        关窗.关当前窗()
    # 失败后无片无归档，缓冲仍在，关窗锁不残留
    assert _切片("运单-M") == []
    assert _归档("运单-M") == []
    conn = 连接()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM 关窗锁").fetchone()["c"] == 0
        assert all(r["命运"] == "在缓冲" for r in conn.execute(
            "SELECT 命运 FROM 入窗缓冲").fetchall())
    finally:
        conn.close()


def test_未登记网点源装载即驳回():
    报告 = 装载.装一批({
        "批次编号": "野源",
        "事件": [{"事件编号": "X-1", "运单键": "运单-X", "网点键": "网点-火星",
                  "事件时": "2026-11-24T00:09:00Z"}],
    })
    assert 报告["驳回"] == 1
    assert any("未登记源" in m["说明"] for m in 报告["明细"])
    conn = 连接()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM 观测点").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM 入窗缓冲").fetchone()["c"] == 0
    finally:
        conn.close()


def test_贯验附加检查_撕归档即不对齐():
    多源.设策略(隔离.运单合并)
    装载.装一批(双源同运单)
    关窗.关当前窗()
    assert 贯验.跑一次()["是否对齐"] is True
    # 人为撕掉一行源归档，附加贯验必须抓到
    conn = 连接()
    try:
        conn.execute("DELETE FROM 多源归档 WHERE 源键=?", ("网点-西港",))
        conn.commit()
    finally:
        conn.close()
    验 = 贯验.跑一次()
    assert 验["是否对齐"] is False
    assert "多源" in 验["说明"]


def test_夹具_多源同运单样例可装可切():
    体 = 夹具.读取("多源同运单样例")
    报告 = 装载.装一批(体)
    assert 报告["驳回"] == 0
    assert 报告["入缓冲"] >= 2
    关窗.关当前窗()
    视图 = {v["运单键"]: v for v in 多源.运单隔离视图()}
    assert 视图["运单-D"]["片行数"] == 2
    assert sorted(视图["运单-D"]["源列表"]) == ["网点-东站", "网点-西港"]


def test_http_只读接口与策略改写():
    client = TestClient(app)
    r = client.get("/api/多源隔离")
    assert r.status_code == 200
    assert r.json()["策略"] == 隔离.复合隔离
    r2 = client.post("/api/多源隔离/策略", json={"值": 隔离.运单合并})
    assert r2.status_code == 200 and r2.json()["策略"] == 隔离.运单合并
    r3 = client.post("/api/多源隔离/策略", json={"值": "瞎拼"})
    assert r3.status_code == 400
    r4 = client.get("/api/多源隔离/归档")
    assert r4.status_code == 200 and isinstance(r4.json()["归档"], list)
