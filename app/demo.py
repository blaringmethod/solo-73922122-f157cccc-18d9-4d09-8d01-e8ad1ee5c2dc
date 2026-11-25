"""
网页演示场景：一次换月 + 换月参考日旧合约缺价 + 一笔关键报价补录前后的最大回落变化。

日历（8 个交易日）：
  2026-03-02(一) 03-03 03-04 03-05(换月 A→B) 03-06 03-09(一) 03-10 03-11

边权（后一日在用合约的两笔价，后日-前日）：
  03-02→03-03  A: 40880-41000 = -120
  03-03→03-04  A: 缺 A@03-04  → 断点
  03-04→03-05  B: 39800-40000 = -200   ← 换月边：尽管 A@03-04 缺价，仍按规则可算
  03-05→03-06  B: 39850-39800 =  +50
  03-06→03-09  B: 缺 B@03-09  → 断点（补价前与下一条边同时断开）
  03-09→03-10  B: 缺 B@03-09  → 断点
  03-10→03-11  B: 39310-39400 =  -90

补价前最大回落：200 分，2026-03-04 → 2026-03-05（恰好是换月边）。
补入 B@2026-03-09 = 39550 后：
  03-06→03-09 = -300，03-09→03-10 = -150，
  03-04→03-11 累计 = -200+50-300-150-90 = -690 → 最大回落 690 分。
A@03-04 始终不补，因此 03-03→03-04 的断点一直在，演示“不能跨过断点”。
"""

from __future__ import annotations

from . import db

DEMO_START = "2026-03-02"
DEMO_END = "2026-03-11"

CALENDAR = [
    "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05",
    "2026-03-06", "2026-03-09", "2026-03-10", "2026-03-11",
]

INITIAL_CODE = "DEMO_A"
ROLL_DATE = "2026-03-05"
NEW_CODE = "DEMO_B"

# (code, date, price)；注意 DEMO_A@03-04 与 DEMO_B@03-09 故意缺失
PRICES_BEFORE = [
    ("DEMO_A", "2026-03-02", 41000),
    ("DEMO_A", "2026-03-03", 40880),
    # DEMO_A 2026-03-04 故意不登记：换月参考日旧合约缺价
    ("DEMO_B", "2026-03-04", 40000),
    ("DEMO_B", "2026-03-05", 39800),
    ("DEMO_B", "2026-03-06", 39850),
    # DEMO_B 2026-03-09 补价前故意缺失
    ("DEMO_B", "2026-03-10", 39400),
    ("DEMO_B", "2026-03-11", 39310),
]

FILL_CONTRACT_CODE = "DEMO_B"
FILL_DATE = "2026-03-09"
FILL_PRICE = 39550

# 供页面/自测对照的预期结果
BEFORE_EXPECTED = {"amount": 200, "start": "2026-03-04", "end": "2026-03-05"}
AFTER_EXPECTED = {"amount": 690, "start": "2026-03-04", "end": "2026-03-11"}


def seed_before(conn) -> dict:
    """清空现有数据并载入“补价前”演示场景。"""
    db.reset_all_data(conn)
    db.replace_calendar(conn, CALENDAR)
    db.setup_plan(conn, INITIAL_CODE, [(ROLL_DATE, NEW_CODE)])
    ids = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
    for code, day, price in PRICES_BEFORE:
        pv = db.upsert_price(conn, ids[code], day, price)
        if not pv["committed"]:
            raise db.AppError(f"演示数据写入失败：{code} {day} {pv.get('error')}")
    return db.find_max_drawdown(conn, DEMO_START, DEMO_END)


def _fill_contract_id(conn) -> int:
    for c in db.list_contracts(conn):
        if c["code"] == FILL_CONTRACT_CODE:
            return c["contract_id"]
    raise db.AppError("演示合约不存在，请先载入演示场景")


def fill_key_quote(conn) -> tuple[dict, dict]:
    """补入关键报价；返回 (补价前结果, 补价后结果)。"""
    before = db.find_max_drawdown(conn, DEMO_START, DEMO_END)
    cid = _fill_contract_id(conn)
    pv = db.upsert_price(conn, cid, FILL_DATE, FILL_PRICE)
    if not pv["committed"]:
        raise db.AppError(f"补价保存失败：{pv.get('error')}")
    after = db.find_max_drawdown(conn, DEMO_START, DEMO_END)
    return before, after


def unfill_key_quote(conn) -> dict:
    """删除关键报价，回到补价前，便于反复演示。"""
    cid = _fill_contract_id(conn)
    db.delete_price(conn, cid, FILL_DATE)
    return db.find_max_drawdown(conn, DEMO_START, DEMO_END)
