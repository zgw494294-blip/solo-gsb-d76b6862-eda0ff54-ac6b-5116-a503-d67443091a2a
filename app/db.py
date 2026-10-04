"""SQLite 持久化层。

- 数据库文件路径：环境变量 PLANNER_DB > /data/planner.db（容器 Compose 卷）
  > 仓库内 data/planner.db（本地裸跑兜底）。
- 读操作只经过进程内全局读锁（_LOCK），拿到的一定是同一版本下自洽的
  参数与结果。
- 写操作一律按「写互斥锁（_WRITE_LOCK）→ 全局读锁」的顺序取锁，保证
  「配方修改」「归档」「停用」「新建炉次」按某一先后顺序整体生效。
  配方重算与新建炉次的耗时求解都在两把锁之外进行，期间读操作仍可
  进入并读到上一次完整提交的状态（新草稿在整体提交前不可见），只有
  最后的复核+提交瞬间需要短暂持锁；连续竞争导致快照复核反复失效时，
  兜底路径持写互斥锁走完 快照→求解→提交 全程——其他写操作在写互斥
  锁上排队（读操作不经过它），求解期间读取照常返回上一整次提交的
  状态。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_LOCK = threading.RLock()
# 仅写操作持有的互斥锁，读操作不经过它。兜底重算持它走完 快照→求解→提交
# 全程：其他写操作在它上面排队（保持某一先后顺序），读取则完全不受影响。
_WRITE_LOCK = threading.RLock()


def db_path() -> Path:
    env = os.environ.get("PLANNER_DB")
    if env:
        return Path(env)
    preferred = Path("/data/planner.db")
    if os.access("/", os.W_OK) or preferred.parent.exists():
        try:
            preferred.parent.mkdir(parents=True, exist_ok=True)
            return preferred
        except OSError:
            pass
    local = Path(__file__).resolve().parent.parent / "data" / "planner.db"
    local.parent.mkdir(parents=True, exist_ok=True)
    return local


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS recipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    hold_low    INTEGER NOT NULL,
    hold_high   INTEGER NOT NULL,
    duration    INTEGER NOT NULL,
    tolerance   INTEGER NOT NULL,
    -- 'active' 可用（默认）/ 'disabled' 停用；停用后不可被新炉次引用，
    -- 已有引用草稿与已归档方案不受影响
    status      TEXT    NOT NULL DEFAULT 'active',
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    -- 按顺序引用 2~3 个不同的现有配方 ID，创建后不可改
    recipe_ids  TEXT    NOT NULL,
    -- 上次求解时各配方的版本（{recipeId: recipeVersion}）
    recipe_versions_json TEXT NOT NULL DEFAULT '{}',
    -- 'active' 活动（默认）/ 'cancelled' 已作废：作废保留记录而非删除，
    -- 不再参与配方修改/恢复的重算，也不可再归档；已归档由 archives 表决定
    status      TEXT    NOT NULL DEFAULT 'active',
    version     INTEGER NOT NULL DEFAULT 1,
    -- 本次求解所用材料参数快照（与 result_json 严格同版）
    materials_json TEXT NOT NULL,
    solvable    INTEGER NOT NULL,
    result_json TEXT,        -- solvable=0 时为 NULL（无旧曲线）
    reason      TEXT,        -- solvable=0 时的中文原因
    -- 作废时间（UTC ISO 字符串）；未作废为 NULL
    cancelled_at TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS archives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id        INTEGER NOT NULL UNIQUE,
    draft_version   INTEGER NOT NULL,
    materials_json  TEXT    NOT NULL,  -- 冻结当时的材料参数
    result_json     TEXT    NOT NULL,  -- 冻结完整求解结果
    archived_at     TEXT    NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);

-- 配方参数历史：仅追加。每次成功的整份修改（含从历史恢复）写入一个新版本行；
-- 创建配方写入版本 1；停用 / 恢复可用状态不写历史。旧库就地补记每个配方的
-- 当前版本（仅一次，重复启动不重复补记）。随现有 SQLite 卷跨重启保留。
CREATE TABLE IF NOT EXISTS recipe_versions (
    recipe_id   INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    hold_low    INTEGER NOT NULL,
    hold_high   INTEGER NOT NULL,
    duration    INTEGER NOT NULL,
    tolerance   INTEGER NOT NULL,
    PRIMARY KEY (recipe_id, version),
    FOREIGN KEY (recipe_id) REFERENCES recipes(id)
);

-- 单台炉的开炉计划（仅安排已归档炉次，与配方 / 草稿 / 归档互不影响）：
-- meta 为单行表，保存计划版本——空计划版本视为 1，每批成功提交 +1；
-- entries 每炉次至多一段（draft_id 唯一），开始时间为非负整数 Unix 分钟，
-- 时长不入库（始终取归档冻结曲线档数 × 5 分钟）。随现有 SQLite 卷持久化，
-- 旧库启动时由 CREATE TABLE IF NOT EXISTS 自动初始化为空计划（版本 1）。
CREATE TABLE IF NOT EXISTS furnace_plan_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS furnace_plan_entries (
    draft_id      INTEGER PRIMARY KEY,
    start_minute  INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);

-- 开炉 / 结束登记（仅经 HTTP 登记，不控制设备）：每个炉次至多一条执行记录
-- （draft_id 唯一，落实「每炉次仅执行一次」）；status='running' 进行中
-- （全表至多一条，由写互斥锁 + 单条事务保证「同时仅一项进行中」），
-- 'finished' 已结束（计划项已移除且不得重新排入）。开炉时冻结当时的计划
-- 起止（plan_end = plan_start + 归档冻结曲线档数 × 5）；实际起止均为
-- SQLite 可存的非负整数 Unix 分钟，未结束时 actual_end_minute 为 NULL。
-- 随现有 SQLite 卷跨重启保留；旧库启动时由 CREATE TABLE IF NOT EXISTS
-- 自动初始化为空表（无正在占炉的炉次）。
CREATE TABLE IF NOT EXISTS furnace_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id            INTEGER NOT NULL UNIQUE,
    plan_start_minute   INTEGER NOT NULL,
    plan_end_minute     INTEGER NOT NULL,
    actual_start_minute INTEGER NOT NULL,
    actual_end_minute   INTEGER,
    status              TEXT NOT NULL CHECK (status IN ('running','finished')),
    -- 开炉时所读（也即所基于）的计划版本
    plan_version        INTEGER NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """旧库就地升级。

    1. recipes 缺少 status 列时补上，既有配方默认可用；
    2. recipe_versions 历史表对旧库只补记每个配方的**当前 version**（历史
       参数无法追溯）；INSERT OR IGNORE + 「已有历史即不补记」双重保证
       重复启动不会重复补记，此后新创建 / 修改 / 恢复照常只追加；
    3. drafts 缺少 status / cancelled_at 列时补上：旧库中的未归档炉次
       一律为 active（cancelled_at 为 NULL），重启后保留作废状态。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()}
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE recipes ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )

    existing = conn.execute("SELECT COUNT(*) FROM recipe_versions").fetchone()[0]
    if existing == 0:
        conn.execute(
            """INSERT OR IGNORE INTO recipe_versions
               (recipe_id, version, name, hold_low, hold_high, duration, tolerance)
               SELECT id, version, name, hold_low, hold_high, duration, tolerance
               FROM recipes"""
        )

    draft_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(drafts)").fetchall()
    }
    if "status" not in draft_cols:
        conn.execute(
            "ALTER TABLE drafts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    if "cancelled_at" not in draft_cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN cancelled_at TEXT")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def lock():
    """全局读锁。所有涉及配方/草稿/归档的操作都在该锁内完成。"""
    return _LOCK


def write_lock():
    """写操作互斥锁。所有写路径（含配方重算的提交与兜底）都先取它、再取
    全局读锁；读操作不经过它，因此兜底路径求解期间读取照常返回已提交状态。"""
    return _WRITE_LOCK
