"""已结束炉次实测温度质检（按归档配方核验，不核验升降温及末温）。

实测温度为 1～20 个 0～200°C 的有限数（允许小数），依次代表实际开始后
的 5 分钟恒温；档数 ×5 须等于实际时长（实际结束 − 实际开始分钟）。

核验规则（对每条实测曲线、每个归档配方逐项进行）：

- 连续保温：每档 5 分钟恒温，档位温度落在 [保温下限, 保温上限] 内则
  连续进度 +1，达到所需档数即达标；离开区间时未达标进度归零。首次达标
  区间为第一段连续满足所需时长的档位（1-based，含端点），无则 None；
- 全程耐受：每一档都不得超过该材料耐受上限（达标后仍逐档检查），记录
  首次超限档号（1-based），无则 None；
- 不核验升降温（相邻档温差）与末温（是否回到 20°C）。

逐项结论按配方引用顺序给出：配方 ID、冻结版本、首次达标区间与首次
超限档号；任一配方未达标或超限即判该炉次实测不合格，对应的
（配方 ID, 冻结版本）成为阻断来源。
"""

from __future__ import annotations

import math
from typing import Any

from . import reuse
from .solver import Material

MIN_SLOTS = 1
MAX_SLOTS = 20
SLOT_MINUTES = 5
MIN_TEMP = 0.0
MAX_TEMP = 200.0


def validate_temperatures(raw: Any) -> list[Any]:
    """校验实测温度数组（1～20 个 0～200°C 有限数）；非法抛 ValueError（中文）。

    通过时返回原样数值列表（int / float 保持提交时的 JSON 形态）。
    """
    if not isinstance(raw, list):
        raise ValueError("实测温度必须是以数组形式给出的档位列表。")
    if not (MIN_SLOTS <= len(raw) <= MAX_SLOTS):
        raise ValueError(
            f"实测温度必须为 {MIN_SLOTS}～{MAX_SLOTS} 个档位"
            f"（每档 {SLOT_MINUTES} 分钟恒温）。"
        )
    for i, item in enumerate(raw, start=1):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"第 {i} 档实测温度必须是 0～200°C 的有限数。")
        value = float(item)
        if not math.isfinite(value) or not (MIN_TEMP <= value <= MAX_TEMP):
            raise ValueError(f"第 {i} 档实测温度 {item} 必须是 0～200°C 的有限数。")
    return list(raw)


def compute_items(
    temperatures: list[float],
    recipe_ids: list[int],
    recipe_versions: dict[str, int],
    materials: list[dict],
) -> list[dict]:
    """按引用顺序逐项核验实测曲线，返回各配方的质检结论。

    参数均取归档冻结快照：recipe_ids 为引用顺序，recipe_versions 为归档时
    冻结的配方版本（{recipeId: version}），materials 为冻结材料参数（与
    recipe_ids 同序）。每项含：配方 ID、冻结版本、首次达标区间（无则
    None）与首次超限档号（无则 None）。
    """
    items: list[dict] = []
    for rid, snap in zip(recipe_ids, materials):
        material = Material(
            name=snap["name"],
            hold_low=snap["holdLow"],
            hold_high=snap["holdHigh"],
            duration=snap["duration"],
            tolerance=snap["tolerance"],
        )
        check = reuse.check_item(temperatures, material)
        items.append(
            {
                "recipeId": rid,
                "version": recipe_versions[str(rid)],
                "window": check["window"],
                "toleranceBreachStep": check["toleranceBreachStep"],
            }
        )
    return items


def item_failed(item: dict) -> bool:
    """单项是否构成阻断来源：保温未达标（无首次达标区间）或存在超限档。"""
    return item["window"] is None or item["toleranceBreachStep"] is not None
