"""
连续合约结算价 —— 数据访问与计算层

核心模型
========
合约序列：初始合约 ord=0；第 k 次换月启用的合约 ord=k（k>=1），换月记录在 rolls(ord=k)。
某日 d 在用合约 = ord 满足：roll ord r 的 switch_date > d 的最小 r（即 roll r-1 已生效、r 未生效），
等价于 ord = count(rolls.switch_date <= d)，该 ord 即 rolls 中严格大于 d 的最小 ord 再减 1。

连续价（基准日 B）
==================
cont(d, B) = price(contract(d), d) + Σ { delta(r) | d < switch_date(r) <= B }
其中 delta(r) = price(new_r, pre_date_r) - price(old_r, pre_date_r)，
pre_date_r 为切换前一交易日；任一所需报价缺失则 delta 为 NULL。
缺价时连续价留空，并指出：
  - 当天原价缺失；或
  - 区间内最早一个 delta 为空的换月，以及该换月待补的合约报价。

复杂度保证
==========
- 换月价差以 Fenwick 树（bit_sums 表）维护：区间求和、单点更新均为 O(log n)，
  辅助记录数 ≤ n+1（n=换月次数），随换月次数对数级增长。
- 连续价不落地：单笔报价更新只写 prices 与至多一个 delta + Fenwick 单点，
  绝不逐日回写连续价。
- 单次取数在一个只读快照事务内完成（同一已提交状态），逐日用对数级查询。

事务语义
========
- 报价更新（新增/更正）在单个 IMMEDIABLE 事务内提交：原价与受影响价差一起生效，
  不会出现“价已改、价差未改”的中间状态。
- 保存失败时事务整体回滚，调用方仍可拿到本次计算的预期结果用于页面展示。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import date as date_cls, datetime
from typing import Optional


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

ISO_FMT = "%Y-%m-%d"


def iso(d: date_cls) -> str:
    return d.strftime(ISO_FMT)


def parse_iso(s: str) -> date_cls:
    return datetime.strptime(s, ISO_FMT).date()


def valid_iso(s: str) -> bool:
    try:
        parse_iso(s)
        return True
    except (ValueError, TypeError):
        return False


class AppError(Exception):
    """面向用户的业务错误（中文消息可直接展示）。"""


# --------------------------------------------------------------------------- #
# 数据库连接
# --------------------------------------------------------------------------- #

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_db_path() -> str:
    return os.environ.get("CONT_DB_PATH", os.path.join(os.getcwd(), "data", "continuous.db"))


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())


@contextmanager
def snapshot(conn: sqlite3.Connection):
    """
    只读快照事务：整次取数只看到同一个已提交状态。
    SQLite 默认 DEFERRED 事务的首条 SELECT 即获取读快照，事务结束前不变。
    若事务内顺带做了幂等的回落索引补建（派生数据），正常结束一并提交；
    任何异常都整体回滚，绝不留下半建索引。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """立即写事务：保证“原价 + 受影响价差”原子提交，失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------- #
# 基础配置维护：交易日历 / 合约 / 换月计划
# --------------------------------------------------------------------------- #

def list_calendar(conn: sqlite3.Connection) -> list[str]:
    return [r["date"] for r in conn.execute("SELECT date FROM calendar ORDER BY date")]


def replace_calendar(conn: sqlite3.Connection, dates: list[str]) -> int:
    """全量替换交易日历（去重、校验格式、排序）。"""
    cleaned = sorted({d.strip() for d in dates if d and d.strip()})
    bad = [d for d in cleaned if not valid_iso(d)]
    if bad:
        raise AppError(f"交易日日期格式非法：{bad[0]}，应为 YYYY-MM-DD")
    with write_tx(conn):
        conn.execute("DELETE FROM calendar")
        conn.executemany("INSERT INTO calendar(date) VALUES (?)", [(d,) for d in cleaned])
        # 日历变了：回落索引按新配置全量重建（允许的例外路径），与日历同事务生效
        _rebuild_drawdown_index_locked(conn)
    return len(cleaned)


def prev_trade_date(conn: sqlite3.Connection, day: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM calendar WHERE date < ?", (day,)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def is_trade_date(conn: sqlite3.Connection, day: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM calendar WHERE date = ?", (day,)
    ).fetchone() is not None


def list_contracts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT c.contract_id, c.code, c.ord, "
        "  (SELECT COUNT(*) FROM prices p WHERE p.contract_id = c.contract_id) AS price_count "
        "FROM contracts c ORDER BY c.ord"
    ))


def get_contract(conn: sqlite3.Connection, contract_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()


def contract_by_ord(conn: sqlite3.Connection, ord_: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE ord = ?", (ord_,)
    ).fetchone()


def list_rolls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT r.ord, r.switch_date, r.pre_date, r.new_contract_id, r.delta, "
        "       nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "ORDER BY r.ord"
    ))


def setup_plan(conn: sqlite3.Connection, initial_code: str,
               switches: list[tuple[str, str]]) -> None:
    """
    （重新）登记初始合约与固定换月计划。
    switches: 按时间顺序的 [(switch_date, new_contract_code), ...]
    仅允许在尚无任何报价/价差数据时整体重建，避免历史数据错配。
    """
    initial_code = (initial_code or "").strip()
    if not initial_code:
        raise AppError("初始合约代码不能为空")
    plan: list[tuple[str, str]] = []
    seen_dates: set[str] = set()
    prev = None
    for sd, code in switches:
        sd = (sd or "").strip()
        code = (code or "").strip()
        if not sd or not code:
            continue
        if not valid_iso(sd):
            raise AppError(f"换月切换日格式非法：{sd}")
        if sd in seen_dates:
            raise AppError(f"换月切换日重复：{sd}")
        if prev is not None and sd <= prev:
            raise AppError(f"换月切换日必须严格递增：{prev} 之后不能是 {sd}")
        if not is_trade_date(conn, sd):
            raise AppError(f"换月切换日 {sd} 不在交易日历中")
        seen_dates.add(sd)
        plan.append((sd, code))
        prev = sd

    with write_tx(conn):
        n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
        if n_prices or n_rolls:
            raise AppError("已存在报价或换月数据，不能重建计划；如需调整请先清空数据")
        conn.execute("DELETE FROM rolls")
        conn.execute("DELETE FROM contracts")
        conn.execute("DELETE FROM bit_sums")

        cur = conn.execute(
            "INSERT INTO contracts(code, ord) VALUES (?, 0)", (initial_code,)
        )
        init_id = cur.lastrowid
        conn.execute("UPDATE meta SET initial_contract_id = ? WHERE id = 1", (init_id,))
        for k, (sd, code) in enumerate(plan, start=1):
            cur = conn.execute(
                "INSERT INTO contracts(code, ord) VALUES (?, ?)", (code, k)
            )
            new_id = cur.lastrowid
            pre = prev_trade_date(conn, sd)
            if pre is None:
                raise AppError(f"切换日 {sd} 之前没有交易日，无法取换月价差")
            conn.execute(
                "INSERT INTO rolls(ord, switch_date, pre_date, new_contract_id, delta) "
                "VALUES (?, ?, ?, ?, NULL)",
                (k, sd, pre, new_id),
            )
        # 计划变了：在用合约归属可能全变，按新计划全量重建回落索引
        _rebuild_drawdown_index_locked(conn)


# --------------------------------------------------------------------------- #
# Fenwick 树：换月价差的对数级前缀和
# --------------------------------------------------------------------------- #

def _bit_add(conn: sqlite3.Connection, n: int, pos: int, v: int) -> None:
    i = pos
    while i <= n:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO bit_sums(pos, val) VALUES (?, ?)", (i, v)
            )
        else:
            conn.execute(
                "UPDATE bit_sums SET val = val + ? WHERE pos = ?", (v, i)
            )
        i += i & -i


def _bit_sum(conn: sqlite3.Connection, pos: int) -> int:
    total = 0
    i = pos
    while i > 0:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is not None:
            total += row["val"]
        i -= i & -i
    return total


def range_delta_sum(conn: sqlite3.Connection, lo_ord: int, hi_ord: int) -> int:
    """换月序号闭区间 [lo_ord, hi_ord] 内已知价差之和（调用前需确认区间内无 NULL）。"""
    if lo_ord > hi_ord:
        return 0
    return _bit_sum(conn, hi_ord) - _bit_sum(conn, lo_ord - 1)


# --------------------------------------------------------------------------- #
# 报价维护
# --------------------------------------------------------------------------- #

def get_price(conn: sqlite3.Connection, contract_id: int, day: str) -> Optional[int]:
    row = conn.execute(
        "SELECT price FROM prices WHERE contract_id = ? AND date = ?",
        (contract_id, day),
    ).fetchone()
    return row["price"] if row else None


def list_prices(conn: sqlite3.Connection, contract_id: Optional[int] = None,
                limit: int = 500) -> list[sqlite3.Row]:
    if contract_id is None:
        return list(conn.execute(
            "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
            "JOIN contracts c ON c.contract_id = p.contract_id "
            "ORDER BY p.date DESC, c.ord LIMIT ?", (limit,)
        ))
    return list(conn.execute(
        "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
        "JOIN contracts c ON c.contract_id = p.contract_id "
        "WHERE p.contract_id = ? ORDER BY p.date DESC LIMIT ?",
        (contract_id, limit),
    ))


def _recompute_roll_delta(conn: sqlite3.Connection, ord_: int) -> Optional[int]:
    """
    依据当前 prices 重算第 ord_ 次换月的价差并更新 rolls/bit_sums。
    返回新的 delta（缺报价为 None）。
    """
    roll = conn.execute("SELECT * FROM rolls WHERE ord = ?", (ord_,)).fetchone()
    if roll is None:
        return None
    n = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    old = contract_by_ord(conn, ord_ - 1)
    new_id = roll["new_contract_id"]
    p_old = get_price(conn, old["contract_id"], roll["pre_date"])
    p_new = get_price(conn, new_id, roll["pre_date"])
    new_delta = (p_new - p_old) if (p_old is not None and p_new is not None) else None

    old_delta = roll["delta"]
    if new_delta != old_delta:
        conn.execute("UPDATE rolls SET delta = ? WHERE ord = ?", (new_delta, ord_))
        # Fenwick 只承载非空值：旧值撤下、新值加入，均为 O(log n)
        if old_delta is not None:
            _bit_add(conn, n, ord_, -old_delta)
        if new_delta is not None:
            _bit_add(conn, n, ord_, new_delta)
    return new_delta


def upsert_price(conn: sqlite3.Connection, contract_id: int, day: str,
                 price: int) -> dict:
    """
    新增/更正一笔整数分结算价。
    原子事务内完成：
      1) prices 写入（原价与价差一起生效）；
      2) 仅重算“价差参考日 = 该日”的换月（至多 1 个，因为 pre_date 唯一）；
      3) 不触碰任何连续价、不逐日回写。
    返回预览信息（即使提交失败，调用方也可展示计算结果）。
    """
    if not valid_iso(day):
        raise AppError(f"日期格式非法：{day}，应为 YYYY-MM-DD")
    if not isinstance(price, int) or isinstance(price, bool):
        raise AppError("结算价必须是整数（单位：分）")
    if not is_trade_date(conn, day):
        raise AppError(f"{day} 不在交易日历中，不能登记报价")

    contract = get_contract(conn, contract_id)
    if contract is None:
        raise AppError(f"合约不存在：id={contract_id}")

    affected_roll = conn.execute(
        "SELECT ord FROM rolls WHERE pre_date = ?", (day,)
    ).fetchone()
    before = get_price(conn, contract_id, day)
    preview = {
        "contract": contract["code"], "date": day, "price": price,
        "old_price": before, "is_correction": before is not None,
        "affected_roll_ord": affected_roll["ord"] if affected_roll else None,
        "committed": False,
    }

    try:
        with write_tx(conn):
            # 必须在本事务写 prices（会经触发器推高 rev）之前读取索引版本状态
            dd_state_pre = _dd_state_row(conn)
            conn.execute(
                "INSERT INTO prices(contract_id, date, price) VALUES (?, ?, ?) "
                "ON CONFLICT(contract_id, date) DO UPDATE SET price = excluded.price",
                (contract_id, day, price),
            )
            if affected_roll is not None:
                new_delta = _recompute_roll_delta(conn, affected_roll["ord"])
                preview["new_delta"] = new_delta
            # 报价 + 换月价差 + 回落索引在同一事务生效；失败整体回滚，照旧索引供数
            _sync_drawdown_after_price_write(conn, dd_state_pre, contract_id, day)
        # 事务已由上下文管理器提交：原价、价差与回落索引同时生效
        preview["committed"] = True
    except sqlite3.Error as e:
        # 保存失败已整体回滚；预期结果仍随 preview 返回供页面展示
        preview["committed"] = False
        preview["error"] = str(e)
    return preview


# --------------------------------------------------------------------------- #
# 取数：逐日连续价
# --------------------------------------------------------------------------- #

def active_contract_ord(conn: sqlite3.Connection, day: str) -> int:
    """
    当日在用合约的 ord：切换日 <= day 的换月次数。
    走 switch_date 唯一索引的反向定位（LIMIT 1），单次 O(log n)，
    不做区间计数扫描；因 ord 稠密且与切换日同序，取到的最大 ord 即次数。
    """
    row = conn.execute(
        "SELECT ord FROM rolls WHERE switch_date <= ? "
        "ORDER BY switch_date DESC LIMIT 1",
        (day,),
    ).fetchone()
    return row["ord"] if row else 0


def _first_missing_roll(conn: sqlite3.Connection, lo_ord: int,
                        hi_ord: int) -> Optional[sqlite3.Row]:
    """区间 [lo_ord, hi_ord] 内最早一个缺价差的换月。"""
    if lo_ord > hi_ord:
        return None
    return conn.execute(
        "SELECT r.*, nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "WHERE r.ord BETWEEN ? AND ? AND r.delta IS NULL "
        "ORDER BY r.ord LIMIT 1",
        (lo_ord, hi_ord),
    ).fetchone()


def _missing_quote_contracts(conn: sqlite3.Connection,
                             roll: sqlite3.Row) -> list[sqlite3.Row]:
    """某换月价差缺失时，具体缺 pre_date 上哪个合约的报价。"""
    missing = []
    old_c = contract_by_ord(conn, roll["ord"] - 1)
    new_c = get_contract(conn, roll["new_contract_id"])
    for c in (old_c, new_c):
        if get_price(conn, c["contract_id"], roll["pre_date"]) is None:
            missing.append(c)
    return missing


def fetch_range(conn: sqlite3.Connection, start: str, end: str,
                base: str) -> list[dict]:
    """
    取 [start, end] 交易日的连续价，基准日 base（不早于 end）。
    整个过程在一个只读快照事务中执行，看到的是同一已提交状态。
    逐日查询：在用合约定位 O(log n)（唯一索引反向定位）、区间价差和 O(log n)（Fenwick）。
    """
    for label, v in (("开始日", start), ("结束日", end), ("基准日", base)):
        if not valid_iso(v):
            raise AppError(f"{label}格式非法：{v}，应为 YYYY-MM-DD")
    if start > end:
        raise AppError("开始日不能晚于结束日")
    if base < end:
        raise AppError(f"基准日 {base} 不能早于结束日 {end}")

    rows: list[dict] = []
    with snapshot(conn):
        # 所有读取均在同一只读快照内：整次取数只看到一个已提交状态
        base_row = conn.execute(
            "SELECT ord FROM rolls WHERE switch_date <= ? "
            "ORDER BY switch_date DESC LIMIT 1",
            (base,),
        ).fetchone()
        base_roll_hi = base_row["ord"] if base_row else 0
        days = [r["date"] for r in conn.execute(
            "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        )]
        if not days:
            raise AppError(f"{start} 至 {end} 之间没有交易日")

        for day in days:
            ord_ = active_contract_ord(conn, day)
            contract = contract_by_ord(conn, ord_)
            raw = get_price(conn, contract["contract_id"], day)

            # 该日之后至基准日（含）的换月：switch_date in (day, base] → ord in (ord_, base_roll_hi]
            lo, hi = ord_ + 1, base_roll_hi
            miss_roll = _first_missing_roll(conn, lo, hi)

            row = {
                "date": day,
                "contract": contract["code"],
                "raw_price": raw,
                "continuous": None,
                "status": "ok",
                "message": "",
            }
            if raw is None:
                row["status"] = "missing_raw"
                row["message"] = f"缺少原价：合约 {contract['code']} 在 {day} 的结算价待补"
            elif miss_roll is not None:
                row["status"] = "missing_roll"
                miss_cs = _missing_quote_contracts(conn, miss_roll)
                names = "、".join(
                    f"{c['code']}({miss_roll['pre_date']})" for c in miss_cs
                ) or "价差所需报价"
                row["message"] = (
                    f"换月 {miss_roll['old_code']} → {miss_roll['new_code']} "
                    f"（切换日 {miss_roll['switch_date']}）缺价差，待补报价：{names}"
                )
            else:
                adj = range_delta_sum(conn, lo, hi)
                row["continuous"] = raw + adj
                row["adjustment"] = adj
            rows.append(row)
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["交易日", "在用合约", "原价(分)", "连续价(分)", "状态", "说明"])
    status_cn = {"ok": "正常", "missing_raw": "缺原价", "missing_roll": "缺换月报价"}
    for r in rows:
        writer.writerow([
            r["date"], r["contract"],
            "" if r["raw_price"] is None else r["raw_price"],
            "" if r["continuous"] is None else r["continuous"],
            status_cn.get(r["status"], r["status"]),
            r["message"],
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 区间最大回落：相邻交易日边权 + 线段树索引
#
# 口径（与连续价的换月价差调整是两套独立算法）
# ------------------------------------------------
# 日历排序为 d_0..d_{n-1}。边 li 连接 d_li -> d_li+1，取“后一日在用合约” c：
#   w_li = price(c, d_li+1) - price(c, d_li)
# 两笔报价任一缺失，该边即为断点；任意两日 d_a -> d_b 的累计涨跌 =
# Σ_{li=a..b-1} w_li，且区间内不能含断点。最大回落 = 最小累计和的相反数。
# 同额裁决：较早的起日，再较早的止日（候选键 (和, a, b) 取最小）。
#
# 复杂度
# ------
# dd_node 是堆式线段树（叶编号 size+li）。区间查询只按键取 O(log n) 个节点；
# 一笔报价至多影响相邻两条边，每条边自叶向根改写 O(log n) 个节点。
# 日历/计划大改或检测到库外直接改价（rev 不一致）时允许全量重建；
# 旧库首次访问时 built_rev=-1，也走同一补建路径。
# --------------------------------------------------------------------------- #

class _Seg:
    """线段树节点摘要。候选统一为 (和, 起日全局下标, 止日全局下标)。"""

    __slots__ = ("cnt", "tot", "pref", "suff", "best")

    def __init__(self, cnt: int = 0, tot: Optional[int] = 0,
                 pref=None, suff=None, best=None):
        self.cnt = cnt        # 真实边数；0 表示补齐用的空段（合并单位元）
        self.tot = tot        # 整段和；非空段含断点时为 None
        self.pref = pref      # 最小和前缀候选（非空、不跨断点）
        self.suff = suff      # 最小和后缀候选
        self.best = best      # 最小和连续子段候选（即最大回落对应段）


def _better(c1, c2):
    """候选键 (和, 起日, 止日) 最小者：同额先早起日、再早止日。"""
    if c1 is None:
        return c2
    if c2 is None:
        return c1
    return c1 if (c1[0], c1[1], c1[2]) <= (c2[0], c2[1], c2[2]) else c2


def _merge_seg(a: _Seg, b: _Seg) -> _Seg:
    """左段 a 与右段 b 顺序拼接（运算满足结合律，空段为单位元）。"""
    if a.cnt == 0:
        return b
    if b.cnt == 0:
        return a
    cnt = a.cnt + b.cnt
    tot = None if (a.tot is None or b.tot is None) else a.tot + b.tot

    pref = a.pref
    # 横跨“整个 a + b 的最小前缀”的前缀；a.tot 非 None 才可能不跨断点
    if a.tot is not None and b.pref is not None:
        crossing = (a.tot + b.pref[0], a.pref[1], b.pref[2])
        pref = _better(pref, crossing)

    suff = b.suff
    if b.tot is not None and a.suff is not None:
        crossing = (a.suff[0] + b.tot, a.suff[1], b.suff[2])
        suff = _better(suff, crossing)

    best = _better(a.best, b.best)
    if a.suff is not None and b.pref is not None:
        crossing = (a.suff[0] + b.pref[0], a.suff[1], b.pref[2])
        best = _better(best, crossing)

    return _Seg(cnt, tot, pref, suff, best)


def _leaf_seg(li: int, w: int) -> _Seg:
    cand = (w, li, li + 1)
    return _Seg(1, w, cand, cand, cand)


def _broken_leaf_seg() -> _Seg:
    # cnt=1 与“补齐空段 cnt=0”区分；tot/候选全空即断点
    return _Seg(1, None, None, None, None)


def _dd_state_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM dd_state WHERE id = 1").fetchone()


def _row_to_seg(r: Optional[sqlite3.Row]) -> _Seg:
    if r is None:
        return _Seg()

    def cand(kind: str):
        v = r[f"{kind}_sum"]
        if v is None:
            return None
        return (v, r[f"{kind}_a"], r[f"{kind}_b"])

    return _Seg(r["cnt"], r["tot"], cand("pref"), cand("suff"), cand("best"))


def _dd_load_seg(conn: sqlite3.Connection, node: int) -> _Seg:
    """按键读取单个线段树节点记录（复杂度计数的基本读单位）。"""
    r = conn.execute("SELECT * FROM dd_node WHERE node = ?", (node,)).fetchone()
    return _row_to_seg(r)


_DD_UPSERT_SQL = (
    "INSERT INTO dd_node(node, cnt, tot, pref_sum, pref_a, pref_b, "
    "                    suff_sum, suff_a, suff_b, best_sum, best_a, best_b) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(node) DO UPDATE SET "
    "  cnt=excluded.cnt, tot=excluded.tot, "
    "  pref_sum=excluded.pref_sum, pref_a=excluded.pref_a, pref_b=excluded.pref_b, "
    "  suff_sum=excluded.suff_sum, suff_a=excluded.suff_a, suff_b=excluded.suff_b, "
    "  best_sum=excluded.best_sum, best_a=excluded.best_a, best_b=excluded.best_b"
)


def _dd_save_seg(conn: sqlite3.Connection, node: int, s: _Seg) -> None:
    """写入单个线段树节点记录（复杂度计数的基本写单位）。"""
    def flat(c):
        return (None, None, None) if c is None else (c[0], c[1], c[2])

    ps, pa, pb = flat(s.pref)
    ss, sa, sb = flat(s.suff)
    bs, ba, bb = flat(s.best)
    conn.execute(_DD_UPSERT_SQL, (
        node, s.cnt, s.tot, ps, pa, pb, ss, sa, sb, bs, ba, bb
    ))


def _calendar_days_and_ords(conn: sqlite3.Connection) -> tuple[list[str], list[int]]:
    days = [r["date"] for r in conn.execute("SELECT date FROM calendar ORDER BY date")]
    rolls = conn.execute(
        "SELECT ord, switch_date FROM rolls ORDER BY switch_date, ord"
    ).fetchall()
    ords: list[int] = []
    k = 0
    cur = 0
    for d in days:
        while k < len(rolls) and rolls[k]["switch_date"] <= d:
            cur = rolls[k]["ord"]
            k += 1
        ords.append(cur)
    return days, ords


def _build_leaf_segs(conn: sqlite3.Connection) -> tuple[list[str], list[_Seg]]:
    """按当前已提交的日历/计划/报价计算全部边叶（全量重建时使用，允许 O(n)）。"""
    days, ords = _calendar_days_and_ords(conn)
    contracts = {
        r["ord"]: r["contract_id"]
        for r in conn.execute("SELECT contract_id, ord FROM contracts")
    }
    prices = {
        (r["contract_id"], r["date"]): r["price"]
        for r in conn.execute("SELECT contract_id, date, price FROM prices")
    }
    segs: list[_Seg] = []
    for li in range(len(days) - 1):
        d_prev, d_next = days[li], days[li + 1]
        cid = contracts.get(ords[li + 1])  # 后一日在用合约
        if cid is None:
            segs.append(_broken_leaf_seg())
            continue
        p_next = prices.get((cid, d_next))
        p_prev = prices.get((cid, d_prev))
        if p_next is None or p_prev is None:
            segs.append(_broken_leaf_seg())
        else:
            segs.append(_leaf_seg(li, p_next - p_prev))
    return days, segs


def _rebuild_drawdown_index_locked(conn: sqlite3.Connection) -> dict:
    """
    全量重建 dd_day_idx / dd_node，并把 built_rev 对齐到当前 rev。
    调用方必须已持有事务（write_tx 或 snapshot 内）。
    """
    days, leaves = _build_leaf_segs(conn)
    n = len(days)
    edge_count = n - 1 if n >= 2 else 0
    size = 1
    while size < max(edge_count, 1):
        size <<= 1

    conn.execute("DELETE FROM dd_node")
    conn.execute("DELETE FROM dd_day_idx")
    conn.executemany(
        "INSERT INTO dd_day_idx(idx, date) VALUES (?, ?)",
        list(enumerate(days)),
    )

    tree = [_Seg() for _ in range(2 * size)]
    for li, seg in enumerate(leaves):
        tree[size + li] = seg
    for node in range(size - 1, 0, -1):
        tree[node] = _merge_seg(tree[2 * node], tree[2 * node + 1])

    rows = []
    for node in range(1, 2 * size):
        s = tree[node]

        def flat(c):
            return (None, None, None) if c is None else (c[0], c[1], c[2])

        ps, pa, pb = flat(s.pref)
        ss, sa, sb = flat(s.suff)
        bs, ba, bb = flat(s.best)
        rows.append((node, s.cnt, s.tot, ps, pa, pb, ss, sa, sb, bs, ba, bb))
    conn.executemany(_DD_UPSERT_SQL, rows)

    rev = conn.execute("SELECT rev FROM dd_state WHERE id = 1").fetchone()["rev"]
    conn.execute(
        "UPDATE dd_state SET built_rev = ?, n_days = ?, leaf_count = ?, size = ?, "
        "built_at = ? WHERE id = 1",
        (rev, n, edge_count, size, datetime.now().isoformat(timespec="seconds")),
    )
    return {"n_days": n, "leaf_count": edge_count, "size": size}


def rebuild_drawdown_index(conn: sqlite3.Connection) -> dict:
    """独立事务版本的全量重建（启动补建/运维入口）。"""
    with write_tx(conn):
        return _rebuild_drawdown_index_locked(conn)


def ensure_drawdown_index(conn: sqlite3.Connection) -> bool:
    """旧库首次补建 / 过期重建。返回是否发生了重建。"""
    row = _dd_state_row(conn)
    if row is None or row["built_rev"] != row["rev"]:
        rebuild_drawdown_index(conn)
        return True
    return False


def _compute_leaf_seg_by_idx(conn: sqlite3.Connection, li: int) -> _Seg:
    """实时重算单条边 li（d_li -> d_li+1），供单笔报价后的增量维护。"""
    row = conn.execute(
        "SELECT d0.date AS d0, d1.date AS d1 "
        "FROM dd_day_idx d0 JOIN dd_day_idx d1 ON d1.idx = d0.idx + 1 "
        "WHERE d0.idx = ?",
        (li,),
    ).fetchone()
    if row is None:
        return _broken_leaf_seg()
    ord_ = active_contract_ord(conn, row["d1"])
    contract = contract_by_ord(conn, ord_)
    if contract is None:
        return _broken_leaf_seg()
    p_prev = get_price(conn, contract["contract_id"], row["d0"])
    p_next = get_price(conn, contract["contract_id"], row["d1"])
    if p_prev is None or p_next is None:
        return _broken_leaf_seg()
    return _leaf_seg(li, p_next - p_prev)


def _refresh_leaf_path_locked(conn: sqlite3.Connection, size: int, li: int) -> int:
    """重算叶 li 并自底向上更新到根：写 O(log n) 节点、读 O(log n) 兄弟节点。"""
    leaf_id = size + li
    seg = _compute_leaf_seg_by_idx(conn, li)
    _dd_save_seg(conn, leaf_id, seg)

    child_id = leaf_id
    child_seg = seg
    while child_id > 1:
        parent_id = child_id >> 1
        if child_id & 1:  # 当前 child 是右孩子，左兄弟 = child_id - 1
            sibling = _dd_load_seg(conn, child_id - 1)
            merged = _merge_seg(sibling, child_seg)
        else:             # 当前 child 是左孩子，右兄弟 = child_id + 1
            sibling = _dd_load_seg(conn, child_id + 1)
            merged = _merge_seg(child_seg, sibling)
        _dd_save_seg(conn, parent_id, merged)
        child_id, child_seg = parent_id, merged
    return leaf_id


def _sync_drawdown_after_price_write(
    conn: sqlite3.Connection,
    state_pre: Optional[sqlite3.Row],
    contract_id: int,
    day: str,
) -> None:
    """
    报价写事务内的回落索引同步：
      - 写前索引与 rev 对齐（受支持的正常路径）→ 至多刷新相邻两条边，O(log n)；
      - 写前已过期（如旧库/库外改动）→ 全量重建兜底；
    最后把 built_rev 对齐到本事务触发后的最新 rev。
    """
    fresh = state_pre is not None and state_pre["built_rev"] == state_pre["rev"]
    if not fresh:
        _rebuild_drawdown_index_locked(conn)
        return

    n_days = state_pre["n_days"]
    size = state_pre["size"]
    idx_row = conn.execute(
        "SELECT idx FROM dd_day_idx WHERE date = ?", (day,)
    ).fetchone()
    if idx_row is not None and n_days >= 2:
        di = idx_row["idx"]
        # 该报价至多被“终于该日”和“起于该日”的两条边使用
        for li in (di - 1, di):
            if 0 <= li < n_days - 1:
                _refresh_leaf_path_locked(conn, size, li)

    conn.execute(
        "UPDATE dd_state SET built_rev = "
        "(SELECT rev FROM dd_state WHERE id = 1) WHERE id = 1"
    )


def delete_price(conn: sqlite3.Connection, contract_id: int, day: str) -> dict:
    """删除一笔报价（演示回放/运维用），与 upsert 一样原子联动价差与回落索引。"""
    try:
        with write_tx(conn):
            state_pre = _dd_state_row(conn)
            affected_roll = conn.execute(
                "SELECT ord FROM rolls WHERE pre_date = ?", (day,)
            ).fetchone()
            conn.execute(
                "DELETE FROM prices WHERE contract_id = ? AND date = ?",
                (contract_id, day),
            )
            new_delta = None
            if affected_roll is not None:
                new_delta = _recompute_roll_delta(conn, affected_roll["ord"])
            _sync_drawdown_after_price_write(conn, state_pre, contract_id, day)
        return {"committed": True, "new_delta": new_delta}
    except sqlite3.Error as e:
        return {"committed": False, "error": str(e)}


def reset_all_data(conn: sqlite3.Connection) -> None:
    """清空全部业务数据（网页演示场景载入前使用）。"""
    with write_tx(conn):
        # 先解除 meta 对 contracts 的外键引用，再按子表→父表顺序删除
        conn.execute("UPDATE meta SET initial_contract_id = NULL WHERE id = 1")
        for table in ("prices", "rolls", "contracts", "calendar",
                      "bit_sums", "dd_node", "dd_day_idx"):
            conn.execute(f"DELETE FROM {table}")
        # 上面 DELETE 源表会经触发器推动 rev；这里把索引状态彻底归零
        conn.execute(
            "UPDATE dd_state SET rev = 0, built_rev = -1, n_days = 0, "
            "leaf_count = 0, size = 1, built_at = NULL WHERE id = 1"
        )


def _dd_range_seg(conn: sqlite3.Connection, ql: int, qr: int,
                  size: int) -> _Seg:
    """
    取叶下标半开区间 [ql, qr) 的合并摘要。
    迭代式线段树标准区间分解，选中节点 ≤ 2 log2(size)，逐节点主键读取。
    """
    left_acc = _Seg()
    right_acc = _Seg()
    l = ql + size
    r = qr + size
    while l < r:
        if l & 1:
            left_acc = _merge_seg(left_acc, _dd_load_seg(conn, l))
            l += 1
        if r & 1:
            r -= 1
            right_acc = _merge_seg(_dd_load_seg(conn, r), right_acc)
        l >>= 1
        r >>= 1
    return _merge_seg(left_acc, right_acc)


def _date_by_idx(conn: sqlite3.Connection, idx: int) -> Optional[str]:
    row = conn.execute("SELECT date FROM dd_day_idx WHERE idx = ?", (idx,)).fetchone()
    return row["date"] if row else None


def _edge_details_locked(conn: sqlite3.Connection, start: str, end: str,
                         best: Optional[dict]) -> list[dict]:
    """
    生成逐日边权核对明细（供网页展示，会按区间逐日取价，属展示开销，
    不参与最大回落的对数级索引核算）。
    """
    days = [r["date"] for r in conn.execute(
        "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end),
    )]
    roll_dates = {r["switch_date"] for r in conn.execute("SELECT switch_date FROM rolls")}
    edges: list[dict] = []
    for d_prev, d_next in zip(days, days[1:]):
        ord_ = active_contract_ord(conn, d_next)
        contract = contract_by_ord(conn, ord_)
        code = contract["code"] if contract else "（无合约）"
        cid = contract["contract_id"] if contract else None
        p_prev = get_price(conn, cid, d_prev) if cid is not None else None
        p_next = get_price(conn, cid, d_next) if cid is not None else None
        valid = p_prev is not None and p_next is not None
        missing = []
        if cid is None:
            missing.append("该日尚未登记在用合约/换月计划")
        else:
            if p_prev is None:
                missing.append(f"{code} 在 {d_prev} 的报价")
            if p_next is None:
                missing.append(f"{code} 在 {d_next} 的报价")
        in_best = False
        if best and best.get("found"):
            in_best = d_prev >= best["start"] and d_next <= best["end"]
        edges.append({
            "prev_date": d_prev,
            "next_date": d_next,
            "contract": code,
            "prev_price": p_prev,
            "next_price": p_next,
            "change": (p_next - p_prev) if valid else None,
            "valid": valid,
            "missing": "、".join(missing),
            "is_roll_edge": d_next in roll_dates,
            "in_best": in_best,
        })
    return edges


def find_max_drawdown(conn: sqlite3.Connection, start: str, end: str,
                      details: bool = False) -> dict:
    """
    在 [start, end] 交易日窗口内，找累计下跌最多的两日 (a, b)。

    返回 dict：
      found=True  → start/end（日期）、amount（正整数分）、sum（负的累计和）
      found=False → reason ∈ {bad_window, no_trade_days, need_two_days, no_decline}
    整个核算在同一个只读快照事务内完成；索引过期时在该事务内幂等补建并随提交发布。
    details=True 时额外在同一快照内生成逐日核对明细 edges。
    """
    for label, v in (("开始日", start), ("结束日", end)):
        if not valid_iso(v):
            raise AppError(f"{label}格式非法：{v}，应为 YYYY-MM-DD")
    if start > end:
        raise AppError("开始日不能晚于结束日")

    result: dict = {
        "found": False, "start": start, "end": end,
        "amount": None, "sum": None, "reason": None, "message": "",
        "window_days": 0, "edges": None,
    }

    with snapshot(conn):
        st = _dd_state_row(conn)
        if st is None or st["built_rev"] != st["rev"]:
            # 旧库首次补建，或检测到库外直接改源表后的重建；同一事务同一快照
            st_info = _rebuild_drawdown_index_locked(conn)
            size = st_info["size"]
        else:
            size = st["size"]

        lo = conn.execute(
            "SELECT idx, date FROM dd_day_idx WHERE date >= ? "
            "ORDER BY date ASC LIMIT 1",
            (start,),
        ).fetchone()
        hi = conn.execute(
            "SELECT idx, date FROM dd_day_idx WHERE date <= ? "
            "ORDER BY date DESC LIMIT 1",
            (end,),
        ).fetchone()

        if lo is None or hi is None or lo["idx"] > hi["idx"]:
            result["reason"] = "no_trade_days"
            result["message"] = f"{start} 至 {end} 之间没有交易日，无法核实回落"
            if details:
                result["edges"] = []
            return result

        i0, i1 = lo["idx"], hi["idx"]
        result["window_days"] = i1 - i0 + 1
        if i1 - i0 < 1:
            result["reason"] = "need_two_days"
            result["message"] = "所选区间只有一个交易日，至少需要相邻两个交易日才能计算回落"
            if details:
                result["edges"] = []
            return result

        # 日下标 [i0, i1] → 叶下标半开区间 [i0, i1)（叶 li 连接日 li 与 li+1）
        seg = _dd_range_seg(conn, i0, i1, size)
        cand = seg.best

        if cand is None or cand[0] >= 0:
            result["reason"] = "no_decline"
            result["message"] = (
                "所选区间内没有可核实的累计下跌：所有未被断点分开的两日累计涨跌均未下跌"
                if cand is not None else
                "所选区间内没有可核实的下跌：相邻交易日之间的必需报价均不完整（全是断点）"
            )
            if details:
                result["edges"] = _edge_details_locked(conn, start, end, result)
            return result

        total, a, b = cand
        result.update({
            "found": True,
            "sum": total,
            "amount": -total,           # 回落金额，正整数分
            "start_idx": a,
            "end_idx": b,
            "start": _date_by_idx(conn, a),
            "end": _date_by_idx(conn, b),
        })
        result["message"] = (
            f"最大回落 {result['amount']} 分："
            f"{result['start']} → {result['end']} 累计涨跌 {total} 分"
        )
        if details:
            result["edges"] = _edge_details_locked(conn, start, end, result)
        return result


# 便于不同调用习惯/潜在外部脚本按名取用
query_drawdown = find_max_drawdown
max_drawdown = find_max_drawdown


# --------------------------------------------------------------------------- #
# 状态总览
# --------------------------------------------------------------------------- #

def overview(conn: sqlite3.Connection) -> dict:
    n_contracts = conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
    n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    n_missing_delta = conn.execute(
        "SELECT COUNT(*) AS n FROM rolls WHERE delta IS NULL"
    ).fetchone()["n"]
    n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
    n_days = conn.execute("SELECT COUNT(*) AS n FROM calendar").fetchone()["n"]
    cal = list_calendar(conn)
    return {
        "n_contracts": n_contracts,
        "n_rolls": n_rolls,
        "n_missing_delta": n_missing_delta,
        "n_prices": n_prices,
        "n_days": n_days,
        "cal_min": cal[0] if cal else None,
        "cal_max": cal[-1] if cal else None,
    }
