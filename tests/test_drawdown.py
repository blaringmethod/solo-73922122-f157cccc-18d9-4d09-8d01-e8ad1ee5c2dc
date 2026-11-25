"""
区间最大回落（segment-tree index）测试：
  口径（后一日在用合约两笔价）、换月旧约缺价、断点不跨越、最大回落与同额裁决、
  与暴力枚举随机对照、O(log n) 节点读写、原子提交/回滚、库外改动自愈重建、
  旧库首次补建、Web 页面与 JSON 接口、演示场景补价前后。
运行：PYTHONPATH=. python -m unittest tests.test_drawdown -v
"""
import os
import random
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from app import db, demo


# --------------------------------------------------------------------------- #
# 暴力参考实现
# --------------------------------------------------------------------------- #

def brute_force(conn, start, end):
    days = [r["date"] for r in conn.execute(
        "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end))]
    if len(days) < 2:
        return None
    # 构造边
    edges = []
    for a, b in zip(days, days[1:]):
        ord_ = db.active_contract_ord(conn, b)
        c = db.contract_by_ord(conn, ord_)
        if c is None:
            edges.append(None)
            continue
        pa = db.get_price(conn, c["contract_id"], a)
        pb = db.get_price(conn, c["contract_id"], b)
        if pa is None or pb is None:
            edges.append(None)
        else:
            edges.append(pb - pa)

    best = None  # (sum, ia, ib)
    n = len(days)
    for ia in range(0, n - 1):
        s = 0
        for ib in range(ia + 1, n):
            w = edges[ib - 1]
            if w is None:
                break  # 跨过不断点
            s += w
            cand = (s, ia, ib)
            if best is None or (cand[0], cand[1], cand[2]) < \
                    (best[0], best[1], best[2]):
                best = cand
    if best is None or best[0] >= 0:
        return None
    s, ia, ib = best
    return {"amount": -s, "start": days[ia], "end": days[ib]}


class DrawdownBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "d.db")
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def assertMatchesBrute(self, start, end):
        got = db.find_max_drawdown(self.conn, start, end)
        exp = brute_force(self.conn, start, end)
        if exp is None:
            self.assertFalse(
                got["found"],
                f"expected no decline but got {got} for {start}..{end}")
        else:
            self.assertTrue(got["found"], f"missing {exp}, got {got}")
            self.assertEqual(got["amount"], exp["amount"])
            self.assertEqual(got["start"], exp["start"])
            self.assertEqual(got["end"], exp["end"])


class TestDemoScenario(DrawdownBase):
    def test_before_and_after_fill(self):
        before = demo.seed_before(self.conn)
        self.assertTrue(before["found"])
        self.assertEqual((before["amount"], before["start"], before["end"]),
                         (200, "2026-03-04", "2026-03-05"))
        # 换月边可核算，且旧合约 DEMO_A 在参考日缺价
        details = db.find_max_drawdown(
            self.conn, demo.DEMO_START, demo.DEMO_END, details=True)["edges"]
        roll_edge = next(e for e in details if e["is_roll_edge"])
        self.assertTrue(roll_edge["valid"])
        self.assertEqual(roll_edge["change"], -200)
        self.assertEqual(roll_edge["contract"], "DEMO_B")

        b, after = demo.fill_key_quote(self.conn)
        self.assertEqual(b["amount"], 200)
        self.assertEqual((after["amount"], after["start"], after["end"]),
                         (690, "2026-03-04", "2026-03-11"))

        # 撤销补价后恢复 200（增量删除路径）
        again = demo.unfill_key_quote(self.conn)
        self.assertEqual(again["amount"], 200)

    def test_reseed_populated_database(self):
        # 回归：对已含 meta 外键引用的库执行演示重置不能触发 FK 失败
        demo.seed_before(self.conn)
        demo.fill_key_quote(self.conn)
        again = demo.seed_before(self.conn)
        self.assertEqual(again["amount"], 200)
        self.assertEqual(db.overview(self.conn)["n_prices"],
                         len(demo.PRICES_BEFORE))

    def test_no_decline_and_single_day_messages(self):
        demo.seed_before(self.conn)
        up = db.find_max_drawdown(self.conn, "2026-03-05", "2026-03-06")
        self.assertFalse(up["found"])
        self.assertEqual(up["reason"], "no_decline")
        one = db.find_max_drawdown(self.conn, "2026-03-05", "2026-03-05")
        self.assertEqual(one["reason"], "need_two_days")
        none = db.find_max_drawdown(self.conn, "2020-01-01", "2020-02-01")
        self.assertEqual(none["reason"], "no_trade_days")

    def test_bad_input(self):
        with self.assertRaises(db.AppError):
            db.find_max_drawdown(self.conn, "2026/03/01", "2026-03-11")
        with self.assertRaises(db.AppError):
            db.find_max_drawdown(self.conn, "2026-03-11", "2026-03-02")


class TestTieBreak(DrawdownBase):
    def _setup_edges(self, weights):
        """
        直接构造一个单合约、无边缺的场景：给定目标边权序列反推价格。
        n 个交易日 = len(weights)+1。
        """
        n = len(weights) + 1
        days = [(date(2026, 1, 5) + timedelta(days=i)).isoformat()
                for i in range(n)]
        db.replace_calendar(self.conn, days)
        db.setup_plan(self.conn, "K0", [])
        cid = db.list_contracts(self.conn)[0]["contract_id"]
        prices = [1000]
        for w in weights:
            prices.append(prices[-1] + w)
        for d, p in zip(days, prices):
            db.upsert_price(self.conn, cid, d, p)
        return days

    def test_equal_amount_earlier_start_then_earlier_end(self):
        # 两腿各 -100，中间 +100 完全收复：d0->d1、d0->d3、d2->d3 同额 -100。
        # 先取较早起日 d0；同起日再取较早止日 d1。
        days = self._setup_edges([-100, 100, -100])
        got = db.find_max_drawdown(self.conn, days[0], days[-1])
        self.assertEqual(got["amount"], 100)
        self.assertEqual(got["start"], days[0])
        self.assertEqual(got["end"], days[1])

    def test_equal_amount_same_start_pick_earlier_end(self):
        # 构造两个同起日、同最大跌幅但止日不同的区间：
        # 边权 [-50, -50, +100]：d0->d1=-50；d0->d2=-100（唯一最深）。
        # 改为 [-100, +0?] 难以同额不同止；用 [-100, +40, -40]：
        # d0->d1=-100，d0->d3=-100 同起日，取较早止日 d1。
        days = self._setup_edges([-100, 40, -40])
        got = db.find_max_drawdown(self.conn, days[0], days[-1])
        self.assertEqual(got["amount"], 100)
        self.assertEqual(got["start"], days[0])
        self.assertEqual(got["end"], days[1])

    def test_later_start_can_win_on_tie_only_if_start_earlier_rule(self):
        # 同额但较晚起日的区间不得赢过早起日
        days = self._setup_edges([20, -80, -20, 30, -100])
        got = db.find_max_drawdown(self.conn, days[0], days[-1])
        # 暴力会给出裁决，直接对照
        exp = brute_force(self.conn, days[0], days[-1])
        self.assertEqual((got["amount"], got["start"], got["end"]),
                         (exp["amount"], exp["start"], exp["end"]))


class TestRandomBruteForce(DrawdownBase):
    def test_random_configs_match_brute_force(self):
        rng = random.Random(20260311)
        for case in range(12):
            tmp = tempfile.TemporaryDirectory()
            try:
                conn = db.connect(os.path.join(tmp.name, f"r{case}.db"))
                db.init_db(conn)
                n_days = rng.randint(6, 45)
                days = [(date(2026, 1, 5) + timedelta(days=i)).isoformat()
                        for i in range(n_days)]
                db.replace_calendar(conn, days)

                n_rolls = rng.randint(0, min(4, n_days - 2))
                sw = sorted(rng.sample(range(2, n_days), n_rolls))
                switches = [(days[k], f"K{j+1}") for j, k in enumerate(sw)]
                db.setup_plan(conn, "K0", switches)
                cids = {r["code"]: r["contract_id"]
                        for r in db.list_contracts(conn)}

                # 随机报价：每个 (合约, 日) 以一定概率缺价
                for code, cid in cids.items():
                    for d in days:
                        if rng.random() < 0.22:
                            continue
                        db.upsert_price(conn, cid, d, rng.randint(30000, 42000))

                # 全量重建结果也必须与暴力一致
                db.rebuild_drawdown_index(conn)

                for _ in range(14):
                    i = rng.randrange(0, n_days - 1)
                    j = rng.randrange(i + 1, n_days)
                    got = db.find_max_drawdown(conn, days[i], days[j])
                    exp = brute_force(conn, days[i], days[j])
                    if exp is None:
                        self.assertFalse(got["found"], (case, i, j, got))
                    else:
                        self.assertEqual(got["amount"], exp["amount"],
                                         (case, i, j))
                        self.assertEqual(got["start"], exp["start"])
                        self.assertEqual(got["end"], exp["end"])

                # 随机改/补几笔后，增量索引仍与暴力一致
                for _ in range(6):
                    code = rng.choice(list(cids))
                    d = rng.choice(days)
                    pv = db.upsert_price(
                        conn, cids[code], d, rng.randint(30000, 42000))
                    self.assertTrue(pv["committed"])
                for _ in range(10):
                    i = rng.randrange(0, n_days - 1)
                    j = rng.randrange(i + 1, n_days)
                    got = db.find_max_drawdown(conn, days[i], days[j])
                    exp = brute_force(conn, days[i], days[j])
                    if exp is None:
                        self.assertFalse(got["found"], (case, i, j, got))
                    else:
                        self.assertEqual(
                            (got["amount"], got["start"], got["end"]),
                            (exp["amount"], exp["start"], exp["end"]),
                            (case, i, j))
                conn.close()
            finally:
                tmp.cleanup()


class TestLogComplexity(DrawdownBase):
    def _build_n_days(self, n, with_price=True):
        days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat()
                for i in range(n)]
        db.replace_calendar(self.conn, days)
        db.setup_plan(self.conn, "K0", [])
        cid = db.list_contracts(self.conn)[0]["contract_id"]
        if with_price:
            # 批量写入（复杂度用例不关心逐笔事务），随后由 rev 自愈重建索引
            self.conn.executemany(
                "INSERT INTO prices(contract_id,date,price) VALUES (?,?,?)",
                [(cid, d, 40000 + ((i * 37) % 500)) for i, d in enumerate(days)])
        return days, cid

    def _build_n_days_on(self, conn, n):
        days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat()
                for i in range(n)]
        db.replace_calendar(conn, days)
        db.setup_plan(conn, "K0", [])
        cid = db.list_contracts(conn)[0]["contract_id"]
        conn.executemany(
            "INSERT INTO prices(contract_id,date,price) VALUES (?,?,?)",
            [(cid, d, 40000 + ((i * 37) % 500)) for i, d in enumerate(days)])
        return days, cid

    def test_range_query_reads_log_nodes(self):
        # 分别在两个规模上验证节点读取上界（取非对齐窗口以触发区间分解）
        for n in (129, 2049):
            with self.subTest(n=n):
                tmp = tempfile.TemporaryDirectory()
                try:
                    conn = db.connect(os.path.join(tmp.name, "c.db"))
                    db.init_db(conn)
                    days, _ = self._build_n_days_on(conn, n)
                    s = days[n // 7]
                    e = days[n - 3]
                    db.find_max_drawdown(conn, s, e)  # 首次：自愈全量重建
                    size = conn.execute(
                        "SELECT size FROM dd_state WHERE id=1").fetchone()["size"]
                    depth = size.bit_length() - 1

                    calls = 0
                    orig = db._dd_load_seg

                    def counting(c, node):
                        nonlocal calls
                        calls += 1
                        return orig(c, node)

                    db._dd_load_seg = counting
                    try:
                        got = db.find_max_drawdown(conn, s, e)
                    finally:
                        db._dd_load_seg = orig
                    # 标准迭代区间分解至多取 2*depth 个节点，留少量余量
                    self.assertLessEqual(calls, 2 * depth + 2,
                                         f"n={n} calls={calls} depth={depth}")
                    self.assertIn(got["reason"], (None, "no_decline"))
                    conn.close()
                finally:
                    tmp.cleanup()

    def test_single_upsert_writes_log_nodes(self):
        n = 2049
        days, cid = self._build_n_days(n, with_price=False)
        db.ensure_drawdown_index(self.conn)
        size = self.conn.execute(
            "SELECT size FROM dd_state WHERE id=1").fetchone()["size"]
        depth = size.bit_length() - 1

        writes = 0
        orig_save = db._dd_save_seg

        def counting_save(c, node, seg):
            nonlocal writes
            writes += 1
            return orig_save(c, node, seg)

        db._dd_save_seg = counting_save
        try:
            # 中间日报价：可能影响相邻两条边 → 至多两条根路径
            pv = db.upsert_price(self.conn, cid, days[n // 2], 40123)
            self.assertTrue(pv["committed"])
        finally:
            db._dd_save_seg = orig_save
        self.assertLessEqual(writes, 2 * (depth + 1),
                             f"writes={writes} depth={depth}")
        self.assertGreater(writes, 0)

    def test_record_touch_grows_sublinearly(self):
        # 规模 x16，非对齐窗口的节点读取次数增长远小于线性（仅多约 4 层）
        counts = {}
        for n in (129, 2049):
            tmp = tempfile.TemporaryDirectory()
            try:
                conn = db.connect(os.path.join(tmp.name, "g.db"))
                db.init_db(conn)
                days, _ = self._build_n_days_on(conn, n)
                s, e = days[n // 7], days[n - 3]
                db.find_max_drawdown(conn, s, e)  # 触发重建
                calls = 0
                orig = db._dd_load_seg

                def counting(c, node, _o=orig):
                    nonlocal calls
                    calls += 1
                    return _o(c, node)

                db._dd_load_seg = counting
                try:
                    db.find_max_drawdown(conn, s, e)
                finally:
                    db._dd_load_seg = orig
                counts[n] = calls
                conn.close()
            finally:
                tmp.cleanup()
        self.assertLess(counts[2049], counts[129] + 12)


class TestAtomicAndSelfHeal(DrawdownBase):
    def test_index_rolls_back_when_index_write_fails(self):
        demo.seed_before(self.conn)
        before = db.find_max_drawdown(self.conn, demo.DEMO_START, demo.DEMO_END)
        root_before = self.conn.execute(
            "SELECT * FROM dd_node WHERE node=1").fetchone()
        rev_before = self.conn.execute(
            "SELECT rev, built_rev FROM dd_state WHERE id=1").fetchone()

        cid = demo._fill_contract_id(self.conn)
        orig = db._dd_save_seg

        def boom(c, node, seg):
            raise sqlite3.OperationalError("simulated index disk failure")

        db._dd_save_seg = boom
        try:
            pv = db.upsert_price(
                self.conn, cid, demo.FILL_DATE, demo.FILL_PRICE)
        finally:
            db._dd_save_seg = orig

        self.assertFalse(pv["committed"])
        # 报价、索引、rev 全部回滚
        self.assertIsNone(db.get_price(self.conn, cid, demo.FILL_DATE))
        root_after = self.conn.execute(
            "SELECT * FROM dd_node WHERE node=1").fetchone()
        self.assertEqual(tuple(root_before), tuple(root_after))
        rev_after = self.conn.execute(
            "SELECT rev, built_rev FROM dd_state WHERE id=1").fetchone()
        self.assertEqual(tuple(rev_before), tuple(rev_after))
        # 照旧索引供数：仍是补价前的 200
        still = db.find_max_drawdown(
            self.conn, demo.DEMO_START, demo.DEMO_END)
        self.assertEqual(still["amount"], before["amount"])

    def test_out_of_band_delete_triggers_rebuild_and_correct(self):
        demo.seed_before(self.conn)
        # 库外直接补入关键报价（绕过 upsert）：rev 触发器仍会自增
        cid = demo._fill_contract_id(self.conn)
        self.conn.execute(
            "INSERT INTO prices(contract_id,date,price) VALUES (?,?,?)",
            (cid, demo.FILL_DATE, demo.FILL_PRICE))
        self.conn.commit()
        got = db.find_max_drawdown(
            self.conn, demo.DEMO_START, demo.DEMO_END)
        self.assertEqual(got["amount"], 690)
        # 再库外删除 → 再次自愈
        self.conn.execute(
            "DELETE FROM prices WHERE contract_id=? AND date=?",
            (cid, demo.FILL_DATE))
        self.conn.commit()
        got = db.find_max_drawdown(
            self.conn, demo.DEMO_START, demo.DEMO_END)
        self.assertEqual(got["amount"], 200)

    def test_stale_built_rev_rebuilds(self):
        demo.seed_before(self.conn)
        self.conn.execute("UPDATE dd_state SET built_rev = -1 WHERE id=1")
        self.conn.commit()
        got = db.find_max_drawdown(
            self.conn, demo.DEMO_START, demo.DEMO_END)
        self.assertEqual(got["amount"], 200)
        row = self.conn.execute(
            "SELECT rev, built_rev FROM dd_state WHERE id=1").fetchone()
        self.assertEqual(row["rev"], row["built_rev"])

    def test_old_database_first_backfill(self):
        # 模拟功能上线前的旧库：只有旧五张表，没有任何 dd_* 对象
        old = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        old.close()
        try:
            c = sqlite3.connect(old.name)
            c.executescript(
                """
                CREATE TABLE contracts (
                    contract_id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
                    ord INTEGER NOT NULL UNIQUE);
                CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1),
                    initial_contract_id INTEGER);
                INSERT INTO meta(id) VALUES(1);
                CREATE TABLE rolls (
                    ord INTEGER PRIMARY KEY, switch_date TEXT NOT NULL UNIQUE,
                    pre_date TEXT NOT NULL, new_contract_id INTEGER NOT NULL,
                    delta INTEGER);
                CREATE TABLE bit_sums (pos INTEGER PRIMARY KEY, val INTEGER NOT NULL);
                CREATE TABLE calendar (date TEXT PRIMARY KEY);
                CREATE TABLE prices (
                    contract_id INTEGER NOT NULL, date TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    PRIMARY KEY(contract_id,date)) WITHOUT ROWID;
                """
            )
            days = demo.CALENDAR
            c.executemany("INSERT INTO calendar(date) VALUES(?)",
                           [(d,) for d in days])
            c.execute("INSERT INTO contracts(code,ord) VALUES('A',0)")
            c.execute("INSERT INTO contracts(code,ord) VALUES('B',1)")
            c.execute(
                "INSERT INTO rolls(ord,switch_date,pre_date,new_contract_id,delta) "
                "VALUES(1,?,?,2,NULL)", (demo.ROLL_DATE, "2026-03-04"))
            c.executemany(
                "INSERT INTO prices(contract_id,date,price) VALUES(1,?,?)",
                [(d, p) for code, d, p in demo.PRICES_BEFORE if code == "DEMO_A"])
            c.executemany(
                "INSERT INTO prices(contract_id,date,price) VALUES(2,?,?)",
                [(d, p) for code, d, p in demo.PRICES_BEFORE if code == "DEMO_B"])
            c.commit()
            c.close()

            conn = db.connect(old.name)
            db.init_db(conn)              # 幂等补建新表/触发器
            changed = db.ensure_drawdown_index(conn)
            self.assertTrue(changed)      # 首次补建
            got = db.find_max_drawdown(conn, demo.DEMO_START, demo.DEMO_END)
            self.assertEqual(got["amount"], 200)
            conn.close()
        finally:
            os.unlink(old.name)


class TestWebDrawdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "web.db")
        os.environ["CONT_DB_PATH"] = self.db_path
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        from app.app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_query_page_has_drawdown_panel(self):
        html = self.client.get("/query").get_data(as_text=True)
        self.assertEqual(self.client.get("/query").status_code, 200)
        self.assertIn("区间最大回落", html)
        self.assertIn("/demo/seed", html)

    def test_demo_seed_fill_and_api(self):
        r = self.client.post("/demo/seed", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        loc = r.headers["Location"]
        self.assertIn("dd_start=2026-03-02", loc)

        html = self.client.get("/query?dd_start=2026-03-02&dd_end=2026-03-11"
                               "&demo=before").get_data(as_text=True)
        self.assertIn(">200<", html)
        self.assertIn("2026-03-04", html)
        self.assertIn("2026-03-05", html)
        self.assertIn("换月边", html)
        # 旧合约缺价造成的断点说明
        self.assertIn("断点", html)

        r = self.client.post("/demo/fill", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        html = self.client.get("/query?dd_start=2026-03-02&dd_end=2026-03-11"
                               "&demo=after").get_data(as_text=True)
        self.assertIn(">690<", html)
        self.assertIn("2026-03-11", html)

        js = self.client.get("/api/drawdown?start=2026-03-02&end=2026-03-11")
        self.assertEqual(js.status_code, 200)
        payload = js.get_json()
        self.assertTrue(payload["found"])
        self.assertEqual(payload["amount"], 690)

        # 复用连续价表单的 start/end（无 dd_* 参数）也应给出回落结果
        html = self.client.get(
            "/query?start=2026-03-02&end=2026-03-11&base=2026-03-11"
        ).get_data(as_text=True)
        self.assertIn('dd-num">690<', html)

        bad = self.client.get("/api/drawdown?start=nope&end=2026-03-11")
        self.assertEqual(bad.status_code, 400)

    def test_drawdown_alias_redirects(self):
        r = self.client.get("/drawdown?start=2026-03-02&end=2026-03-11")
        self.assertEqual(r.status_code, 302)
        self.assertIn("#drawdown", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
