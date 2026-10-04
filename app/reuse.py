"""已归档炉次原曲线复用检查（只读核验，不重新求解）。

按炉次创建时的配方引用顺序，用各配方**当前**参数逐项核验归档冻结曲线：

- 连续保温沿用原求解规则：每档 5 分钟，档位温度落在
  [保温下限, 保温上限] 内则连续进度 +1，达到所需档数即达标；
  离开区间时未达标进度归零，已达标者保持达标；
- 首次达标区间：第一段连续满足所需时长的档位（1-based，含端点）；
- 耐受检查覆盖整条曲线（达标后仍继续逐档检查）：记录首次超过该
  材料耐受上限的档号（1-based），无超限为 None；
- 停用配方同样完成上述温度检查，仅其状态会影响整体可复用结论。

全部材料保温达标、无任何超限且配方均为可用时，整体才标为可复用。
"""

from __future__ import annotations

from .solver import Material


def check_item(temperatures: list[int], material: Material) -> dict:
    """用单个配方的当前参数核验冻结曲线，返回该项的温度检查结论。"""
    need = material.need_steps
    run = 0
    window: tuple[int, int] | None = None
    breach: int | None = None

    for idx, temp in enumerate(temperatures, start=1):
        # 耐受检查覆盖全曲线：即使已达标，仍逐档检查耐受上限
        if breach is None and temp > material.tolerance:
            breach = idx
        if window is not None:
            continue  # 首次达标区间已确定，保温结论不再变化
        if material.hold_low <= temp <= material.hold_high:
            run += 1
            if run >= need:
                window = (idx - need + 1, idx)
        else:
            run = 0  # 未达标时离开区间，连续进度归零

    return {
        "holdOk": window is not None,
        "window": (
            {"startStep": window[0], "endStep": window[1]} if window else None
        ),
        "toleranceBreachStep": breach,
    }
