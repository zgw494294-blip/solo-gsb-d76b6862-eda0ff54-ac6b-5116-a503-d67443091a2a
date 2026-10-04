"""共炉保温编排求解器。

规则：
- 炉温初态 20°C；每档持续 5 分钟，设定温度为 20..200 的整十数。
- 首档与初态、相邻档温差均不超过 20°C，末档必须回到 20°C。
- 任一档位温度均不得超过任何材料的耐受上限。
- 每种材料需要在其保温区间 [下限, 上限] 内连续保持给定的整档时长
  （5/10/15 分钟，即 1/2/3 档）。不同材料可以共用同一档位。
- 材料一旦达标即视为完成，后续不再要求保温。

求 20 档以内的最短完整曲线；等长时取温度序列字典序最小。

续炉判定：可给定 1～20 个「已执行」档位（executed）。已执行记录按
温度范围、整十度、相邻（含初态）≤20°C 温差、所有材料耐受上限逐档核验，
首个违规档即判记录非法，不予续排。合法记录逐档累计各材料连续保温进度：
离开区间则未达标进度归零，已达标者保持达标；记录中途回到 20°C 也允许
继续。随后以最后已执行温度为起点、已用档数计入 20 档上限，搜索最短且
回到 20°C 的后续曲线。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

STEP_MINUTES = 5          # 每档 5 分钟
INIT_TEMP = 20            # 炉温初态
MAX_TEMP = 200            # 最高设定温度
TEMP_STEP = 10            # 整十度
MAX_RAMP = 20             # 相邻档（含初态到首档）最大温差
MAX_STEPS = 20            # 档位上限
VALID_DURATIONS = (5, 10, 15)


@dataclass(frozen=True)
class Material:
    name: str
    hold_low: int    # 保温下限
    hold_high: int   # 保温上限
    duration: int    # 连续保温分钟数（5/10/15）
    tolerance: int   # 耐受上限

    @property
    def need_steps(self) -> int:
        return self.duration // STEP_MINUTES


def validate_material(m: Material, index: int = 0) -> None:
    """复核单个材料参数；非法时抛出 ValueError（中文信息）。"""
    tag = f"材料「{m.name}」" if m.name else f"第 {index + 1} 种材料"

    def ok_tens(v: int) -> bool:
        return isinstance(v, int) and 40 <= v <= 180 and v % TEMP_STEP == 0

    if not ok_tens(m.hold_low) or not ok_tens(m.hold_high):
        raise ValueError(f"{tag}的保温上、下限必须是 40~180°C 之间的整十数。")
    if m.hold_low > m.hold_high:
        raise ValueError(f"{tag}的保温下限不得高于上限。")
    if m.duration not in VALID_DURATIONS:
        raise ValueError(f"{tag}的保温时长必须为 5、10 或 15 分钟。")
    if (
        not isinstance(m.tolerance, int)
        or m.tolerance % TEMP_STEP != 0
        or not (m.hold_high <= m.tolerance <= MAX_TEMP)
    ):
        raise ValueError(
            f"{tag}的耐受上限必须是不低于保温上限（{m.hold_high}°C）、"
            f"至多 {MAX_TEMP}°C 的整十数。"
        )


def validate_materials(materials: list[Material]) -> None:
    """复核整组材料参数；非法时抛出 ValueError（中文信息）。"""
    if not (2 <= len(materials) <= 3):
        raise ValueError("材料数量必须为 2 或 3 种。")

    for i, m in enumerate(materials):
        validate_material(m, i)


def _precheck(materials: list[Material]) -> str | None:
    """简单的充分性预检：返回无解原因；通过则返回 None（不保证一定有解）。"""
    cap = min(m.tolerance for m in materials)

    for m in materials:
        # 区间完全被自身（或其他材料）的耐受上限封死
        if m.hold_low > cap:
            return (
                f"材料「{m.name}」的保温下限 {m.hold_low}°C 已高于所有材料耐受上限的"
                f"最小值 {cap}°C，任何可用档位都无法进入其保温区间。"
            )
        # 末档固定 20°C，可用档位最多 19 档；首尾升降温各需若干档
        need = m.need_steps
        # 必要条件（保守下界）：至少要升到保温下限、连续保温、再降回 20
        climb = (m.hold_low - INIT_TEMP + MAX_RAMP - 1) // MAX_RAMP
        min_steps = climb + need + climb  # 已含回到 20°C 的末档
        if min_steps > MAX_STEPS:
            return (
                f"仅完成材料「{m.name}」的保温就至少需要 {min_steps} 档"
                f"（升至不低于 {m.hold_low}°C、连续 {need} 档、再降回 20°C），"
                f"超过 {MAX_STEPS} 档上限。"
            )
    return None


def _advance(progress: tuple[int, ...], temp: int, materials: list[Material]):
    """在 temp 档位后，各材料的连续保温进度（档数）。"""
    nxt = list(progress)
    for i, m in enumerate(materials):
        if progress[i] < 0:
            nxt[i] = -1  # 已达标
        elif m.hold_low <= temp <= m.hold_high:
            nxt[i] = progress[i] + 1
            if nxt[i] >= m.need_steps:
                nxt[i] = -1  # 本档结束时达标
        else:
            nxt[i] = 0
    return tuple(nxt)


def replay_executed(
    executed: list[int], materials: list[Material]
) -> tuple[int, tuple[int, ...], list[int]]:
    """逐档核验已执行记录并模拟连续保温进度。

    返回 (最后一个已执行温度, 各材料当前进度, 规范化为整数的温度列表)；
    进度 -1 表示已达标。首个违规档即抛出 ValueError（1-based 档位编号），
    不给出任何续排。
    """
    if not (1 <= len(executed) <= MAX_STEPS):
        raise ValueError(f"已执行档位数量必须为 1～{MAX_STEPS} 个。")

    cap = min(m.tolerance for m in materials)
    prev = INIT_TEMP
    progress = tuple([0] * len(materials))
    normed: list[int] = []

    for i, raw in enumerate(executed, start=1):
        # 接受整数值（含 JSON 中数值为整数的 40.0）；拒绝布尔、非数值与小数
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"已执行记录第 {i} 档温度必须为整数（整十数）。")
        if not math.isfinite(raw) or float(raw) != int(raw):
            raise ValueError(f"已执行记录第 {i} 档温度 {raw}°C 必须为整数。")
        t = int(raw)
        if not (INIT_TEMP <= t <= MAX_TEMP):
            raise ValueError(
                f"已执行记录第 {i} 档温度 {t}°C 超出允许范围 "
                f"{INIT_TEMP}～{MAX_TEMP}°C。"
            )
        if t % TEMP_STEP != 0:
            raise ValueError(f"已执行记录第 {i} 档温度 {t}°C 不是整十数。")
        if abs(t - prev) > MAX_RAMP:
            raise ValueError(
                f"已执行记录第 {i} 档温度 {t}°C 与上一档 {prev}°C 温差 "
                f"{abs(t - prev)}°C，超过 {MAX_RAMP}°C 上限。"
            )
        if t > cap:
            raise ValueError(
                f"已执行记录第 {i} 档温度 {t}°C 超过所有材料耐受上限的最小值 "
                f"{cap}°C。"
            )
        progress = _advance(progress, t, materials)
        prev = t
        normed.append(t)

    return prev, progress, normed


def _bfs(
    materials: list[Material],
    start_temp: int = INIT_TEMP,
    start_progress: tuple[int, ...] | None = None,
    used_steps: int = 0,
) -> list[int] | None:
    """按档数升序、同档数按温度序列字典序升序搜索最短曲线。

    可从已执行记录的 (start_temp, start_progress, used_steps) 接续搜索，
    返回的仅为后续档位；总档数（已执行 + 后续）不得超过 MAX_STEPS。
    """
    n = len(materials)
    cap = min(m.tolerance for m in materials)
    # 候选温度：20..cap 的整十数（cap 本身为整十数）
    temps = list(range(INIT_TEMP, cap + 1, TEMP_STEP))

    if start_progress is None:
        start_progress = tuple([0] * n)
    # 状态 = (当前温度, 连续保温进度)
    start_state = (start_temp, start_progress)
    seen: set = {start_state}
    # 队列元素：(state, parent, chosen_temp, depth)，depth 为总档数
    queue = deque([(start_state, None, None, used_steps)])

    goal = None
    while queue:
        state, parent, chosen, depth = queue.popleft()
        cur_temp, progress = state

        # 末档必须为 20°C；必须至少有一个已执行档位
        if depth >= 1 and cur_temp == INIT_TEMP and all(p < 0 for p in progress):
            goal = (state, parent, chosen, depth)
            break

        if depth >= MAX_STEPS:
            continue

        for t in temps:  # 升序枚举 => 同层字典序
            if abs(t - cur_temp) > MAX_RAMP:
                continue
            if t == INIT_TEMP:
                # 20°C 只能作为末档：若尚有材料未达标，进入 20 后
                # 既无法保温（区间均 ≥40°C），也无法成为合法终点，
                # 这类死状态不入队；已全部达标时作为末档候选。
                if not all(p < 0 for p in progress):
                    continue
            new_progress = _advance(progress, t, materials)
            new_state = (t, new_progress)
            if new_state in seen:
                continue
            seen.add(new_state)
            queue.append((new_state, (state, parent, chosen), t, depth + 1))

    if goal is None:
        return None

    # 回溯温度序列
    seq: list[int] = []
    state, parent, chosen, _depth = goal
    while chosen is not None:
        seq.append(chosen)
        state, parent, chosen = parent
    seq.reverse()
    return seq


def _first_windows(seq: list[int], materials: list[Material]) -> list[dict]:
    """各材料在该曲线中首次达标的连续档位区间（1-based，含末档判定）。"""
    windows = []
    for m in materials:
        run = 0
        win = None
        for idx, t in enumerate(seq, start=1):
            if m.hold_low <= t <= m.hold_high:
                run += 1
                if run == m.need_steps and win is None:
                    win = (idx - m.need_steps + 1, idx)
                    break
            else:
                run = 0
        assert win is not None, f"材料 {m.name} 应在解中达标"
        s, e = win
        windows.append(
            {
                "startStep": s,
                "endStep": e,
                "steps": list(range(s, e + 1)),
                "temperatures": seq[s - 1 : e],
                "minutes": (e - s + 1) * STEP_MINUTES,
            }
        )
    return windows


def _build_result(
    materials: list[Material],
    seq: list[int],
    executed: list[int] | None = None,
) -> dict:
    """由完整曲线（已执行 + 后续）组装返回结构。"""
    executed = executed or []
    exec_count = len(executed)
    continuation = seq[exec_count:]
    windows = _first_windows(seq, materials)

    # 被两个及以上材料保温区间共用的档位
    used_by: dict[int, list[str]] = {}
    for m, w in zip(materials, windows):
        for s in w["steps"]:
            used_by.setdefault(s, []).append(m.name)
    shared_steps = sorted(s for s, names in used_by.items() if len(names) >= 2)

    steps = []
    prev = INIT_TEMP
    for i, t in enumerate(seq, start=1):
        is_executed = i <= exec_count
        steps.append(
            {
                "index": i,
                "temperature": t,
                "minutes": STEP_MINUTES,
                "deltaFromPrev": t - prev,
                "inWindow": sorted(used_by.get(i, [])),
                "shared": i in shared_steps,
                "executed": is_executed,
                "phase": "executed" if is_executed else "continuation",
            }
        )
        prev = t

    result = {
        "solvable": True,
        "temperatures": seq,
        "stepCount": len(seq),
        "totalMinutes": len(seq) * STEP_MINUTES,
        "toleranceCap": min(m.tolerance for m in materials),
        "steps": steps,
        "windows": [
            {"material": m.name, **w} for m, w in zip(materials, windows)
        ],
        "sharedSteps": shared_steps,
        "materials": [
            {
                "name": m.name,
                "holdLow": m.hold_low,
                "holdHigh": m.hold_high,
                "duration": m.duration,
                "tolerance": m.tolerance,
            }
            for m in materials
        ],
    }
    if exec_count > 0:
        result.update(
            {
                "hasExecuted": True,
                "executedTemperatures": list(executed),
                "continuationTemperatures": continuation,
                "executedCount": exec_count,
                "continuationCount": len(continuation),
                "continuationMinutes": len(continuation) * STEP_MINUTES,
                "alreadyComplete": len(continuation) == 0,
            }
        )
    else:
        result.update(
            {
                "hasExecuted": False,
                "executedTemperatures": [],
                "continuationTemperatures": continuation,
                "executedCount": 0,
                "continuationCount": len(continuation),
                "continuationMinutes": len(continuation) * STEP_MINUTES,
                "alreadyComplete": False,
            }
        )
    return result


def solve(materials: list[Material], executed: list[int] | None = None) -> dict:
    """求解入口。返回 {solvable: bool, ...}，永不抛参数以外的异常。"""
    validate_materials(materials)
    # 补默认名称
    materials = [
        Material(
            name=m.name.strip() or f"材料{i + 1}",
            hold_low=m.hold_low,
            hold_high=m.hold_high,
            duration=m.duration,
            tolerance=m.tolerance,
        )
        for i, m in enumerate(materials)
    ]

    if executed:
        # 已执行记录逐档核验：非法记录直接抛错（路由层转 422），不展示部分方案
        last_temp, progress, executed = replay_executed(list(executed), materials)
        cont = _bfs(
            materials,
            start_temp=last_temp,
            start_progress=progress,
            used_steps=len(executed),
        )
        if cont is None:
            return {
                "solvable": False,
                "reason": (
                    f"已执行 {len(executed)} 档后，在 {MAX_STEPS} 档总上限内找不到"
                    "满足剩余材料并回到 20°C 的后续曲线，无法续排。"
                ),
            }
        return _build_result(materials, list(executed) + cont, list(executed))

    reason = _precheck(materials)
    if reason is not None:
        return {"solvable": False, "reason": reason}

    seq = _bfs(materials)
    if seq is None:
        return {
            "solvable": False,
            "reason": f"在 {MAX_STEPS} 档以内找不到满足全部约束并回到 20°C 的完整曲线。",
        }

    return _build_result(materials, seq)
