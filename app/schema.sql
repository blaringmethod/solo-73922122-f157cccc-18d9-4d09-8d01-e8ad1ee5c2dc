-- 连续合约结算价库结构
-- 设计要点：
--   contracts.ord = 0 为初始合约；第 k 次换月启用的合约 ord = k（k >= 1）
--   rolls 记录每次换月；delta 为换月价差（新合约价 - 旧合约价，取切换前一交易日）
--   bit_sums 是树状数组（Fenwick tree）辅助表，前缀和/单点更新均为 O(log n)
--   连续价不落地存储，取数时实时计算，改价不逐日回写

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS contracts (
    contract_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    ord  INTEGER NOT NULL UNIQUE      -- 0=初始合约，k=第 k 次换月启用的合约
);

CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_contract_id INTEGER REFERENCES contracts(contract_id)
);
INSERT OR IGNORE INTO meta (id) VALUES (1);

CREATE TABLE IF NOT EXISTS rolls (
    ord              INTEGER PRIMARY KEY,          -- 第几次换月，1..n 稠密
    switch_date      TEXT NOT NULL UNIQUE,         -- 切换日（当日起启用新合约）
    pre_date         TEXT NOT NULL,                 -- 切换前一交易日
    new_contract_id  INTEGER NOT NULL REFERENCES contracts(contract_id),
    delta            INTEGER                        -- 新合约价 - 旧合约价，缺报价时为 NULL
);

-- Fenwick 树：pos 为树状数组下标，val 为区间累加值；仅覆盖非空 delta
CREATE TABLE IF NOT EXISTS bit_sums (
    pos INTEGER PRIMARY KEY,
    val INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- 区间回落索引：边上的线段树（segment tree）
--
-- 交易日按日历排序后得到全局下标 0..n_days-1；叶节点 li（0 <= li < n_days-1）
-- 对应相邻两日 li -> li+1，边权 w = 后一日在用合约在“后一日、前一日”两笔
-- 结算价之差（后日 - 前日）。任一所需报价缺失，该叶为断点（cnt=1, tot=NULL,
-- 候选全 NULL），区间累计不得跨过它。
-- 线段树节点保存：整段和 tot（含断点则 NULL）、最小前缀/后缀/子段和候选
-- （候选带起止日下标，供同额时按“早起日、再早止日”裁决）。
-- 一次区间最大回落只按键读 O(log n) 个 dd_node；一笔报价只改写 O(log n) 个节点。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dd_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    rev         INTEGER NOT NULL DEFAULT 0,   -- 源表任何写操作经触发器 +1
    built_rev   INTEGER NOT NULL DEFAULT -1,  -- 索引构建时对应的 rev；不等即需重建
    n_days      INTEGER NOT NULL DEFAULT 0,
    leaf_count  INTEGER NOT NULL DEFAULT 0,
    size        INTEGER NOT NULL DEFAULT 1,   -- 线段树叶槽数（>= leaf_count 的 2 的幂）
    built_at    TEXT
);
INSERT OR IGNORE INTO dd_state(id, rev, built_rev) VALUES (1, 0, -1);

-- 日期 -> 全局交易日下标，区间端点定位只需主键查询，不必扫描日历
CREATE TABLE IF NOT EXISTS dd_day_idx (
    idx  INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE
);

-- 堆式编号线段树节点：叶节点编号 = size + li
CREATE TABLE IF NOT EXISTS dd_node (
    node     INTEGER PRIMARY KEY,
    cnt      INTEGER NOT NULL,          -- 真实边数；0 = 补齐用空段（单位元）
    tot      INTEGER,                   -- 整段边权和；cnt>0 且含断点时为 NULL
    pref_sum INTEGER, pref_a INTEGER, pref_b INTEGER,
    suff_sum INTEGER, suff_a INTEGER, suff_b INTEGER,
    best_sum INTEGER, best_a INTEGER, best_b INTEGER
);
-- 版本触发器统一放在 prices/calendar/rolls/contracts 建表之后（见文件末尾）。

CREATE TABLE IF NOT EXISTS calendar (
    date TEXT PRIMARY KEY      -- 仅登记交易日，ISO 格式 YYYY-MM-DD
);

CREATE TABLE IF NOT EXISTS prices (
    contract_id INTEGER NOT NULL REFERENCES contracts(contract_id),
    date        TEXT NOT NULL,
    price       INTEGER NOT NULL,                    -- 整数分结算价
    PRIMARY KEY (contract_id, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_rolls_switch  ON rolls(switch_date);
CREATE INDEX IF NOT EXISTS idx_rolls_predate ON rolls(pre_date);
-- 部分索引：快速定位区间内最早缺价差的换月
CREATE INDEX IF NOT EXISTS idx_rolls_delta_null ON rolls(ord) WHERE delta IS NULL;
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

-- 源表版本触发器：任何绕过 upsert_price 的直接增删改也会让索引标记为过期，
-- 下一次回落查询据此全量重建（旧库首次补建同路径）。
CREATE TRIGGER IF NOT EXISTS trg_dd_rev_prices_ins AFTER INSERT ON prices
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS trg_dd_rev_prices_upd AFTER UPDATE ON prices
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS trg_dd_rev_prices_del AFTER DELETE ON prices
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;

CREATE TRIGGER IF NOT EXISTS trg_dd_rev_cal_ins AFTER INSERT ON calendar
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS trg_dd_rev_cal_del AFTER DELETE ON calendar
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;

CREATE TRIGGER IF NOT EXISTS trg_dd_rev_rolls_ins AFTER INSERT ON rolls
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS trg_dd_rev_rolls_upd AFTER UPDATE ON rolls
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS trg_dd_rev_rolls_del AFTER DELETE ON rolls
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;

CREATE TRIGGER IF NOT EXISTS trg_dd_rev_contracts_ins AFTER INSERT ON contracts
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;
CREATE TRIGGER IF NOT EXISTS trg_dd_rev_contracts_del AFTER DELETE ON contracts
BEGIN UPDATE dd_state SET rev = rev + 1 WHERE id = 1; END;
