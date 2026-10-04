"""配方 / 炉次草稿 / 归档 的持久化操作。

读操作只持进程内全局读锁（见 db.lock）；写操作一律先持「写互斥锁」
（见 db.write_lock）再持全局读锁，并在单条 SQLite 事务中整体提交，
保证「归档」「停用」「新建炉次」与「修改配方」按某一先后顺序生效；
读操作拿到的一定是同一版本下自洽的参数与结果。

「修改配方并重算草稿」与「新建炉次」是分两阶段的操作：锁内只取快照，
耗时的求解放在锁外（期间 GET 仍能读到上一整次提交的完整状态，新草稿
在整体提交前不可见），随后再上锁、在单条事务里复核并发漂移并整体
提交；见 update_recipe 与 create_draft。连续竞争导致复核反复失效时，
兜底路径持写互斥锁走完 快照→求解→提交 全程：求解仍不持读锁（GET
不被阻塞），其他写操作在写互斥锁上排队，保证本次写操作必然整体提交
成功。「从历史恢复」（restore_recipe）复用同一两阶段流程但只尝试
一次：复核不过（他次普通修改先提交等）直接 409，绝不重试覆盖新修改。

参数历史（recipe_versions 表）只追加：创建配方记版本 1，每次成功的
整份修改（含恢复）在同一事务里追加新版本行；停用 / 恢复可用状态不记
历史。旧库启动时仅对每个配方补记当前版本，重复启动不重复补记。

炉次作废（cancel_draft）保留记录而非删除：标记 status='cancelled'、
版本 +1 并记录作废时间，原引用、参数快照、结果或无解原因全部保留；
作废炉次不再参与配方修改 / 历史恢复的重算（重算的活动草稿集合始终
排除已作废），也不再允许归档。作废与重算、归档并发时按某一先后顺序
生效：作废先提交则重算复核发现集合漂移、整批作废重算（已算出的结果
不会回写到已作废炉次）；重算先提交则旧版本作废收到 409；归档先提交
则作废收到 409。

单台炉的「开炉 / 结束登记」（furnace_runs 表，仅登记、不控制设备）在
同一把写互斥锁与单条事务里与计划修改串行，按请求先后生效：

- 开炉（start_furnace_run）只能启动当前计划首项：校验所读计划版本、
  「无进行中项」「该炉次从未执行」「为计划首项」与实际开始分钟（不早于
  计划开始及上次实际结束，且 SQLite 可存），通过后写入 running 记录、
  冻结当时计划起止、计划 version +1（计划项保留）；
- 结束（finish_furnace_run）对进行中炉次保存结束分钟、置 finished、
  释放占炉、移除对应计划项并计划 version +1，一起生效，不自动挪动后续
  计划；
- 计划修改（submit_furnace_plan）若改时或移除进行中项则整批拒绝；已结束
  炉次不得重新排入（任何对其计划项的设置都 409；移除早已被结束删去的
  计划项为空操作）。

已结束炉次的实测温度质检（run_quality 表，每炉次一份，整份保存或替换，
不合格也保存）与开炉登记共用同一把写互斥锁，按请求先后串行生效：

- 提交质检（submit_run_quality）要求炉次已结束（未知炉次 404，未开炉 /
  进行中 422），实测为 1～20 个 0～200°C 有限数且档数 ×5 等于实际时长；
  按归档冻结配方核验连续保温与全程耐受（不核验升降温及末温），逐项结论
  与实测整份落库；
- 阻断由质检表即时推导：任一未达标或超限项对应的（配方 ID, 冻结版本）
  被阻断，来源为对应炉次；替换实测按新结论增减本炉次阻断，同版其他
  失败来源仍在则继续阻断；
- 开炉（start_furnace_run）按归档冻结的配方版本核对当前阻断，命中即
  409 并附配方与来源炉次；先开炉者不受此后提交的阻断影响，仍可结束。
  配方修改 / 停用不改质检记录与阻断，计划与归档也不受质检读写影响。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from . import quality, reuse
from .db import connect, lock, write_lock
from .solver import Material, solve


# ---------------- 异常（路由层映射为 HTTP 状态码） ----------------


class StoreError(Exception):
    status_code = 400


class NotFound(StoreError):
    status_code = 404


class Conflict(StoreError):
    """状态冲突：草稿已归档，或携带的版本已过期（HTTP 409）。"""

    status_code = 409

    def __init__(
        self,
        detail: str,
        current_version: int | None = None,
        extra: dict | None = None,
    ):
        super().__init__(detail)
        self.detail = detail
        self.current_version = current_version
        # 随 409 一起返回的附加字段（如质检阻断的配方与来源炉次）
        self.extra = extra or {}


class Unprocessable(StoreError):
    """语义上无法处理：引用非法，或待归档草稿当前无解（HTTP 422）。"""

    status_code = 422


# ---------------- 小工具 ----------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


@contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _recipe_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "holdLow": row["hold_low"],
        "holdHigh": row["hold_high"],
        "duration": row["duration"],
        "tolerance": row["tolerance"],
        "status": row["status"],
        "version": row["version"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _material_snapshot(row) -> dict:
    """一份配方在某个时刻对应的材料参数（含默认名称）。"""
    return {
        "name": (row["name"] or "").strip() or f"配方{row['id']}",
        "holdLow": row["hold_low"],
        "holdHigh": row["hold_high"],
        "duration": row["duration"],
        "tolerance": row["tolerance"],
    }


def _to_material(row) -> Material:
    snap = _material_snapshot(row)
    return Material(
        name=snap["name"],
        hold_low=snap["holdLow"],
        hold_high=snap["holdHigh"],
        duration=snap["duration"],
        tolerance=snap["tolerance"],
    )


def _draft_dict(row) -> dict:
    """组装炉次草稿读取结构：状态、参数、版本、结果或无解原因。

    活动草稿的 materials 与 result/reason 是同一次求解一起写入的快照；
    已归档草稿则读取归档时冻结的快照，二者绝不会混用。已作废草稿保留
    作废前的参数快照与结果/原因（作废不重算），并附作废时间。
    """
    recipe_ids = json.loads(row["recipe_ids"])
    archived = row["archive_id"] is not None
    cancelled = not archived and row["status"] == "cancelled"

    if archived:
        materials = json.loads(row["archived_materials_json"])
        result = json.loads(row["archived_result_json"])
        solvable = True
        reason = None
        recipe_versions = json.loads(row["recipe_versions_json"])
    else:
        materials = json.loads(row["materials_json"])
        solvable = bool(row["solvable"])
        result = json.loads(row["result_json"]) if row["result_json"] else None
        reason = row["reason"]
        recipe_versions = json.loads(row["recipe_versions_json"])

    return {
        "id": row["id"],
        "name": row["name"],
        "status": (
            "archived" if archived else ("cancelled" if cancelled else "active")
        ),
        "recipeIds": recipe_ids,
        "recipeVersions": recipe_versions,
        "version": row["archived_draft_version"] if archived else row["version"],
        "materials": materials,
        "solvable": solvable,
        "result": result,
        "reason": reason,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "archivedAt": row["archived_at"],
        "cancelledAt": row["cancelled_at"] if cancelled else None,
    }


_DRAFT_SELECT = """
SELECT d.*, a.id AS archive_id, a.draft_version AS archived_draft_version,
       a.materials_json AS archived_materials_json,
       a.result_json AS archived_result_json, a.archived_at
FROM drafts d
LEFT JOIN archives a ON a.draft_id = d.id
"""


# ---------------- 配方 ----------------


def list_recipes() -> list[dict]:
    with lock(), connect() as conn:
        rows = conn.execute("SELECT * FROM recipes ORDER BY id").fetchall()
        return [_recipe_dict(r) for r in rows]


def get_recipe(recipe_id: int) -> dict:
    with lock(), connect() as conn:
        row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if row is None:
            raise NotFound(f"配方 {recipe_id} 不存在。")
        return _recipe_dict(row)


def _recipe_version_dict(row) -> dict:
    return {
        "version": row["version"],
        "name": row["name"],
        "holdLow": row["hold_low"],
        "holdHigh": row["hold_high"],
        "duration": row["duration"],
        "tolerance": row["tolerance"],
    }


def list_recipe_versions(recipe_id: int) -> list[dict]:
    """按 version 升序列出配方的全部历史参数（版本号、名称与全部材料参数）。

    历史只追加：创建记版本 1，每次成功整份修改（含从历史恢复）记新版本；
    停用 / 恢复可用状态不记历史。配方不存在 => 404。
    """
    with lock(), connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM recipes WHERE id=?", (recipe_id,)
        ).fetchone()
        if exists is None:
            raise NotFound(f"配方 {recipe_id} 不存在。")
        rows = conn.execute(
            """SELECT * FROM recipe_versions WHERE recipe_id=?
               ORDER BY version""",
            (recipe_id,),
        ).fetchall()
        return [_recipe_version_dict(r) for r in rows]


def create_recipe(
    name: str, hold_low: int, hold_high: int, duration: int, tolerance: int
) -> dict:
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            cur = conn.execute(
                """INSERT INTO recipes
                   (name, hold_low, hold_high, duration, tolerance,
                    version, created_at, updated_at)
                   VALUES (?,?,?,?,?,1,?,?)""",
                (name, hold_low, hold_high, duration, tolerance, now, now),
            )
            rid = cur.lastrowid
            # 新配方版本从 1 记起：参数历史与配方在同一事务一起写入
            conn.execute(
                """INSERT INTO recipe_versions
                   (recipe_id, version, name, hold_low, hold_high,
                    duration, tolerance)
                   VALUES (?,?,?,?,?,?,?)""",
                (rid, 1, name, hold_low, hold_high, duration, tolerance),
            )
        return get_recipe(rid)


# 锁外求解失败后允许的无锁重试次数；期间的并发修改会让整批结果作废重算。
# 超出后转入兜底路径：持写互斥锁走完 快照→求解→提交 全程（求解仍不持
# 读锁），必然落在他次修改之后，按某一先后顺序串行生效，永远不会把修改
# 请求做丢。
_RECOMPUTE_MAX_ATTEMPTS = 5


def update_recipe(
    recipe_id: int,
    name: str,
    hold_low: int,
    hold_high: int,
    duration: int,
    tolerance: int,
) -> tuple[dict, list[dict]]:
    """整份修改配方（版本 +1），并原子地重算所有引用它的活动草稿。

    两阶段提交流程（单进程，无后台任务）：

    1. 锁内取快照：配方当前参数/版本与所有引用它的活动草稿、各引用配方
       当前参数/版本；
    2. **锁外**用新参数求解全部受影响草稿——此期间不持锁、未写任何数据，
       GET /api/recipes、GET /api/drafts、GET /api/drafts/{id} 读到的仍是
       上一整次提交的完整状态（旧参数 + 旧结果），绝看不到半成品；
    3. 锁内、单条 BEGIN IMMEDIATE 事务里复核并发漂移（配方或引用配方被
       他次修改、草稿被归档、出现引用该配方的新草稿），无漂移才把配方新
       参数/版本与全部草稿的新快照、结果/原因、版本一起 COMMIT；有漂移则
       整批作废，刷新快照重新求解。

    已归档炉次不受影响。引用草稿（无论新结果是否有解）版本一律 +1；
    无解草稿清除旧曲线并记录原因。求解异常 / 写库异常时本事务回滚，本次
    修改全部不生效。重试耗尽时转入兜底路径：持写互斥锁（读操作不经过它）
    走完 快照→求解→提交 全程——求解期间 GET 依旧立即读到上一整次提交
    的完整状态，其他写操作在写互斥锁上排队，快照到提交之间不会再插入
    他次写操作，保证请求最终成功且严格串行在他次修改之后。
    """
    # 阶段 1 + 2：锁内快照、锁外求解，允许在并发修改后整批重算
    for _ in range(_RECOMPUTE_MAX_ATTEMPTS):
        with lock():
            snapshot = _snapshot_recipe_recompute(recipe_id)
        prepared = _solve_recompute(
            snapshot,
            name,
            hold_low,
            hold_high,
            duration,
            tolerance,
        )

        with write_lock(), lock(), connect() as conn:
            with _tx(conn):
                affected = _commit_recipe_recompute(conn, recipe_id, prepared)
            if affected is not None:
                recipe = get_recipe(recipe_id)
                drafts = [get_draft(did) for did in affected]
                return recipe, drafts
            # 提交期发现并发漂移：整批结果作废，刷新快照后重新求解

    # 兜底：并发过于激烈时，持写互斥锁走完 快照→求解→提交 全程。求解期间
    # 不持全局读锁，GET 读到的仍是上一整次提交的完整状态；其他写操作在
    # 写互斥锁上排队，与本次修改严格按某一先后顺序生效，因此快照到提交
    # 之间不会再有漂移，复核一次通过、必然提交成功，不漏草稿、不覆盖、
    # 不改归档。
    with write_lock():
        while True:
            with lock():
                snapshot = _snapshot_recipe_recompute(recipe_id)
            prepared = _solve_recompute(
                snapshot,
                name,
                hold_low,
                hold_high,
                duration,
                tolerance,
            )
            with lock(), connect() as conn:
                with _tx(conn):
                    affected = _commit_recipe_recompute(conn, recipe_id, prepared)
                if affected is not None:
                    recipe = get_recipe(recipe_id)
                    drafts = [get_draft(did) for did in affected]
                    return recipe, drafts
                # 持写互斥锁期间没有其他写操作生效，复核必然通过；
                # 循环仅为防御，正常只走一遍


def restore_recipe(
    recipe_id: int, target_version: int, current_version: int
) -> tuple[dict, list[dict]]:
    """把选定历史版本恢复为当前配方（一次成型，不做任何重试）。

    请求携带目标历史 version（target_version）与查看时的当前 version
    （current_version）：

    - 配方不存在 => 404；携带的当前版本与现状不一致 => 409（附当前版本）；
      目标历史版本不存在 => 404；
    - 成功则**复制旧版的名称与全部材料参数**，配方当前 version +1，**保持
      当前可用状态**（停用中恢复仍停用），并追加一条新的参数历史；按原
      规则在同一事务里重算全部活动引用草稿（版本各 +1，无解清除旧曲线），
      已归档炉次不改。

    与整份修改相同的两阶段提交：锁内取快照（含目标历史行），**锁外**求解
    全部受影响草稿（求解期间不持锁、不写数据，GET 仍立即读到上一整次已
    提交的完整状态），再上锁在单条事务里复核并整体提交。但恢复**只有这
    一次机会**：快照与提交之间若有任何他次写操作生效——包括并发的普通
    修改先于本次提交（哪怕恢复已经完成求解）——复核即失败，直接返回 409，
    **绝不重试、绝不覆盖新修改**。求解或写库异常时事务回滚，本次恢复整体
    不生效（历史、配方、草稿都不留痕）。
    """
    # 阶段 1：锁内快照与预检（配方 404 → 当前版本 409 → 历史版本 404）
    with lock():
        snapshot = _snapshot_recipe_recompute(recipe_id, target_version)
    if snapshot["target"]["version"] != current_version:
        raise Conflict(
            f"配方已过期：你查看的是版本 {current_version}，"
            f"当前为版本 {snapshot['target']['version']}，请重新读取后再恢复。",
            snapshot["target"]["version"],
        )
    if snapshot["history"] is None:
        raise NotFound(f"配方 {recipe_id} 的历史版本 {target_version} 不存在。")
    history = snapshot["history"]

    # 阶段 2：锁外按旧版参数求解全部受影响草稿；不持锁、不写数据
    prepared = _solve_recompute(
        snapshot,
        history["name"],
        history["hold_low"],
        history["hold_high"],
        history["duration"],
        history["tolerance"],
    )

    # 阶段 3：锁内单条事务复核并整体提交；任何漂移都直接 409，不重试
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            affected = _commit_recipe_recompute(
                conn, recipe_id, prepared, strict=True
            )
        recipe = get_recipe(recipe_id)
        drafts = [get_draft(did) for did in affected]
    return recipe, drafts


def set_recipe_status(recipe_id: int, status: str) -> dict:
    """停用（disabled）/ 恢复（active）配方，只切换状态本身。

    不改变材料参数，不增加配方或任何草稿的版本，也不重算已有炉次；
    重复停用 / 恢复为幂等。与新建炉次并发时由写互斥锁串行化：停用先
    生效，则引用它的新建因「已停用」以 422 失败。
    """
    assert status in ("active", "disabled")  # 路由层只传这两个值
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            row = conn.execute(
                "SELECT id FROM recipes WHERE id=?", (recipe_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"配方 {recipe_id} 不存在。")
            conn.execute(
                "UPDATE recipes SET status=? WHERE id=?", (status, recipe_id)
            )
        return get_recipe(recipe_id)


def _snapshot_recipe_recompute(recipe_id: int, history_version: int | None = None) -> dict:
    """在已持有全局锁时读取修改所需快照（只读，不写任何数据）。

    包含：目标配方当前行、所有引用它的活动草稿（未归档且未作废；已作废
    炉次保留记录但不再参与重算），以及这些草稿各自引用配方的当前行。
    全部读取自同一连接、同一锁时点，状态彼此自洽。

    history_version 不为 None 时（从历史恢复），同时读取目标历史版本行。
    恢复入口在调用本函数后才复核「携带的当前版本是否过期」，为保证
    「配方 404 → 当前已变化 409 → 历史版本不存在 404」的判定先后，
    目标历史行的缺失检查交由 restore_recipe 显式完成，这里只带回行数据
    （缺失为 None）。
    """
    with connect() as conn:
        target = conn.execute(
            "SELECT * FROM recipes WHERE id=?", (recipe_id,)
        ).fetchone()
        if target is None:
            raise NotFound(f"配方 {recipe_id} 不存在。")
        target = dict(target)

        history_row = None
        if history_version is not None:
            hr = conn.execute(
                """SELECT * FROM recipe_versions
                   WHERE recipe_id=? AND version=?""",
                (recipe_id, history_version),
            ).fetchone()
            history_row = dict(hr) if hr is not None else None

        drafts = [
            dict(d)
            for d in conn.execute(
                """SELECT d.* FROM drafts d
                   LEFT JOIN archives a ON a.draft_id = d.id
                   WHERE a.id IS NULL AND d.status = 'active'
                   ORDER BY d.id"""
            ).fetchall()
            if recipe_id in json.loads(d["recipe_ids"])
        ]
        ref_ids = {rid for d in drafts for rid in json.loads(d["recipe_ids"])}
        ref_ids.add(recipe_id)
        placeholders = ",".join("?" * len(ref_ids))
        recipes = {
            r["id"]: dict(r)
            for r in conn.execute(
                f"SELECT * FROM recipes WHERE id IN ({placeholders})",
                list(ref_ids),
            ).fetchall()
        }
    snapshot = {"target": target, "drafts": drafts, "recipes": recipes}
    if history_version is not None:
        # 缺失（None）的判定交给 restore_recipe，以保证
        # 「当前版本过期 409」优先于「历史版本不存在 404」
        snapshot["history"] = history_row
    return snapshot


def _solve_recompute(
    snapshot: dict,
    name: str,
    hold_low: int,
    hold_high: int,
    duration: int,
    tolerance: int,
) -> dict:
    """锁外按新参数求解全部受影响草稿；只做计算，不碰数据库。

    目标配方行用新参数覆盖（版本记为 base_version+1），其余引用配方沿用
    快照值。异常直接向上抛出：此时尚未写入任何数据，本次修改天然全不生效。
    """
    recipe_id = snapshot["target"]["id"]
    base_version = snapshot["target"]["version"]

    target = dict(snapshot["target"])
    target.update(
        name=name,
        hold_low=hold_low,
        hold_high=hold_high,
        duration=duration,
        tolerance=tolerance,
        version=base_version + 1,
    )
    recipes = dict(snapshot["recipes"])
    recipes[recipe_id] = target

    prepared_drafts: list[dict] = []
    for d in snapshot["drafts"]:
        refs = json.loads(d["recipe_ids"])
        ordered = [recipes[r] for r in refs]
        materials = [_to_material(r) for r in ordered]
        snapshot_params = [_material_snapshot(r) for r in ordered]
        versions = {str(r["id"]): r["version"] for r in ordered}
        outcome = solve(materials)
        if outcome.get("solvable"):
            solvable, result_json, reason = 1, _dumps(outcome), None
        else:
            # 无解：仍保存，但清除旧曲线并记录原因
            solvable, result_json, reason = 0, None, outcome.get("reason")
        prepared_drafts.append(
            {
                "id": d["id"],
                "base_version": d["version"],
                # 快照时点数据库中各引用配方的版本（目标配方此刻还未 +1）
                "base_ref_versions": {
                    str(rid): snapshot["recipes"][rid]["version"] for rid in refs
                },
                "materials_json": _dumps(snapshot_params),
                "recipe_versions_json": _dumps(versions),
                "solvable": solvable,
                "result_json": result_json,
                "reason": reason,
            }
        )

    return {
        "recipe_id": recipe_id,
        "base_version": base_version,
        "fields": (name, hold_low, hold_high, duration, tolerance),
        "drafts": prepared_drafts,
    }


def _commit_recipe_recompute(conn, recipe_id: int, prepared: dict, strict: bool = False):
    """锁内、单条事务内复核漂移并整体提交；返回受影响草稿 ID。

    任一复核不过即说明快照后有他次写操作生效（并发修改共享配方、创建引用
    炉次、归档、作废等）：普通整份修改（strict=False）返回 None，让调用方
    整批作废、刷新快照重算，绝不覆盖他次修改、不漏算新草稿、不触碰已归档
    冻结方案、不回写已作废炉次；从历史恢复（strict=True）**不允许重试**——
    普通修改先于恢复提交时，即便恢复已完成求解也直接抛 Conflict（409），
    由调用方放弃本次恢复，绝不重试后覆盖新修改。复核通过后，配方新参数/
    版本、新追加的参数历史与全部草稿的新参数快照、结果/原因、版本在同一
    事务里一起生效。
    """

    def drift(current: int | None = None) -> None:
        if strict:
            raise Conflict(
                "配方在恢复求解期间已发生变化，请重新读取历史与当前参数后再恢复。",
                current,
            )

    target = conn.execute(
        "SELECT * FROM recipes WHERE id=?", (recipe_id,)
    ).fetchone()
    if target is None:
        # 配方没有删除接口；缺失意味着状态已不可预期，直接回滚
        raise NotFound(f"配方 {recipe_id} 不存在。")
    if target["version"] != prepared["base_version"]:
        drift(target["version"])  # 普通修改重试；恢复：他次修改已先生效，直接 409
        return None

    # 受影响草稿集合必须与快照一致：不多（不能改写已归档/已作废/新建外的
    # 草稿）、不少（引用该配方的新草稿必须纳入）
    current_active = [
        d
        for d in conn.execute(
            """SELECT d.* FROM drafts d
               LEFT JOIN archives a ON a.draft_id = d.id
               WHERE a.id IS NULL AND d.status = 'active'
               ORDER BY d.id"""
        ).fetchall()
        if recipe_id in json.loads(d["recipe_ids"])
    ]
    by_id = {d["id"]: d for d in current_active}
    expected_ids = {d["id"] for d in prepared["drafts"]}
    if set(by_id) != expected_ids:
        drift()  # 快照后有草稿被归档/作废，或有引用该配方的新炉次创建
        return None

    for pd in prepared["drafts"]:
        cur = by_id[pd["id"]]
        if cur["version"] != pd["base_version"]:
            drift()  # 该草稿已被他次修改（其他共享配方的修改）重算
            return None
        # 该草稿引用的每个配方版本都必须与求解快照时一致（目标配方此刻
        # 也尚未 +1）；若有其他共享配方被改过，本批结果必须作废重算
        for rid, snapshot_version in pd["base_ref_versions"].items():
            row = conn.execute(
                "SELECT version FROM recipes WHERE id=?", (int(rid),)
            ).fetchone()
            if row is None or row["version"] != snapshot_version:
                drift()
                return None

    # 复核通过：配方与全部受影响草稿在同一事务内一起生效
    name, hold_low, hold_high, duration, tolerance = prepared["fields"]
    new_version = prepared["base_version"] + 1
    now = _now()
    conn.execute(
        """UPDATE recipes SET name=?, hold_low=?, hold_high=?, duration=?,
               tolerance=?, version=version+1, updated_at=?
           WHERE id=?""",
        (name, hold_low, hold_high, duration, tolerance, now, recipe_id),
    )
    # 每次成功整份修改（含从历史恢复）都追加一条参数历史；停用/恢复不经过
    # 这里，因此不会记参数历史。历史与配方、草稿同一事务，异常整体回滚。
    conn.execute(
        """INSERT INTO recipe_versions
           (recipe_id, version, name, hold_low, hold_high, duration, tolerance)
           VALUES (?,?,?,?,?,?,?)""",
        (recipe_id, new_version, name, hold_low, hold_high,
         duration, tolerance),
    )
    for pd in prepared["drafts"]:
        conn.execute(
            """UPDATE drafts SET materials_json=?, recipe_versions_json=?,
                   solvable=?, result_json=?, reason=?, version=version+1,
                   updated_at=?
               WHERE id=?""",
            (
                pd["materials_json"],
                pd["recipe_versions_json"],
                pd["solvable"],
                pd["result_json"],
                pd["reason"],
                now,
                pd["id"],
            ),
        )
    return sorted(by_id)


# ---------------- 炉次草稿 ----------------

# 新建炉次锁外求解失败后允许的无锁重试次数；期间的并发修改（引用配方被
# 整份修改）会让整批结果作废重算。超出后转入兜底路径：持写互斥锁走完
# 快照→求解→提交 全程（求解仍不持读锁），与配方重算的兜底策略一致。
_CREATE_MAX_ATTEMPTS = 5


def list_drafts() -> list[dict]:
    with lock(), connect() as conn:
        rows = conn.execute(_DRAFT_SELECT + " ORDER BY d.id").fetchall()
        return [_draft_dict(r) for r in rows]


def get_draft(draft_id: int) -> dict:
    with lock(), connect() as conn:
        row = conn.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        return _draft_dict(row)


def create_draft(name: str, recipe_ids: list[int]) -> dict:
    """按顺序引用 2~3 个不同的可用配方；引用不可改，从 20°C 空记录求解。

    引用不存在或已停用的配方 => 422（指明配方 ID），不保存炉次。

    与配方重算相同的两阶段提交流程：

    1. 锁内取引用配方快照（存在 / 可用性在此校验）；
    2. **锁外**求解——此期间不持锁、未写任何数据，GET /api/recipes、
       GET /api/drafts、GET /api/drafts/{id} 仍立即读到上一整次提交的
       完整状态，新草稿在整体提交前绝不可见；
    3. 锁内、单条 BEGIN IMMEDIATE 事务里复核引用配方（仍存在、仍可用、
       版本未被他次修改漂移），无漂移才把新草稿一次写入——参数快照、
       结果/原因与引用版本严格同版。有漂移则整批作废、刷新快照重新求解。

    停用与新建按某一先后顺序生效：停用先提交，则新建在快照或提交复核时
    看到停用，以 422 失败且不保存。求解异常 / 写库异常时本次新建全部
    不生效，不会留下新草稿。重试耗尽时转入兜底路径：持写互斥锁（读
    操作不经过它）走完 快照→求解→提交 全程——求解期间 GET 依旧立即
    读到上一整次提交的完整状态，其他写操作在写互斥锁上排队，快照到
    提交之间不会再插入他次写操作，保证本次新建必然整体提交成功。
    """
    # 阶段 1 + 2：锁内快照、锁外求解，允许在并发修改后整批重算
    for _ in range(_CREATE_MAX_ATTEMPTS):
        with lock():
            snapshot = _snapshot_draft_create(recipe_ids)
        prepared = _solve_draft_create(snapshot, name, recipe_ids)

        with write_lock(), lock(), connect() as conn:
            with _tx(conn):
                draft_id = _commit_draft_create(conn, prepared)
            if draft_id is not None:
                return get_draft(draft_id)
            # 提交期发现引用配方被并发修改：整批作废，刷新快照重新求解

    # 兜底：并发过于激烈时，持写互斥锁走完 快照→求解→提交 全程。求解
    # 期间不持全局读锁，GET 读到的仍是上一整次提交的完整状态；其他写
    # 操作在写互斥锁上排队，与本次新建严格按某一先后顺序生效，因此
    # 快照到提交之间不会再有漂移，复核一次通过、必然提交成功。
    with write_lock():
        while True:
            with lock():
                snapshot = _snapshot_draft_create(recipe_ids)
            prepared = _solve_draft_create(snapshot, name, recipe_ids)
            with lock(), connect() as conn:
                with _tx(conn):
                    draft_id = _commit_draft_create(conn, prepared)
                if draft_id is not None:
                    return get_draft(draft_id)
                # 持写互斥锁期间没有其他写操作生效，复核必然通过；
                # 循环仅为防御，正常只走一遍


def _snapshot_draft_create(recipe_ids: list[int]) -> dict:
    """在已持有全局锁时读取新建炉次所需的引用配方快照（只读，不写数据）。

    引用配方不存在或已停用 => 422（指明配方 ID），与原先锁内校验口径
    一致；通过则返回按引用顺序排列的配方行（含版本），供锁外求解与
    提交复核使用。全部读取自同一连接、同一锁时点，状态彼此自洽。
    """
    with connect() as conn:
        placeholders = ",".join("?" * len(recipe_ids))
        rows = conn.execute(
            f"SELECT * FROM recipes WHERE id IN ({placeholders})", recipe_ids
        ).fetchall()
    by_id = {r["id"]: r for r in rows}
    missing = [rid for rid in recipe_ids if rid not in by_id]
    if missing:
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, missing))} 不存在，无法引用。"
        )
    ordered = [by_id[r] for r in recipe_ids]
    # 停用配方不可用于新建炉次（不保存草稿）；已有引用不受影响
    disabled = [r["id"] for r in ordered if r["status"] != "active"]
    if disabled:
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, disabled))} 已停用，"
            "不能用于新建炉次。"
        )
    return {"ordered": [dict(r) for r in ordered]}


def _solve_draft_create(
    snapshot: dict, name: str, recipe_ids: list[int]
) -> dict:
    """锁外用引用配方快照从 20°C 空记录求解新炉次；只做计算，不碰数据库。

    异常直接向上抛出：此时尚未写入任何数据，本次新建天然全不生效。
    """
    ordered = snapshot["ordered"]
    materials = [_to_material(r) for r in ordered]
    snapshot_params = [_material_snapshot(r) for r in ordered]
    versions = {str(r["id"]): r["version"] for r in ordered}
    # 从 20°C 空记录求解（executed=None），复用原求解规则
    outcome = solve(materials)
    if outcome.get("solvable"):
        solvable, result_json, reason = 1, _dumps(outcome), None
    else:
        # 无解：仍保存，但不保留任何曲线，记录中文原因
        solvable, result_json, reason = 0, None, outcome.get("reason")
    return {
        "name": name,
        "recipe_ids": list(recipe_ids),
        "ordered": ordered,
        "materials_json": _dumps(snapshot_params),
        "recipe_versions_json": _dumps(versions),
        "solvable": solvable,
        "result_json": result_json,
        "reason": reason,
    }


def _commit_draft_create(conn, prepared: dict):
    """锁内、单条事务内复核引用配方并写入新草稿；返回新草稿 ID。

    复核：引用配方仍存在（配方无删除接口，仅作防御）、仍可用（求解
    期间被停用 => 422，与「停用先于新建生效」口径一致，不保存草稿）、
    版本与求解快照一致（否则返回 None，让调用方整批作废、按最新参数
    重新求解）。复核通过才 INSERT：新草稿的参数快照、结果/原因与引用
    版本严格同版，且在整体提交前对任何读取不可见。
    """
    recipe_ids = prepared["recipe_ids"]
    snapshot_by_id = {r["id"]: r for r in prepared["ordered"]}
    placeholders = ",".join("?" * len(recipe_ids))
    rows = conn.execute(
        f"SELECT * FROM recipes WHERE id IN ({placeholders})", recipe_ids
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    missing = [rid for rid in recipe_ids if rid not in by_id]
    if missing:
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, missing))} 不存在，无法引用。"
        )
    disabled = [rid for rid in recipe_ids if by_id[rid]["status"] != "active"]
    if disabled:
        raise Unprocessable(
            f"配方 ID {', '.join(map(str, disabled))} 已停用，"
            "不能用于新建炉次。"
        )
    for rid in recipe_ids:
        if by_id[rid]["version"] != snapshot_by_id[rid]["version"]:
            return None  # 引用配方被他次修改：整批作废，刷新快照重算

    now = _now()
    cur = conn.execute(
        """INSERT INTO drafts
           (name, recipe_ids, version, recipe_versions_json,
            materials_json, solvable, result_json, reason,
            created_at, updated_at)
           VALUES (?,?,1,?,?,?,?,?,?,?)""",
        (
            prepared["name"],
            _dumps(recipe_ids),
            prepared["recipe_versions_json"],
            prepared["materials_json"],
            prepared["solvable"],
            prepared["result_json"],
            prepared["reason"],
            now,
            now,
        ),
    )
    return cur.lastrowid


def archive_draft(draft_id: int, expected_version: int) -> dict:
    """归档核准的炉次方案。

    - 携带所查看的草稿版本：与当前版本不一致 => 409（附当前版本）；
    - 草稿当前无解 => 422（不改变任何状态）；
    - 已归档 => 409；已作废 => 409（作废炉次不再允许归档）；
    - 成功则冻结当时的材料参数、草稿版本与完整求解结果；此后配方修改
      不再影响该炉次。
    """
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            row = conn.execute(
                _DRAFT_SELECT + " WHERE d.id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")
            if row["archive_id"] is not None:
                raise Conflict("该炉次已归档，不能重复归档。", row["version"])
            if row["status"] == "cancelled":
                raise Conflict("该炉次已作废，不能归档。", row["version"])
            if row["version"] != expected_version:
                raise Conflict(
                    f"炉次草稿已过期：你查看的是版本 {expected_version}，"
                    f"当前为版本 {row['version']}，请重新读取后再归档。",
                    row["version"],
                )
            if not row["solvable"]:
                raise Unprocessable(
                    row["reason"] or "该炉次当前无可行曲线，无法归档。"
                )

            conn.execute(
                """INSERT INTO archives
                   (draft_id, draft_version, materials_json, result_json, archived_at)
                   VALUES (?,?,?,?,?)""",
                (
                    draft_id,
                    row["version"],
                    row["materials_json"],
                    row["result_json"],
                    now,
                ),
            )
        return get_draft(draft_id)


def cancel_draft(draft_id: int, expected_version: int) -> dict:
    """作废不再执行的炉次草稿：保留记录而非删除。

    - 未知炉次 => 404；
    - 已归档 => 409；已作废 => 409（重复作废幂等拒绝）；
    - 携带的查看版本与当前不一致 => 409（附当前版本）；
    - 可行与无解草稿均可作废；成功则标记 status='cancelled'、version +1、
      记录作废时间，原引用、参数快照、结果或无解原因全部保留；此后不再
      允许归档，也不再被配方整份修改 / 历史恢复重算。

    与重算、归档并发时按某一先后顺序串行生效（同一把写互斥锁 + 单条
    事务）：作废先提交，则重算提交复核时发现活动草稿集合漂移、整批作废
    重算，已算出的结果不会回写到已作废炉次；重算先提交（版本已 +1），
    则携带旧版本的作废收到 409；归档先提交，则作废收到 409。
    """
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            row = conn.execute(
                _DRAFT_SELECT + " WHERE d.id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")
            if row["archive_id"] is not None:
                raise Conflict("该炉次已归档，不能作废。", row["version"])
            if row["status"] == "cancelled":
                raise Conflict("该炉次已作废，不能重复作废。", row["version"])
            if row["version"] != expected_version:
                raise Conflict(
                    f"炉次草稿已过期：你查看的是版本 {expected_version}，"
                    f"当前为版本 {row['version']}，请重新读取后再作废。",
                    row["version"],
                )
            # 只切换状态与版本：引用、参数快照、结果或无解原因原样保留
            conn.execute(
                """UPDATE drafts SET status='cancelled', version=version+1,
                       cancelled_at=?, updated_at=?
                   WHERE id=?""",
                (now, now, draft_id),
            )
        return get_draft(draft_id)


# ---------------- 电费试算（只读归档冻结曲线） ----------------


def get_archived_curve(draft_id: int) -> dict:
    """读取已归档炉次的冻结曲线用于电费试算。

    - 炉次不存在 => NotFound（404）；
    - 炉次存在但未归档（活动草稿 / 无解草稿）=> Unprocessable（422）；
    - 已归档 => 返回冻结版本与冻结温度序列。只读，绝无任何写入。
    """
    with lock(), connect() as conn:
        row = conn.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        if row["archive_id"] is None:
            raise Unprocessable("该炉次尚未归档，电费试算只能针对已归档的冻结曲线。")
        result = json.loads(row["archived_result_json"])
        return {
            "id": draft_id,
            "version": row["archived_draft_version"],
            "temperatures": list(result["temperatures"]),
        }


# ---------------- 工艺单导出（只读归档快照，不写库） ----------------


def get_archived_sheet(draft_id: int) -> dict:
    """读取已归档炉次的**归档快照**用于导出离线 HTML 工艺单。

    - 炉次不存在 => NotFound（404）；
    - 炉次存在但未归档（活动草稿 / 无解草稿）=> Unprocessable（422）；
    - 已归档 => 返回炉次名称 / ID / 冻结版本 / 归档时间，以及按原引用
      顺序排列的冻结配方版本与冻结材料参数、冻结完整求解结果。

    纯只读：只取 archives 与 drafts 的冻结列，不读当前配方、不重新求解，
    绝不写入任何数据；后续配方修改、停用及重启都不改变返回内容。
    """
    with lock(), connect() as conn:
        row = conn.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        if row["archive_id"] is None:
            raise Unprocessable(
                "该炉次尚未归档，工艺单只能导出已归档炉次的冻结快照。"
            )
        return {
            "draft": {"id": row["id"], "name": row["name"]},
            "recipeIds": json.loads(row["recipe_ids"]),
            "recipeVersions": json.loads(row["recipe_versions_json"]),
            "materials": json.loads(row["archived_materials_json"]),
            "result": json.loads(row["archived_result_json"]),
            "version": row["archived_draft_version"],
            "archivedAt": row["archived_at"],
        }


# ---------------- 原曲线复用检查（只读，不重新求解） ----------------


def check_archived_reuse(draft_id: int) -> dict:
    """核验已归档炉次的冻结曲线能否用于各配方的**当前**参数与状态。

    - 炉次不存在 => NotFound（404）；
    - 炉次存在但未归档（活动草稿 / 无解草稿）=> Unprocessable（422）；
    - 已归档 => 在同一把锁、同一条连接内读取冻结曲线与全部引用配方的
      当前参数/状态（同一读取时点），按原引用顺序逐项核验并给出整体
      可复用结论。纯只读：不改配方、草稿或归档，也不重新求解。
    """
    with lock(), connect() as conn:
        row = conn.execute(_DRAFT_SELECT + " WHERE d.id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        if row["archive_id"] is None:
            raise Unprocessable(
                "该炉次尚未归档，复用检查只能针对已归档的冻结曲线。"
            )

        recipe_ids = json.loads(row["recipe_ids"])
        result = json.loads(row["archived_result_json"])
        temperatures = list(result["temperatures"])

        # 按原配方引用顺序读取当前参数与状态（引用创建后不可改，配方不会被删除）
        placeholders = ",".join("?" * len(recipe_ids))
        rows = conn.execute(
            f"SELECT * FROM recipes WHERE id IN ({placeholders})", recipe_ids
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        ordered = [by_id[rid] for rid in recipe_ids]

        items: list[dict] = []
        for r in ordered:
            # 停用配方同样完成温度检查；其状态只影响整体可复用结论
            check = reuse.check_item(temperatures, _to_material(r))
            items.append(
                {
                    "recipeId": r["id"],
                    "name": _material_snapshot(r)["name"],
                    "version": r["version"],
                    "status": r["status"],
                    **check,
                }
            )

        # 全部保温达标、无任何超限且配方均可用时，整体才可复用
        reusable = all(
            it["holdOk"]
            and it["toleranceBreachStep"] is None
            and it["status"] == "active"
            for it in items
        )
        return {
            "draftId": draft_id,
            "version": row["archived_draft_version"],
            "stepCount": len(temperatures),
            "temperatures": temperatures,
            "items": items,
            "reusable": reusable,
        }


# ---------------- 单台炉开炉计划（仅安排已归档炉次） ----------------


# SQLite 整数列是有符号 64 位：超出范围的 Python int 无法作为 INTEGER
# 绑定（直接抛 OverflowError），不截断、也不转浮点数
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _plan_items(conn) -> list[dict]:
    """在已持锁的连接上读取计划全部排段。

    每段的结束分钟 = 开始分钟 + 归档冻结曲线档数 × 5；时长不入库，因此
    归档曲线是唯一数据来源，不读当前配方、不重新求解。按开始分钟、再按
    炉次 ID 升序返回。
    """
    rows = conn.execute(
        """SELECT p.draft_id AS draft_id, p.start_minute AS start_minute,
                  a.result_json AS result_json
           FROM furnace_plan_entries p
           JOIN archives a ON a.draft_id = p.draft_id"""
    ).fetchall()
    items: list[dict] = []
    for r in rows:
        steps = len(json.loads(r["result_json"])["temperatures"])
        start = r["start_minute"]
        items.append(
            {
                "draftId": r["draft_id"],
                "startMinute": start,
                "endMinute": start + steps * 5,
            }
        )
    items.sort(key=lambda it: (it["startMinute"], it["draftId"]))
    return items


def _plan_version(conn) -> int:
    row = conn.execute(
        "SELECT version FROM furnace_plan_meta WHERE id=1"
    ).fetchone()
    # 无 meta 行即尚未提交过的空计划：版本为 1
    return int(row["version"]) if row is not None else 1


def get_furnace_plan() -> dict:
    """读取整台炉的开炉计划：版本 + 全部排段（按开始分钟、炉次 ID 排序）。

    空计划返回版本 1 与空数组。只读，持全局读锁，永远不会读到提交到一半
    的批次。
    """
    with lock(), connect() as conn:
        return {"version": _plan_version(conn), "items": _plan_items(conn)}


def submit_furnace_plan(
    expected_version: int, changes: list[tuple[int, int | None]]
) -> dict:
    """整批查看后提交开炉计划改动（upsert/移除，未提及项保留）。

    - 每项 (draft_id, start)：start 为非负整数 Unix 分钟表示设置 / 覆盖，
      None 表示移除该炉次（原本没有该炉次时为空操作）；同一批炉次 ID 不
      重复已由路由层以 422 拦截；
    - 未知炉次（含超出 SQLite 有符号 64 位整数范围、无法绑定查询的 ID）
      => 404；炉次存在但未归档 => 422（移除同样要求已归档）；开始分钟超出
      SQLite 可存储范围（>2^63-1）=> 422（中文原因，不截断、不转浮点）；
      每炉次时长取归档冻结曲线档数 × 5 分钟，不读当前配方、不重新求解；
    - 携带的查看版本与当前不一致 => 409（附当前版本）；
    - 按改动后的全部排段 [start, end) 判冲突：区间严格重叠才冲突，首尾
      相接（一端 = 另一端开始）允许；冲突 => 409；
    - 进行中炉次（已开炉未结束）的计划项不得改时或移除：移除、或覆盖为
      不同开始分钟 => 409（覆盖为相同开始分钟为无改动，允许）；已结束炉次
      不得重新排入，设置其开始分钟 => 409（其计划项已在结束时移除，对其
      传 null 为空操作）；
    - 成功才整批写入（未提及项保留）、版本 +1；任何失败都在 COMMIT 之前
      抛错回滚，不改变计划与版本。同版本并发提交由 BEGIN IMMEDIATE 与
      版本复核串行化：只有一批成功，其余收到 409；读取只持读锁，永远
      看不到半批。
    """
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            current_version = _plan_version(conn)
            if current_version != expected_version:
                raise Conflict(
                    f"开炉计划已过期：你查看的是版本 {expected_version}，"
                    f"当前为版本 {current_version}，请重新读取后再提交。",
                    current_version,
                )

            # 引用炉次必须存在且已归档：未知 => 404，未归档 => 422；
            # startMinute 还必须能原样存入 SQLite（有符号 64 位非负整数，
            # 不截断、不转浮点），存不下 => 422。按请求顺序给出第一个不合规
            # 炉次。超出 SQLite 整数范围的 ID 无法绑定查询，直接按未知处理。
            change_ids = [draft_id for draft_id, _ in changes]
            queryable_ids = [
                draft_id
                for draft_id in change_ids
                if _SQLITE_INT_MIN <= draft_id <= _SQLITE_INT_MAX
            ]
            by_id: dict[int, Any] = {}
            if queryable_ids:
                placeholders = ",".join("?" * len(queryable_ids))
                rows = conn.execute(
                    f"""SELECT d.id AS id, a.id AS archive_id,
                               a.result_json AS result_json
                        FROM drafts d
                        LEFT JOIN archives a ON a.draft_id = d.id
                        WHERE d.id IN ({placeholders})""",
                    queryable_ids,
                ).fetchall()
                by_id = {r["id"]: r for r in rows}
                # 执行记录（若有）：进行中项不得改时 / 移除；已结束项不得
                # 重新排入。随计划项读取一并取回（同一锁 / 事务时点）。
                run_rows = conn.execute(
                    f"""SELECT draft_id, status FROM furnace_runs
                        WHERE draft_id IN ({placeholders})""",
                    queryable_ids,
                ).fetchall()
                run_status = {r["draft_id"]: r["status"] for r in run_rows}
                plan_rows = conn.execute(
                    f"""SELECT draft_id, start_minute FROM furnace_plan_entries
                        WHERE draft_id IN ({placeholders})""",
                    queryable_ids,
                ).fetchall()
                plan_start = {r["draft_id"]: r["start_minute"] for r in plan_rows}
            else:
                run_status = {}
                plan_start = {}
            durations: dict[int, int] = {}
            for draft_id, start in changes:
                if not (_SQLITE_INT_MIN <= draft_id <= _SQLITE_INT_MAX):
                    raise NotFound(f"炉次草稿 {draft_id} 不存在。")
                r = by_id.get(draft_id)
                if r is None:
                    raise NotFound(f"炉次草稿 {draft_id} 不存在。")
                if r["archive_id"] is None:
                    raise Unprocessable(
                        f"炉次 {draft_id} 尚未归档，开炉计划只能安排已归档炉次。"
                    )
                # 执行状态冲突优先于分钟取值范围检查：进行中炉次的计划项
                # 只能保留原样——移除或改成不同开始时间一律 409（覆盖为
                # 相同开始时间为无实际改动，允许）；已结束炉次不得重新排入
                if run_status.get(draft_id) == "running":
                    if start is None or start != plan_start.get(draft_id):
                        raise Conflict(
                            f"炉次 {draft_id} 正在进行中，不能移除或修改其计划"
                            "开始时间；如需改排，请先结束该炉次。",
                            current_version,
                        )
                elif run_status.get(draft_id) == "finished" and start is not None:
                    raise Conflict(
                        f"炉次 {draft_id} 已经开炉执行并结束，不能重新排入开炉计划。",
                        current_version,
                    )
                # null 移除无需存储；设置值必须是 SQLite 可原样存下的非负整数
                if start is not None and not (0 <= start <= _SQLITE_INT_MAX):
                    raise Unprocessable(
                        f"炉次 {draft_id} 的开始分钟 {start} 超出可存储范围"
                        f"（最大 {_SQLITE_INT_MAX}），请提供更小的非负整数。"
                    )
                durations[draft_id] = (
                    len(json.loads(r["result_json"])["temperatures"]) * 5
                )

            # 改动前的全部排段（未提及项保留）
            existing = conn.execute(
                "SELECT draft_id, start_minute FROM furnace_plan_entries"
            ).fetchall()
            merged: dict[int, int] = {
                r["draft_id"]: r["start_minute"] for r in existing
            }
            for draft_id, start in changes:
                if start is None:
                    merged.pop(draft_id, None)
                else:
                    merged[draft_id] = start

            # 合并后全部炉次的时长：本批改动取前面已查到的档数，未提及而
            # 保留的炉次从其归档冻结曲线取（均为归档炉次，JOIN 必有行）
            missing_ids = sorted(set(merged) - set(durations))
            if missing_ids:
                ph = ",".join("?" * len(missing_ids))
                for r in conn.execute(
                    f"""SELECT draft_id, result_json FROM archives
                        WHERE draft_id IN ({ph})""",
                    missing_ids,
                ).fetchall():
                    durations[r["draft_id"]] = (
                        len(json.loads(r["result_json"])["temperatures"]) * 5
                    )

            # 按改动后的全部 [start, end) 判冲突：首尾相接允许
            intervals = sorted(
                (
                    (start, start + durations[draft_id], draft_id)
                    for draft_id, start in merged.items()
                ),
                key=lambda x: (x[0], x[2]),
            )
            for (s1, e1, id1), (s2, e2, id2) in zip(intervals, intervals[1:]):
                if s2 < e1:
                    raise Conflict(
                        f"炉次 {id1}（{s1}～{e1}）与炉次 {id2}"
                        f"（{s2}～{e2}）的开炉时间重叠，请调整后重新提交。"
                    )

            # 整批 upsert / 移除；同一事务内与版本 +1 一起生效
            for draft_id, start in changes:
                if start is None:
                    conn.execute(
                        "DELETE FROM furnace_plan_entries WHERE draft_id=?",
                        (draft_id,),
                    )
                else:
                    conn.execute(
                        """INSERT INTO furnace_plan_entries
                               (draft_id, start_minute, created_at, updated_at)
                           VALUES (?,?,?,?)
                           ON CONFLICT(draft_id) DO UPDATE SET
                               start_minute=excluded.start_minute,
                               updated_at=excluded.updated_at""",
                        (draft_id, start, now, now),
                    )

            new_version = current_version + 1
            conn.execute(
                """INSERT INTO furnace_plan_meta (id, version)
                   VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET version=excluded.version""",
                (new_version,),
            )

        # 提交完成后从库内重读完整计划（复用同一连接，保证与写入严格一致）
        return {"version": _plan_version(conn), "items": _plan_items(conn)}


# ---------------- 开炉 / 结束登记（仅登记，不控制设备） ----------------


def _run_dict(row) -> dict:
    """组装一条开炉执行记录的读取结构。

    含炉次 ID、开炉时冻结的计划起止、实际起止与状态（running /
    finished）；未结束时结束分钟（actualEndMinute）为 None。
    """
    return {
        "id": row["id"],
        "draftId": row["draft_id"],
        "planStartMinute": row["plan_start_minute"],
        "planEndMinute": row["plan_end_minute"],
        "actualStartMinute": row["actual_start_minute"],
        "actualEndMinute": row["actual_end_minute"],
        "status": row["status"],
        "planVersion": row["plan_version"],
        "startedAt": row["started_at"],
        "endedAt": row["ended_at"],
    }


_RUN_SELECT = """
SELECT id, draft_id, plan_start_minute, plan_end_minute,
       actual_start_minute, actual_end_minute, status, plan_version,
       started_at, ended_at
FROM furnace_runs
"""


def list_furnace_runs() -> dict:
    """按开炉顺序（自增 id）读取全部执行记录及当前占炉。

    返回 {"current": 进行中记录或 None, "runs": 全部记录}。只读，持全局
    读锁；进行中记录全表至多一条（由写路径的写互斥锁 + 单条事务保证）。
    """
    with lock(), connect() as conn:
        rows = conn.execute(_RUN_SELECT + " ORDER BY id").fetchall()
        runs = [_run_dict(r) for r in rows]
        current = next((r for r in runs if r["status"] == "running"), None)
        return {"current": current, "runs": runs}


def start_furnace_run(
    draft_id: int, expected_version: int, actual_start: int
) -> dict:
    """按计划开炉：只能启动当前计划首项；每炉次仅执行一次，同时仅一项进行中。

    单条事务内按以下顺序校验（任何失败都不写入、计划版本不变）：

    1. 所读计划版本与当前不一致 => 409（附当前版本）；
    2. 炉次 ID 未知（含超出 SQLite 有符号 64 位范围、无法绑定查询）=> 404；
    3. 该炉次已有执行记录（含进行中与已结束）=> 409（每炉次仅一次）；
    4. 已有另一炉次进行中（占炉未释放）=> 409（同时仅一项进行中）；
    5. 该炉次不在计划中，或不是当前计划首项（按开始分钟、炉次 ID 排序）
       => 409（只能启动计划首项）；
    6. 归档冻结的（配方 ID, 冻结版本）命中当前质检阻断 => 409（附当前
       版本、被阻断配方与来源炉次）；
    7. 实际开始分钟非法 => 422：不是 SQLite 可原样存下的非负整数，或早于
       计划开始，或早于上一炉次的实际结束分钟。

    校验通过：写入 running 执行记录，冻结当时的计划开始与计划结束
    （plan_end = plan_start + 归档冻结曲线档数 × 5），并把计划 version +1
    （计划项本身保留，直到结束才移除）。开炉不控制任何设备。
    """
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            current_version = _plan_version(conn)
            if current_version != expected_version:
                raise Conflict(
                    f"开炉计划已过期：你查看的是版本 {expected_version}，"
                    f"当前为版本 {current_version}，请重新读取后再开炉。",
                    current_version,
                )

            if not (_SQLITE_INT_MIN <= draft_id <= _SQLITE_INT_MAX):
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")
            draft = conn.execute(
                "SELECT id, recipe_ids, recipe_versions_json FROM drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft is None:
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")

            prior = conn.execute(
                "SELECT status FROM furnace_runs WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if prior is not None:
                if prior["status"] == "running":
                    raise Conflict(
                        f"炉次 {draft_id} 已开炉且正在进行中，不能重复开炉；"
                        "请先结束本炉次。",
                        current_version,
                    )
                raise Conflict(
                    f"炉次 {draft_id} 已经开炉执行并结束，每炉次只能执行一次。",
                    current_version,
                )

            running = conn.execute(
                "SELECT draft_id FROM furnace_runs WHERE status='running'"
            ).fetchone()
            if running is not None:
                raise Conflict(
                    f"炉次 {running['draft_id']} 正在进行中，同一时间只能有一项"
                    "炉次进行，请先结束当前炉次再开新炉。",
                    current_version,
                )

            # 计划首项：按开始分钟、再按炉次 ID 升序；计划为空或首项不是
            # 本炉次都拒绝
            first = conn.execute(
                """SELECT p.draft_id AS draft_id, p.start_minute AS start_minute,
                          a.result_json AS result_json
                   FROM furnace_plan_entries p
                   JOIN archives a ON a.draft_id = p.draft_id
                   ORDER BY p.start_minute, p.draft_id
                   LIMIT 1"""
            ).fetchone()
            if first is None or first["draft_id"] != draft_id:
                if first is None:
                    raise Conflict(
                        "当前没有任何开炉计划，只能启动计划首项，请先提交计划。",
                        current_version,
                    )
                raise Conflict(
                    f"只能启动当前计划首项（炉次 {first['draft_id']}）；"
                    f"炉次 {draft_id} 不是计划首项，请等待其排到首位。",
                    current_version,
                )

            plan_start = int(first["start_minute"])
            steps = len(json.loads(first["result_json"])["temperatures"])
            plan_end = plan_start + steps * 5

            # 质检阻断：按归档冻结的（配方 ID, 冻结版本）核对当前阻断，
            # 命中即 409 并附配方与来源炉次。计划首项必为已归档炉次，其
            # recipe_versions_json 即归档时冻结的版本。阻断与开炉在同一
            # 写互斥锁下按请求先后串行生效：阻断先提交则不能开炉；先开炉
            # 的进行中炉次不受此后提交的阻断影响，仍可正常结束。
            ref_versions = json.loads(draft["recipe_versions_json"])
            ref_pairs = {
                (rid, ref_versions[str(rid)])
                for rid in json.loads(draft["recipe_ids"])
            }
            hits = [
                b
                for b in _recipe_blocks(conn)
                if (b["recipeId"], b["version"]) in ref_pairs
            ]
            if hits:
                parts = "；".join(
                    f"配方 {b['recipeId']} 版本 {b['version']}"
                    f"（来源炉次 {', '.join(map(str, b['sourceDraftIds']))}）"
                    for b in hits
                )
                raise Conflict(
                    f"同版工艺已被质检阻断，不能开炉：{parts}。",
                    current_version,
                    extra={"blocks": hits},
                )

            if not (0 <= actual_start <= _SQLITE_INT_MAX):
                raise Unprocessable(
                    f"实际开始分钟 {actual_start} 超出可存储范围"
                    f"（最大 {_SQLITE_INT_MAX}），请提供更小的非负整数。"
                )
            if actual_start < plan_start:
                raise Unprocessable(
                    f"实际开始分钟 {actual_start} 早于该炉次的计划开始 "
                    f"{plan_start}，不能提前开炉。"
                )
            # 上次实际结束：按开炉顺序最后一条已结束记录（进行中项已在上面
            # 排除）；开始不得早于它
            last_finished = conn.execute(
                """SELECT draft_id, actual_end_minute FROM furnace_runs
                   WHERE status='finished'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if last_finished is not None:
                last_end = int(last_finished["actual_end_minute"])
                if actual_start < last_end:
                    raise Unprocessable(
                        f"实际开始分钟 {actual_start} 早于上一炉次"
                        f"（炉次 {last_finished['draft_id']}）的实际结束 "
                        f"{last_end}，炉子仍被占用。"
                    )
            if plan_end > _SQLITE_INT_MAX:
                # 计划结束分钟也要能随记录原样存入 SQLite
                raise Unprocessable(
                    f"炉次 {draft_id} 的计划结束分钟 {plan_end} 超出可存储范围"
                    f"（最大 {_SQLITE_INT_MAX}）。"
                )

            cur = conn.execute(
                """INSERT INTO furnace_runs
                       (draft_id, plan_start_minute, plan_end_minute,
                        actual_start_minute, actual_end_minute, status,
                        plan_version, started_at, ended_at)
                   VALUES (?,?,?,?,NULL,'running',?,?,NULL)""",
                (
                    draft_id,
                    plan_start,
                    plan_end,
                    actual_start,
                    current_version,
                    now,
                ),
            )
            run_id = cur.lastrowid
            new_version = current_version + 1
            conn.execute(
                """INSERT INTO furnace_plan_meta (id, version)
                   VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET version=excluded.version""",
                (new_version,),
            )

            row = conn.execute(
                _RUN_SELECT + " WHERE id=?", (run_id,)
            ).fetchone()
            return _run_dict(row)


def finish_furnace_run(draft_id: int, actual_end: int) -> dict:
    """结束进行中炉次：保存记录、释放占炉、移除计划项、计划 version +1。

    四件事在**同一事务里一起生效**；不自动挪动后续计划（后续排段的计划
    开始分钟保持原样）。校验顺序（任何失败都不改状态）：

    1. 炉次 ID 未知（含超出 SQLite 有符号 64 位范围）=> 404；
    2. 炉次存在但没有进行中的开炉记录（从未开炉 / 已结束）=> 409；
    3. 结束分钟非法 => 422：超出 SQLite 可存非负整数范围，或早于本次实际
       开始（相等允许，0 分钟工艺也可登记结束）。
    """
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            if not (_SQLITE_INT_MIN <= draft_id <= _SQLITE_INT_MAX):
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")
            draft = conn.execute(
                "SELECT id FROM drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if draft is None:
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")

            row = conn.execute(
                _RUN_SELECT + " WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                if row is None:
                    raise Conflict(f"炉次 {draft_id} 尚未开炉，无法结束。")
                raise Conflict(
                    f"炉次 {draft_id} 已结束，不能重复结束。"
                )

            if not (0 <= actual_end <= _SQLITE_INT_MAX):
                raise Unprocessable(
                    f"结束分钟 {actual_end} 超出可存储范围"
                    f"（最大 {_SQLITE_INT_MAX}），请提供更小的非负整数。"
                )
            if actual_end < int(row["actual_start_minute"]):
                raise Unprocessable(
                    f"结束分钟 {actual_end} 早于本次实际开始 "
                    f"{row['actual_start_minute']}，结束不能早于开始。"
                )

            conn.execute(
                """UPDATE furnace_runs
                   SET status='finished', actual_end_minute=?, ended_at=?
                   WHERE id=?""",
                (actual_end, now, row["id"]),
            )
            # 计划项随结束一起移除；正常情况下开炉后该项仍在（进行中项
            # 不允许被计划修改移除），DELETE 缺失时为空操作，保证「移除
            # 计划项与结束一起生效」
            conn.execute(
                "DELETE FROM furnace_plan_entries WHERE draft_id=?",
                (draft_id,),
            )
            current_version = _plan_version(conn)
            conn.execute(
                """INSERT INTO furnace_plan_meta (id, version)
                   VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET version=excluded.version""",
                (current_version + 1,),
            )

            updated = conn.execute(
                _RUN_SELECT + " WHERE id=?", (row["id"],)
            ).fetchone()
            return _run_dict(updated)


# ---------------- 已结束炉次实测温度质检与同版工艺阻断 ----------------


def _quality_dict(row) -> dict:
    """组装一份质检记录的读取结构：实测温度、逐项结论与整体结论。"""
    return {
        "draftId": row["draft_id"],
        "actualStartMinute": row["actual_start_minute"],
        "actualEndMinute": row["actual_end_minute"],
        "temperatures": json.loads(row["temperatures_json"]),
        "items": json.loads(row["items_json"]),
        "passed": bool(row["passed"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


_QUALITY_SELECT = """
SELECT q.draft_id AS draft_id, q.temperatures_json AS temperatures_json,
       q.items_json AS items_json, q.passed AS passed,
       q.created_at AS created_at, q.updated_at AS updated_at,
       r.actual_start_minute AS actual_start_minute,
       r.actual_end_minute AS actual_end_minute
FROM run_quality q
JOIN furnace_runs r ON r.draft_id = q.draft_id
"""


def _finished_run_row(conn, draft_id: int):
    """取已结束的开炉记录；未知炉次 404，未开炉 / 进行中 422。"""
    draft = conn.execute(
        "SELECT id FROM drafts WHERE id=?", (draft_id,)
    ).fetchone()
    if draft is None:
        raise NotFound(f"炉次草稿 {draft_id} 不存在。")
    run = conn.execute(
        """SELECT actual_start_minute, actual_end_minute, status
           FROM furnace_runs WHERE draft_id=?""",
        (draft_id,),
    ).fetchone()
    if run is None:
        raise Unprocessable(
            f"炉次 {draft_id} 尚未开炉，质检需要已结束的开炉记录。"
        )
    if run["status"] != "finished":
        raise Unprocessable(
            f"炉次 {draft_id} 尚未结束，质检需要已结束的开炉记录。"
        )
    return run


def submit_run_quality(draft_id: int, raw_temperatures) -> dict:
    """提交或整份替换一个已结束炉次的实测温度与质检结论（不合格也保存）。

    单条事务内按以下顺序校验（任何失败都不写入、原有质检记录不变）：

    1. 炉次 ID 未知（含超出 SQLite 有符号 64 位范围）=> 404；
    2. 未开炉 / 进行中（未结束）=> 422；
    3. 实测温度不是 1～20 个 0～200°C 有限数 => 422；
    4. 档数 ×5 不等于实际时长（实际结束 − 实际开始分钟）=> 422。

    校验通过：按归档冻结配方（材料参数与冻结版本）逐项核验连续保温与
    全程耐受（不核验升降温及末温），实测与逐项结论整份写入（每炉次
    一份，重复提交整份替换）。替换后本炉次对阻断的贡献按新结论增减，
    同版其他失败来源不受影响。
    """
    now = _now()
    with write_lock(), lock(), connect() as conn:
        with _tx(conn):
            if not (_SQLITE_INT_MIN <= draft_id <= _SQLITE_INT_MAX):
                raise NotFound(f"炉次草稿 {draft_id} 不存在。")
            run = _finished_run_row(conn, draft_id)

            try:
                temperatures = quality.validate_temperatures(raw_temperatures)
            except ValueError as exc:
                raise Unprocessable(str(exc)) from exc

            start = int(run["actual_start_minute"])
            end = int(run["actual_end_minute"])
            duration = end - start
            if len(temperatures) * quality.SLOT_MINUTES != duration:
                raise Unprocessable(
                    f"实测温度档数 ×5（{len(temperatures) * quality.SLOT_MINUTES}"
                    f" 分钟）须等于实际时长 {duration} 分钟（实际开始 {start}、"
                    f"实际结束 {end}）。"
                )

            # 归档冻结快照：引用顺序、冻结配方版本与冻结材料参数。已结束的
            # 炉次必为已归档（开炉要求其计划在列，计划只安排已归档炉次）
            archived = conn.execute(
                """SELECT d.recipe_ids AS recipe_ids,
                          d.recipe_versions_json AS recipe_versions_json,
                          a.materials_json AS materials_json
                   FROM drafts d
                   JOIN archives a ON a.draft_id = d.id
                   WHERE d.id=?""",
                (draft_id,),
            ).fetchone()
            if archived is None:
                raise Unprocessable(
                    f"炉次 {draft_id} 尚未归档，质检需要归档冻结的配方快照。"
                )
            items = quality.compute_items(
                temperatures,
                json.loads(archived["recipe_ids"]),
                json.loads(archived["recipe_versions_json"]),
                json.loads(archived["materials_json"]),
            )
            passed = not any(quality.item_failed(it) for it in items)

            conn.execute(
                """INSERT INTO run_quality
                       (draft_id, temperatures_json, items_json, passed,
                        created_at, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(draft_id) DO UPDATE SET
                       temperatures_json=excluded.temperatures_json,
                       items_json=excluded.items_json,
                       passed=excluded.passed,
                       updated_at=excluded.updated_at""",
                (
                    draft_id,
                    _dumps(temperatures),
                    _dumps(items),
                    1 if passed else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                _QUALITY_SELECT + " WHERE q.draft_id=?", (draft_id,)
            ).fetchone()
            return _quality_dict(row)


def get_run_quality(draft_id: int) -> dict:
    """读取一个炉次的质检记录：未知炉次 404；尚无质检记录 404。"""
    with lock(), connect() as conn:
        if not (_SQLITE_INT_MIN <= draft_id <= _SQLITE_INT_MAX):
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        draft = conn.execute(
            "SELECT id FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise NotFound(f"炉次草稿 {draft_id} 不存在。")
        row = conn.execute(
            _QUALITY_SELECT + " WHERE q.draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"炉次 {draft_id} 尚无质检记录。")
        return _quality_dict(row)


def _recipe_blocks(conn) -> list[dict]:
    """在已持锁的连接上由质检表推导当前全部阻断。

    任一未达标或超限项对应的（配方 ID, 冻结版本）被阻断，来源为对应
    炉次；同一版本的多个失败来源合并列出。按配方 ID、版本升序返回。
    """
    rows = conn.execute(
        "SELECT draft_id, items_json FROM run_quality WHERE passed=0"
    ).fetchall()
    sources: dict[tuple[int, int], set[int]] = {}
    for r in rows:
        for item in json.loads(r["items_json"]):
            if quality.item_failed(item):
                key = (item["recipeId"], item["version"])
                sources.setdefault(key, set()).add(r["draft_id"])
    return [
        {
            "recipeId": recipe_id,
            "version": version,
            "sourceDraftIds": sorted(draft_ids),
        }
        for (recipe_id, version), draft_ids in sorted(sources.items())
    ]


def list_recipe_blocks() -> dict:
    """读取当前全部同版工艺阻断（配方 ID、冻结版本与来源炉次）。只读。"""
    with lock(), connect() as conn:
        return {"blocks": _recipe_blocks(conn)}
