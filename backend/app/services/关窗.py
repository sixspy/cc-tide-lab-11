"""关窗：缓冲按（源、运单）物化为窗切片。

多源串扰隔离接线：
- 互斥锁 关窗锁：并发关窗只准一个进，缺互斥会双成功；
- 复合隔离：切片键 = 窗号 + 源键 + 运单键，两源两行片；
- 运单合并：同运单现行一片，源明细写只读 多源归档；
- 按源汇总再物化，单事务同成同败，异常整笔回滚并释放锁。
"""
from __future__ import annotations
import json
import uuid
from collections import defaultdict
from app.db import 连接
from app.seed import 此刻
from app.services import 目录
from app.modules import 多源串扰隔离 as 隔离


class 关窗互斥错误(RuntimeError):
    """已有一关窗在跑，互斥拒绝。"""


# 测试/编排切口：拿到锁后、提交前可注入同步屏障
屏障 = None


def _加锁(conn, 窗号: str, 批: str) -> None:
    持 = conn.execute("SELECT COUNT(*) AS c FROM 关窗锁").fetchone()["c"]
    if 持:
        raise 关窗互斥错误(f"关窗互斥：窗{窗号}已有关窗在跑，本笔拒绝")
    conn.execute(
        "INSERT INTO 关窗锁(编号,窗号,加锁时,批次) VALUES(?,?,?,?)",
        (隔离.锁编号, 窗号, 此刻(), 批),
    )


def 关当前窗() -> dict:
    规 = 目录.窗规()
    窗号 = 规.get("当前窗号", "W-demo")
    窗起 = 规.get("窗起", "")
    窗止 = 规.get("窗止", "")
    形态值 = 隔离.规整体例(规.get(隔离.窗规键, 隔离.默认形态))
    批 = f"关窗-{uuid.uuid4().hex[:8]}"
    conn = 连接()
    try:
        # 立即拿写锁，两线程并发时后者在写锁上排队，再被互斥行拒绝
        conn.execute("BEGIN IMMEDIATE")
        try:
            _加锁(conn, 窗号, 批)
            隔离.before_close(窗号)
            rows = conn.execute(
                "SELECT * FROM 入窗缓冲 WHERE 窗号=? AND 命运='在缓冲'", (窗号,)
            ).fetchall()

            # 按源汇总：先 (运单, 源) 分组，物化口径再决定片的切法
            按运单源: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
            for r in rows:
                按运单源[r["运单键"]][r["网点键"]].append(r)

            成片 = 0
            归档笔 = 0
            时刻 = 此刻()
            片清单: list[dict] = []

            if 形态值 == 隔离.复合隔离:
                for 运单, 源组 in 按运单源.items():
                    for 源, evs in 源组.items():
                        # 切片键 = 窗号 + 源键 + 运单键
                        切片 = f"片-{窗号}-{源}-{运单}-{uuid.uuid4().hex[:6]}"
                        conn.execute(
                            """INSERT INTO 窗切片账
                               (切片编号,窗号,运单键,源键,隔离形态,事件数,窗起,窗止,物化时,命运)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (切片, 窗号, 运单, 源, 形态值, len(evs), 窗起, 窗止, 时刻, "已成片"),
                        )
                        片清单.append({"切片编号": 切片, "运单键": 运单, "源键": 源, "事件数": len(evs)})
                        for e in evs:
                            conn.execute(
                                "UPDATE 入窗缓冲 SET 命运=? WHERE 事件编号=?",
                                ("已物化", e["事件编号"]),
                            )
                            conn.execute(
                                "UPDATE 观测点 SET 命运=? WHERE 事件编号=?",
                                ("已成片", e["事件编号"]),
                            )
                        成片 += 1
            else:  # 运单合并：同运单一现行片 + 只读源归档
                for 运单, 源组 in 按运单源.items():
                    evs_all = [e for 组 in 源组.values() for e in 组]
                    切片 = f"片-{窗号}-{运单}-{uuid.uuid4().hex[:6]}"
                    conn.execute(
                        """INSERT INTO 窗切片账
                           (切片编号,窗号,运单键,源键,隔离形态,事件数,窗起,窗止,物化时,命运)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (切片, 窗号, 运单, "", 形态值, len(evs_all), 窗起, 窗止, 时刻, "已成片"),
                    )
                    for 源, evs in 源组.items():
                        conn.execute(
                            """INSERT INTO 多源归档
                               (切片编号,窗号,运单键,源键,事件数,窗起,窗止,物化时)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (切片, 窗号, 运单, 源, len(evs), 窗起, 窗止, 时刻),
                        )
                        归档笔 += 1
                    片清单.append({"切片编号": 切片, "运单键": 运单, "源键": "", "事件数": len(evs_all)})
                    for e in evs_all:
                        conn.execute(
                            "UPDATE 入窗缓冲 SET 命运=? WHERE 事件编号=?",
                            ("已物化", e["事件编号"]),
                        )
                        conn.execute(
                            "UPDATE 观测点 SET 命运=? WHERE 事件编号=?",
                            ("已成片", e["事件编号"]),
                        )
                    成片 += 1

            if 屏障 is not None:
                屏障.wait()

            报告 = {
                "批次编号": 批,
                "窗号": 窗号,
                "隔离形态": 形态值,
                "成片数": 成片,
                "归档笔数": 归档笔,
                "缓冲消耗": len(rows),
                "片清单": 片清单,
            }
            conn.execute(
                """INSERT INTO 关窗批次(编号,窗号,时刻,成片数,报告) VALUES (?,?,?,?,?)""",
                (批, 窗号, 时刻, 成片, json.dumps(报告, ensure_ascii=False)),
            )
            conn.execute("DELETE FROM 关窗锁 WHERE 编号=?", (隔离.锁编号,))
            conn.commit()
            return 隔离.after_close(报告)
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
