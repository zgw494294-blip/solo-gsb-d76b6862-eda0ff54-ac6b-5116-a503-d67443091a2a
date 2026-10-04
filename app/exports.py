"""已归档炉次 → 可离线打开的单份 HTML 工艺单导出。

只吃归档快照（冻结的炉次字段 / 材料参数快照 / 冻结配方版本 / 冻结求解
结果 / 归档时间），不读当前配方、不重新求解；后续的配方修改、停用乃至
服务重启都不会改变文档内容。模块本身为纯函数与字符串拼装，不访问
数据库，也不产生任何写入。

文档内全部样式内嵌在 <style> 中，不引用任何外部资源、不含脚本；炉次 /
材料等文本一律经 html.escape 转义，只按原字符展示，不能被当作 HTML
执行。
"""

from __future__ import annotations

import html
from typing import Any

# 固定档长（与 solver.STEP_MINUTES 口径一致；归档快照中的每档恒为 5 分钟）
STEP_MINUTES = 5

_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 48px;
  background: #f4f6f8; color: #1f2933;
  font-family: "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC",
               "Hiragino Sans GB", "WenQuanYi Micro Hei", sans-serif;
  font-size: 15px; line-height: 1.6;
}
.sheet {
  max-width: 880px; margin: 0 auto; background: #fff;
  padding: 36px 40px 44px; border: 1px solid #d9dee3;
  border-radius: 10px; box-shadow: 0 2px 10px rgba(31, 41, 51, 0.08);
}
.sheet-head { border-bottom: 3px solid #1d6fa8; padding-bottom: 14px; margin-bottom: 20px; }
.sheet-head h1 { margin: 0 0 6px; font-size: 24px; color: #143f5f; }
.sheet-head .sub { margin: 0; color: #52606d; font-size: 13px; }
h2 {
  font-size: 17px; margin: 28px 0 10px; color: #143f5f;
  border-left: 4px solid #1d6fa8; padding-left: 10px;
}
table { border-collapse: collapse; width: 100%; margin: 8px 0 4px; }
th, td {
  border: 1px solid #cbd2d9; padding: 7px 10px; text-align: left;
  vertical-align: top; word-break: break-word;
}
th { background: #eef3f7; font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; white-space: nowrap; }
.meta th { width: 130px; background: #f5f8fa; }
.summary {
  display: flex; flex-wrap: wrap; gap: 10px 28px;
  margin: 10px 0 4px; padding: 12px 16px;
  background: #f5f8fa; border: 1px solid #d9e3ea; border-radius: 8px;
}
.summary .item b { display: inline-block; margin-left: 6px; font-size: 16px; color: #143f5f; }
.win { font-weight: 600; color: #143f5f; white-space: nowrap; }
.note { color: #52606d; font-size: 13px; margin: 6px 0 0; }
.temp-table td.temp { font-weight: 600; }
.sheet-foot {
  margin-top: 32px; padding-top: 12px; border-top: 1px solid #d9dee3;
  color: #7b8794; font-size: 12px;
}
@media print {
  body { background: #fff; padding: 0; }
  .sheet { border: none; box-shadow: none; max-width: none; padding: 0 8px; }
}
"""


def _esc(value: Any) -> str:
    """按原字符展示文本：转义全部 HTML 特殊字符，名称等不能被当作标记执行。"""
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_time_window(k: int) -> str:
    """第 k 档（1 起）对应的时间区间 [(k-1)×5, k×5) 分钟（左闭右开）。"""
    return f"[{(k - 1) * STEP_MINUTES}, {k * STEP_MINUTES}) 分钟"


def _fmt_window(w: dict | None) -> str:
    """首次达标区间：第 a～b 档（含起止档）。"""
    if not w:
        return "—"
    return f"第 {w['startStep']}～{w['endStep']} 档（含起止档）"


def render_sheet(snapshot: dict) -> str:
    """根据归档快照渲染单份离线 HTML 工艺单（UTF-8 字符串）。"""
    draft = snapshot["draft"]
    materials: list[dict] = snapshot["materials"]
    recipe_versions: dict = snapshot["recipeVersions"]
    recipe_ids: list = snapshot["recipeIds"]
    result: dict = snapshot["result"]
    archived_at = snapshot["archivedAt"]
    version = snapshot["version"]

    draft_name = draft.get("name") or ""
    display_name = draft_name or "（未命名）"
    temperatures: list = list(result["temperatures"])
    step_count = int(result.get("stepCount", len(temperatures)))
    total_minutes = int(result.get("totalMinutes", step_count * STEP_MINUTES))
    # 冻结结果的 windows 与冻结 materials 都按原引用顺序逐项对应；优先按下标
    # 对齐（即使两种材料同名也不会串），名称表仅作兜底。
    result_windows: list = result.get("windows", [])
    windows_by_name = {w.get("material"): w for w in result_windows}

    def window_for(i: int, name: str) -> dict | None:
        if i < len(result_windows):
            return result_windows[i]
        return windows_by_name.get(name)

    # —— 炉次信息 ——
    meta_rows = [
        ("炉次名称", _esc(display_name)),
        ("炉次 ID", _esc(draft.get("id"))),
        ("冻结版本", f"v{_esc(version)}"),
        ("归档时间", _esc(archived_at) + "（UTC）"),
    ]
    meta_html = "".join(
        f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in meta_rows
    )

    # —— 材料 / 配方（按原引用顺序；冻结配方版本与冻结材料名称）——
    mat_rows: list[str] = []
    for i, m in enumerate(materials):
        rid = recipe_ids[i] if i < len(recipe_ids) else ""
        frozen_ver = recipe_versions.get(str(rid), recipe_versions.get(rid, ""))
        w = window_for(i, m.get("name"))
        mat_rows.append(
            "<tr>"
            f"<td class='num'>{_esc(i + 1)}</td>"
            f"<td class='num'>{_esc(rid)}</td>"
            f"<td>{'v' + _esc(frozen_ver) if frozen_ver != '' else '—'}</td>"
            f"<td>{_esc(m.get('name', ''))}</td>"
            f"<td class='num'>{_esc(m.get('holdLow'))}–{_esc(m.get('holdHigh'))} °C</td>"
            f"<td class='num'>{_esc(m.get('duration'))} 分钟</td>"
            f"<td class='num'>{_esc(m.get('tolerance'))} °C</td>"
            f"<td class='win'>{_esc(_fmt_window(w))}</td>"
            "</tr>"
        )

    # —— 首次达标区间 + 总时长 ——
    win_items: list[str] = []
    for i, m in enumerate(materials):
        w = window_for(i, m.get("name"))
        win_items.append(
            "<span class='item'>"
            f"{_esc(m.get('name', ''))}：<b>{_esc(_fmt_window(w))}</b>"
            "</span>"
        )

    # —— 温度表（按档号 1..n 排列）——
    temp_rows: list[str] = []
    for k, t in enumerate(temperatures, start=1):
        temp_rows.append(
            "<tr>"
            f"<td class='num'>{k}</td>"
            f"<td>第 {k} 档 · {_esc(_fmt_time_window(k))}</td>"
            f"<td class='num temp'>{_esc(t)} °C</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>炉次 {_esc(draft.get('id'))} 工艺单（冻结 v{_esc(version)}）</title>
<style>{_STYLE}</style>
</head>
<body>
<article class="sheet">
  <header class="sheet-head">
    <h1>共炉保温工艺单</h1>
    <p class="sub">本文件为已归档炉次的冻结快照，全部数据取自归档时的材料参数与
    求解结果；不读取当前配方、不重新求解，后续配方修改、停用及服务重启均不改变本文件内容。</p>
  </header>

  <h2>炉次信息</h2>
  <table class="meta"><tbody>{meta_html}</tbody></table>

  <h2>材料与配方（按原引用顺序，均为归档冻结数据）</h2>
  <table>
    <thead><tr>
      <th class="num">引用顺序</th>
      <th class="num">配方 ID</th>
      <th class="num">冻结配方版本</th>
      <th>材料名称</th>
      <th class="num">保温下限–上限</th>
      <th class="num">保温时长</th>
      <th class="num">耐受上限</th>
      <th>首次达标区间</th>
    </tr></thead>
    <tbody>{''.join(mat_rows)}</tbody>
  </table>

  <h2>首次达标区间与总时长</h2>
  <div class="summary">
    {''.join(win_items)}
    <span class="item">总档数：<b>{step_count} 档</b></span>
    <span class="item">总时长：<b>{total_minutes} 分钟</b></span>
  </div>

  <h2>温度表（按档号排列，每档 5 分钟）</h2>
  <table class="temp-table">
    <thead><tr>
      <th class="num">档号</th>
      <th>时间区间</th>
      <th class="num">设定温度</th>
    </tr></thead>
    <tbody>{''.join(temp_rows)}</tbody>
  </table>
  <p class="note">档号从 1 起，第 k 档对应 [(k−1)×5, k×5) 分钟（左闭右开）。</p>

  <footer class="sheet-foot">
    共炉保温编排台 · 归档工艺单（离线 HTML，样式内嵌、无外部资源与脚本）·
    炉次 ID {_esc(draft.get('id'))} · 冻结 v{_esc(version)} · 归档时间 {_esc(archived_at)} UTC
  </footer>
</article>
</body>
</html>
"""
