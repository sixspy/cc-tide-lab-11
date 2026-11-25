"""多源串扰隔离（已接线）。

东站、西港等网点源上报同一运单键时，关窗物化必须选定工程形态，
两种形态由窗规「多源隔离策略」钉死，可切换、可只读观测：

- 复合隔离（默认）：切片键 = 窗号 + 源键 + 运单键，两源写出两行片，
  源与源永不进同一片。
- 运单合并：同运单合并进现行一片，源明细进只读「多源归档」，
  下游读归档区分来源，缺归档即混源。

硬约束：
- 按源汇总再物化，单事务同成同败；
- 关窗加互斥锁，两源并发关窗不得双成功、不得混成无归档的单行；
- 只改页面徽章不算过，装载/关窗/贯验/只读接口一起接线。
"""
from __future__ import annotations

NAME = "多源串扰隔离"
CONTRACT = "不同网点源的同运单事件不得混进同一切片"

复合隔离 = "复合隔离"
运单合并 = "运单合并"
形态 = (复合隔离, 运单合并)
默认形态 = 复合隔离
窗规键 = "多源隔离策略"

HOOKS = (
    "before_load",
    "after_load",
    "decide_window",
    "before_close",
    "after_close",
    "extra_verify_checks",
)

# 关窗互斥锁的单行编号
锁编号 = "关窗-互斥锁"


def describe() -> dict:
    return {
        "模块": NAME,
        "已接线": True,
        "契约": CONTRACT,
        "形态": list(形态),
        "默认形态": 默认形态,
        "窗规键": 窗规键,
        "钩子": list(HOOKS),
        "说明": "复合隔离=源键+运单键两行片；运单合并=现行一片+只读源归档",
    }


def 规整体例(值: str) -> str:
    """窗规值归一，未知口径直接拒，不静默回落。"""
    v = str(值 or "").strip()
    if v not in 形态:
        raise ValueError(f"未知多源隔离策略:{v}（允许 {形态[0]} / {形态[1]}）")
    return v


def before_load(batch: dict, 已登记源: set[str] | None = None) -> dict:
    """装载前预检：事件网点源必须在网点源目录登记，未登记即驳回。"""
    事件 = (batch or {}).get("事件") or []
    if 已登记源:
        for ev in 事件:
            if isinstance(ev, dict) and ev.get("网点键") and ev["网点键"] not in 已登记源:
                raise ValueError(f"未登记源:{ev['网点键']}")
    return batch


def after_load(report: dict) -> dict:
    """装载后：附加按源入缓冲计数，供页面读到源维度命运。"""
    report = report or {}
    按源: dict[str, int] = {}
    for m in report.get("明细", []):
        if isinstance(m, dict) and m.get("命运") == "入缓冲":
            # 明细不含源键时由装载服务回填 按源缓冲；此处仅兜底
            源 = m.get("网点键")
            if 源:
                按源[源] = 按源.get(源, 0) + 1
    report["按源入缓冲"] = 按源
    return report


def decide_window(event: dict, rules: dict) -> str:
    """入窗判定沿用基线事件时口径，本模块不旁路事件。"""
    return "入缓冲"


def before_close(window_id: str) -> None:
    """关窗前检查由关窗服务在互斥锁内调用；本钩子保留扩展位。"""
    return None


def after_close(report: dict) -> dict:
    return report


def extra_verify_checks(snapshot: dict) -> list[str]:
    """附加贯验：多源隔离不变量。返回中文错误文案，空列表为通过。

    snapshot 由贯验服务传入，含：
    - 片行: list[dict]（窗切片账全量）
    - 归档: list[dict]（多源归档全量）
    - 已物化缓冲: int
    - 切片事件: int
    """
    错: list[str] = []
    片行 = snapshot.get("片行") or []
    归档 = snapshot.get("归档") or []

    # 1) 片必须带隔离形态与源键（复合片按源、合并片留空并由归档承载）
    for p in 片行:
        形态值 = p.get("隔离形态", "")
        if 形态值 not in 形态:
            错.append(f"多源：切片{p.get('切片编号', '?')}缺隔离形态")
            continue
        if 形态值 == 复合隔离 and not p.get("源键"):
            错.append(f"多源：复合切片{p.get('切片编号', '?')}缺源键")

    # 2) 复合口径：同窗同运单不同源必须是不同片，片源不得重复
    复合键: set[tuple] = set()
    for p in 片行:
        if p.get("隔离形态") == 复合隔离:
            k = (p.get("窗号"), p.get("运单键"), p.get("源键"))
            if k in 复合键:
                错.append(f"多源：复合片源重复{k}")
            复合键.add(k)

    # 3) 合并口径：同窗同运单只准一片，且每片必须有归档源明细
    合并片 = [p for p in 片行 if p.get("隔离形态") == 运单合并]
    合并键: dict[tuple, str] = {}
    for p in 合并片:
        k = (p.get("窗号"), p.get("运单键"))
        if k in 合并键:
            错.append(f"多源：合并口径同运单出现多片{k}")
        合并键[k] = p.get("切片编号", "")
    档按片: dict[str, list] = {}
    for a in 归档:
        档按片.setdefault(a.get("切片编号", ""), []).append(a)
    for k, 片号 in 合并键.items():
        源档 = 档按片.get(片号, [])
        if not 源档:
            错.append(f"多源：合并片{片号}缺只读源归档（混成无归档单行）")
            continue
        if len({a.get("源键") for a in 源档}) != len(源档):
            错.append(f"多源：合并片{片号}归档源重复")
        片事件 = next((p.get("事件数", 0) for p in 合并片 if p.get("切片编号") == 片号), 0)
        if sum(int(a.get("事件数", 0)) for a in 源档) != int(片事件):
            错.append(f"多源：合并片{片号}归档事件数与片账不符")

    # 4) 归档不得悬挂到不存在的片
    片号集 = {p.get("切片编号") for p in 片行}
    for a in 归档:
        if a.get("切片编号") not in 片号集:
            错.append(f"多源：归档悬挂到不存在切片{a.get('切片编号', '?')}")

    return 错
