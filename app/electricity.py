"""已归档炉次电费试算（只读归档冻结曲线，不保存、不改动任何方案）。

计费约定：
- 每档持续 5 分钟，该档功率（kW）在 5 分钟内恒定；
- 日电价按 00～23 时给出（元/kWh），次日起原样重复；
- 档位跨小时（含跨午夜）时，按实际落入各小时的分钟数分摊电量与费用；
- 逐档费用各自四舍五入到两位小数；总费用先汇总**未舍入**的逐档费用，
  再四舍五入到两位（因此总费用可能与逐档舍入值之和相差 1 分）。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

STEP_MINUTES = 5
_HOUR_MINUTES = 60
_CENT = Decimal("0.01")


def _to_money(value: Decimal) -> float:
    """四舍五入到分（两位小数，ROUND_HALF_UP）。"""
    return float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def estimate(
    temperatures: list[int],
    start_minute: int,
    prices: list[float],
    powers: list[float],
) -> dict:
    """按冻结温度曲线与逐档功率试算电费。

    temperatures / powers 等长；prices 为 24 个小时电价；
    start_minute 为首档开始时刻距当天 00:00 的分钟数（0～1439）。
    """
    steps: list[dict] = []
    total_kwh = Decimal("0")
    total_cost = Decimal("0")
    cursor = start_minute  # 绝对分钟（可超过 1440，靠 % 24 取日重复电价）

    for index, (temperature, power) in enumerate(
        zip(temperatures, powers), start=1
    ):
        power_d = Decimal(str(power))
        kwh = power_d * Decimal(STEP_MINUTES) / Decimal(_HOUR_MINUTES)

        # 把这 5 分钟按小时边界（小时边界自然也是午夜边界）切成至多两段。
        # 注意用「当日内分钟」求边界，再对 24 小时取模：跨午夜后 hour 归 0，
        # 不得直接用绝对分钟做差（否则会错误地一直延伸到当日 24:00）。
        cost = Decimal("0")
        remaining = STEP_MINUTES
        absolute = cursor
        while remaining > 0:
            minute_of_day = absolute % (24 * _HOUR_MINUTES)
            hour = minute_of_day // _HOUR_MINUTES
            take = min(remaining, _HOUR_MINUTES - minute_of_day % _HOUR_MINUTES)
            slice_kwh = power_d * Decimal(take) / Decimal(_HOUR_MINUTES)
            cost += slice_kwh * Decimal(str(prices[hour]))
            absolute += take
            remaining -= take
        cursor += STEP_MINUTES

        total_kwh += kwh
        total_cost += cost  # 汇总的是未舍入值
        steps.append(
            {
                "index": index,
                "temperature": temperature,
                "minutes": STEP_MINUTES,
                "kwh": float(kwh),
                "cost": _to_money(cost),
            }
        )

    return {
        "startMinute": start_minute,
        "stepCount": len(steps),
        "totalMinutes": len(steps) * STEP_MINUTES,
        "steps": steps,
        "totalKwh": float(total_kwh),
        "totalCost": _to_money(total_cost),
    }
