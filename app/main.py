"""共炉保温编排台 —— FastAPI 后端。"""

from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import electricity, exports, quality, store
from .db import init_db
from .solver import Material, solve
from .solver import validate_material as solver_validate_material

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="共炉保温编排台", version="2.7.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
def _validation_handler(request: Request, exc: RequestValidationError):
    # 统一把 422 的 detail 转成中文字符串，便于页面直接展示
    zh_types = {
        "list_type": "必须是数组。",
        "int_type": "必须是整数。",
        "int_parsing": "必须是整数。",
        "int_from_float": "必须是整数。",
        "float_parsing": "必须是数值。",
        "missing": "缺少必填字段。",
        "too_short": "数量少于最小值。",
        "too_long": "数量超过最大值。",
        "greater_than_equal": "数值小于允许的最小值。",
        "less_than_equal": "数值超过允许的最大值。",
    }
    msgs: list[str] = []
    for err in exc.errors():
        etype = err.get("type", "")
        # 自定义校验器抛出的 ValueError 已是中文，直接使用
        if etype == "value_error":
            msgs.append(str(err.get("msg", "参数非法。")).removeprefix("Value error, "))
            continue
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
        msg = zh_types.get(etype.split(".", 1)[0], str(err.get("msg", "参数非法。")))
        msgs.append(f"{loc} {msg}".strip() if loc else msg)
    detail = "；".join(dict.fromkeys(msgs)) or "请求参数非法。"
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": detail}))


def _raise_store_error(exc: store.StoreError) -> None:
    text = getattr(exc, "detail", None) or str(exc)
    # 409 版本过期时附当前版本，方便调用方刷新后重试；质检阻断等冲突
    # 可携带附加字段（如被阻断配方与来源炉次）
    if isinstance(exc, store.Conflict) and (
        exc.current_version is not None or exc.extra
    ):
        payload: dict = {"detail": text}
        if exc.current_version is not None:
            payload["currentVersion"] = exc.current_version
        payload.update(exc.extra)
        raise HTTPException(status_code=exc.status_code, detail=payload)
    raise HTTPException(status_code=exc.status_code, detail=text)


class MaterialIn(BaseModel):
    name: str = Field(default="", max_length=20)
    hold_low: int = Field(alias="holdLow", ge=40, le=180)
    hold_high: int = Field(alias="holdHigh", ge=40, le=180)
    duration: int
    tolerance: int

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("hold_low", "hold_high", "tolerance")
    @classmethod
    def _tens(cls, v: int) -> int:
        if v % 10 != 0:
            raise ValueError("温度必须是 10 的整数倍。")
        return v

    @field_validator("duration")
    @classmethod
    def _duration(cls, v: int) -> int:
        if v not in (5, 10, 15):
            raise ValueError("连续保温时长必须为 5、10 或 15 分钟。")
        return v

    model_config = {"populate_by_name": True}


class ScheduleIn(BaseModel):
    materials: list[MaterialIn] = Field(min_length=2, max_length=3)
    # 可选：1～20 个已经执行的 5 分钟炉温档位（整十数）
    executed_temperatures: list[Any] | None = Field(
        default=None, alias="executedTemperatures"
    )

    @field_validator("executed_temperatures")
    @classmethod
    def _executed_basic(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("已执行温度必须是以数组形式给出的档位列表。")
        return v

    model_config = {"populate_by_name": True}


def _to_material(item: MaterialIn, index: int) -> Material:
    """构造求解用 Material，并复用求解器的材料字段校验。"""
    m = Material(
        name=item.name or f"材料{index + 1}",
        hold_low=item.hold_low,
        hold_high=item.hold_high,
        duration=item.duration,
        tolerance=item.tolerance,
    )
    try:
        solver_validate_material(m, index)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return m


@app.post("/api/schedule")
def schedule(payload: ScheduleIn):
    materials: list[Material] = [
        _to_material(item, i) for i, item in enumerate(payload.materials)
    ]

    executed = payload.executed_temperatures or None
    try:
        return solve(materials, executed=executed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------- 配方 ----------------


class RecipeIn(BaseModel):
    """配方沿用现有材料字段与校验（整份创建 / 整份修改同一结构）。"""

    name: str = Field(default="", max_length=20)
    hold_low: int = Field(alias="holdLow", ge=40, le=180)
    hold_high: int = Field(alias="holdHigh", ge=40, le=180)
    duration: int
    tolerance: int

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("hold_low", "hold_high", "tolerance")
    @classmethod
    def _tens(cls, v: int) -> int:
        if v % 10 != 0:
            raise ValueError("温度必须是 10 的整数倍。")
        return v

    @field_validator("duration")
    @classmethod
    def _duration(cls, v: int) -> int:
        if v not in (5, 10, 15):
            raise ValueError("连续保温时长必须为 5、10 或 15 分钟。")
        return v

    model_config = {"populate_by_name": True}


def _validate_recipe_fields(payload: RecipeIn, fallback: str) -> Material:
    m = Material(
        name=payload.name or fallback,
        hold_low=payload.hold_low,
        hold_high=payload.hold_high,
        duration=payload.duration,
        tolerance=payload.tolerance,
    )
    try:
        solver_validate_material(m, 0)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return m


@app.get("/api/recipes")
def get_recipes():
    return store.list_recipes()


@app.post("/api/recipes", status_code=201)
def create_recipe(payload: RecipeIn):
    _validate_recipe_fields(payload, "配方")
    try:
        return store.create_recipe(
            payload.name,
            payload.hold_low,
            payload.hold_high,
            payload.duration,
            payload.tolerance,
        )
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.get("/api/recipes/{recipe_id}")
def get_recipe(recipe_id: int):
    try:
        return store.get_recipe(recipe_id)
    except store.StoreError as exc:
        _raise_store_error(exc)


class RestoreIn(BaseModel):
    # 目标历史版本（要恢复成的那一版）
    version: int = Field(ge=1)
    # 查看历史时所看到的当前版本：提交时已变化则 409
    current_version: int = Field(alias="currentVersion", ge=1)

    model_config = {"populate_by_name": True}


@app.get("/api/recipes/{recipe_id}/versions")
def get_recipe_versions(recipe_id: int):
    # 按 version 升序列出历史：版本号、名称与全部材料参数；配方不存在 404
    try:
        return store.list_recipe_versions(recipe_id)
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.post("/api/recipes/{recipe_id}/restore")
def restore_recipe(recipe_id: int, payload: RestoreIn):
    # 恢复旧版为当前配方：携带目标历史 version 与查看时的当前 version；
    # 当前已变化 409，配方或历史版本不存在 404。恢复不做任何重试：
    # 普通修改先于恢复提交时，即使恢复已完成求解也返回 409。
    try:
        recipe, drafts = store.restore_recipe(
            recipe_id, payload.version, payload.current_version
        )
    except store.StoreError as exc:
        _raise_store_error(exc)
    return {"recipe": recipe, "recomputedDrafts": drafts}


@app.put("/api/recipes/{recipe_id}")
def update_recipe(recipe_id: int, payload: RecipeIn):
    # 先校验，非法修改不得进入写事务、不得改变任何数据
    _validate_recipe_fields(payload, "配方")
    try:
        recipe, drafts = store.update_recipe(
            recipe_id,
            payload.name,
            payload.hold_low,
            payload.hold_high,
            payload.duration,
            payload.tolerance,
        )
    except store.StoreError as exc:
        _raise_store_error(exc)
    return {"recipe": recipe, "recomputedDrafts": drafts}


@app.post("/api/recipes/{recipe_id}/disable")
def disable_recipe(recipe_id: int):
    # 停用：不再可被新炉次引用；不改参数与版本，不重算已有炉次
    try:
        return store.set_recipe_status(recipe_id, "disabled")
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.post("/api/recipes/{recipe_id}/enable")
def enable_recipe(recipe_id: int):
    # 恢复：之后可再次被新炉次引用
    try:
        return store.set_recipe_status(recipe_id, "active")
    except store.StoreError as exc:
        _raise_store_error(exc)


# ---------------- 炉次草稿 ----------------


class DraftIn(BaseModel):
    name: str = Field(default="", max_length=20)
    recipe_ids: list[int] = Field(alias="recipeIds")

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("recipe_ids")
    @classmethod
    def _recipe_ids_shape(cls, v: list[int]) -> list[int]:
        if len(v) < 2 or len(v) > 3:
            raise ValueError("炉次必须按顺序引用 2～3 个配方。")
        if len(set(v)) != len(v):
            raise ValueError("同一炉次引用的配方 ID 必须互不相同。")
        return v

    model_config = {"populate_by_name": True}


class ArchiveIn(BaseModel):
    # 归档必须携带所查看的草稿版本
    version: int = Field(ge=1)


class CancelIn(BaseModel):
    # 作废必须携带所查看的草稿版本
    version: int = Field(ge=1)


@app.get("/api/drafts")
def get_drafts():
    return store.list_drafts()


@app.post("/api/drafts", status_code=201)
def create_draft(payload: DraftIn):
    try:
        return store.create_draft(payload.name, payload.recipe_ids)
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: int):
    try:
        return store.get_draft(draft_id)
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.post("/api/drafts/{draft_id}/archive")
def archive_draft(draft_id: int, payload: ArchiveIn):
    try:
        return store.archive_draft(draft_id, payload.version)
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.post("/api/drafts/{draft_id}/cancel")
def cancel_draft(draft_id: int, payload: CancelIn):
    # 作废不再执行的炉次：保留记录（引用/参数快照/结果或无解原因），
    # 标记 cancelled、版本 +1 并记录作废时间；未知 404，版本过期 /
    # 已归档 / 已作废 409，失败不改变任何数据
    try:
        return store.cancel_draft(draft_id, payload.version)
    except store.StoreError as exc:
        _raise_store_error(exc)


# ---------------- 已归档炉次电费试算（只读，不保存） ----------------


def _nonneg_number(raw: Any, label: str) -> float:
    """校验单个「非负有限数」；布尔与非数值一律拒绝。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{label}必须是非负数值。")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label}必须是非负有限数。")
    return value


_ELECTRICITY_BODY_SCHEMA = {
    "type": "object",
    "required": ["startMinute", "prices", "powers"],
    "properties": {
        "startMinute": {
            "title": "开炉分钟",
            "description": "首档开始时刻距当天 00:00 的分钟数（0～1439 整数）",
            "type": "integer",
            "minimum": 0,
            "maximum": 1439,
        },
        "prices": {
            "title": "分时电价",
            "description": "按 00～23 时排列的 24 个电价（元/kWh），非负有限数",
            "type": "array",
            "items": {"type": "number", "minimum": 0},
            "minItems": 24,
            "maxItems": 24,
        },
        "powers": {
            "title": "逐档功率",
            "description": "与归档冻结曲线等长的逐档功率（kW），非负有限数",
            "type": "array",
            "items": {"type": "number", "minimum": 0},
            "minItems": 1,
        },
    },
}


def _validate_electricity_body(body: Any) -> tuple[int, list[float], list[float]]:
    """校验试算输入；非法时抛 422（中文 detail）。"""

    def fail(text: str) -> None:
        raise HTTPException(status_code=422, detail=text)

    if not isinstance(body, dict):
        fail("请求体必须是 JSON 对象。")

    if "startMinute" not in body:
        fail("缺少必填字段 startMinute（开炉分钟）。")
    if "prices" not in body:
        fail("缺少必填字段 prices（24 个分时电价）。")
    if "powers" not in body:
        fail("缺少必填字段 powers（逐档功率）。")

    raw_start = body["startMinute"]
    # 只接受真正的整数；拒绝布尔与 12.0 这类小数
    if isinstance(raw_start, bool) or not isinstance(raw_start, int):
        fail("开炉分钟必须是 0～1439 之间的整数。")
    if not 0 <= raw_start <= 1439:
        fail("开炉分钟必须在 0～1439 之间。")

    raw_prices = body["prices"]
    if not isinstance(raw_prices, list):
        fail("电价必须是以数组形式给出的 24 个数值。")
    if len(raw_prices) != 24:
        fail("电价必须恰好为 24 个（按 00～23 时排列）。")
    try:
        prices = [_nonneg_number(v, "电价") for v in raw_prices]
    except ValueError as exc:
        fail(str(exc))

    raw_powers = body["powers"]
    if not isinstance(raw_powers, list):
        fail("功率必须是以数组形式给出的逐档数值。")
    if not raw_powers:
        fail("功率数组不能为空，且必须与归档冻结曲线等长。")
    try:
        powers = [_nonneg_number(v, "功率") for v in raw_powers]
    except ValueError as exc:
        fail(str(exc))

    return raw_start, prices, powers


@app.post(
    "/api/drafts/{draft_id}/electricity",
    response_model=None,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _ELECTRICITY_BODY_SCHEMA}},
        }
    },
)
async def draft_electricity(draft_id: int, request: Request):
    # 第一步只判定炉次：未知炉次一律 404，哪怕请求体字段缺失 / 类型错误；
    # 存在但未归档（活动 / 无解草稿）一律 422。二者均为只读操作。
    try:
        curve = store.get_archived_curve(draft_id)
    except store.StoreError as exc:
        _raise_store_error(exc)

    # 炉次已确认存在且已归档，再解析并校验请求体（保持 404 优先于 422）
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8") or "null")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="请求体不是合法的 JSON。") from exc

    start_minute, prices, powers = _validate_electricity_body(body)

    temperatures = curve["temperatures"]
    if len(powers) != len(temperatures):
        raise HTTPException(
            status_code=422,
            detail=(
                f"功率数组长度（{len(powers)}）必须与归档冻结曲线档数"
                f"（{len(temperatures)}）一致。"
            ),
        )

    result = electricity.estimate(temperatures, start_minute, prices, powers)
    return {"draftId": draft_id, "version": curve["version"], **result}


# ---------------- 已归档炉次原曲线复用检查（只读，不重新求解） ----------------


@app.post("/api/drafts/{draft_id}/reuse-check")
def draft_reuse_check(draft_id: int):
    # 只读核验：未知炉次 404，未归档 422；不重新求解，不改配方/草稿/归档
    try:
        return store.check_archived_reuse(draft_id)
    except store.StoreError as exc:
        _raise_store_error(exc)


# ---------------- 已归档炉次工艺单导出（只读归档快照，不写库） ----------------


@app.get("/api/drafts/{draft_id}/sheet", response_class=HTMLResponse)
def draft_sheet(draft_id: int):
    # 只读导出：未知炉次 404，未归档 422。全部数据取归档快照，不读当前
    # 配方、不重新求解，也不写数据库；文档为样式内嵌、无外部资源/脚本的
    # 单份 UTF-8 HTML，作为附件下载，文件名含炉次 ID 与冻结版本。
    try:
        snapshot = store.get_archived_sheet(draft_id)
    except store.StoreError as exc:
        _raise_store_error(exc)

    html_text = exports.render_sheet(snapshot)
    ascii_name = f"draft-{snapshot['draft']['id']}-v{snapshot['version']}-sheet.html"
    utf8_name = f"炉次{snapshot['draft']['id']}-v{snapshot['version']}-工艺单.html"
    return HTMLResponse(
        content=html_text,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(utf8_name)}"
            )
        },
    )


# ---------------- 单台炉开炉计划（仅安排已归档炉次） ----------------


# 严格非负整数 Unix 分钟：拒绝 true/false 与 12.0 这类小数（pydantic v2
# 以 Field(strict=True) 收紧）
NonNegativeMinute = Annotated[int, Field(strict=True, ge=0)]
# 严格整数 ID（存在与否由 store 判 404；只拒绝布尔/小数/字符串）
StrictInteger = Annotated[int, Field(strict=True)]


class PlanChangeIn(BaseModel):
    # 炉次 ID（严格整数；ID 本身存在与否由 store 判 404）
    draft_id: StrictInteger = Field(alias="draftId")
    # 非负整数 Unix 开始分钟表示设置/覆盖；null 表示移除
    start_minute: NonNegativeMinute | None = Field(alias="startMinute")

    model_config = {"populate_by_name": True}


class PlanSubmitIn(BaseModel):
    # 批量查看时看到的计划版本：已变化则 409；空计划版本为 1
    version: StrictInteger = Field(ge=1)
    # 1～20 项改动，每项一个炉次；同批炉次 ID 不得重复
    changes: list[PlanChangeIn] = Field(min_length=1, max_length=20)

    @field_validator("changes")
    @classmethod
    def _unique_draft(cls, v: list[PlanChangeIn]) -> list[PlanChangeIn]:
        ids = [item.draft_id for item in v]
        if len(set(ids)) != len(ids):
            raise ValueError("同一批改动中的炉次 ID 不得重复。")
        return v


@app.get("/api/furnace-plan")
def get_furnace_plan():
    # 返回计划 version 与全部排段（炉次 ID、开始/结束分钟，按开始分钟、
    # ID 排序）；从未提交过的空计划版本为 1
    return store.get_furnace_plan()


@app.put("/api/furnace-plan")
def submit_furnace_plan(payload: PlanSubmitIn):
    # 整批查看后提交：未提及项保留；未知炉次 404，未归档 422，版本过期或
    # 排段重叠 409；任何失败都不改变计划。成功整批写入、version +1 并返回
    # 完整计划。时长取归档冻结曲线档数 × 5 分钟，不读当前配方或重新求解。
    # 进行中炉次的计划项不得改时或移除（409），已结束炉次不得重新排入
    # （409）。
    try:
        return store.submit_furnace_plan(
            payload.version,
            [(item.draft_id, item.start_minute) for item in payload.changes],
        )
    except store.StoreError as exc:
        _raise_store_error(exc)


# ---------------- 开炉 / 结束登记（仅登记，不控制设备） ----------------


class FurnaceStartIn(BaseModel):
    # 要开炉的炉次 ID：必须是当前计划首项（按开始分钟、炉次 ID 排序）
    draft_id: StrictInteger = Field(alias="draftId")
    # 提交计划时读到的计划版本：已变化则 409（空计划版本为 1）
    version: StrictInteger = Field(ge=1)
    # 实际开始分钟：SQLite 可存的非负整数 Unix 分钟；不得早于计划开始及
    # 上次实际结束（语义校验在 store 内，非法返回 422）
    actual_start_minute: NonNegativeMinute = Field(alias="actualStartMinute")

    model_config = {"populate_by_name": True}


class FurnaceFinishIn(BaseModel):
    # 要结束的进行中炉次 ID
    draft_id: StrictInteger = Field(alias="draftId")
    # 实际结束分钟：不得早于本次实际开始（语义校验在 store 内）
    actual_end_minute: NonNegativeMinute = Field(alias="actualEndMinute")

    model_config = {"populate_by_name": True}


@app.get("/api/furnace-runs")
def get_furnace_runs():
    # 按开炉顺序读取全部执行记录及当前占炉：current 为进行中记录或 null，
    # runs 含炉次 ID、计划起止、实际起止与状态；未结束时 actualEndMinute
    # 为 null
    return store.list_furnace_runs()


@app.post("/api/furnace-runs/start", status_code=201)
def start_furnace_run(payload: FurnaceStartIn):
    # 开炉登记（不控制设备）：只能启动计划首项；每炉次仅执行一次，同时仅
    # 一项进行中。成功保存 running 记录（冻结计划起止）、计划 version +1。
    # 旧版本 / 非首项 / 已有进行中 / 该炉次已执行过 / 归档版本被质检阻断
    # => 409（阻断附配方与来源炉次），未知炉次 => 404，非法分钟（早于计划
    # 开始或上次实际结束等）=> 422；失败不改状态。阻断与开炉按请求先后
    # 串行生效：阻断先提交则不能开炉，先开炉者仍可结束。
    try:
        return store.start_furnace_run(
            payload.draft_id, payload.version, payload.actual_start_minute
        )
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.post("/api/furnace-runs/finish")
def finish_furnace_run(payload: FurnaceFinishIn):
    # 结束登记（不控制设备）：保存结束记录、释放占炉、移除计划项与计划
    # version +1 在同一事务一起生效，不自动挪动后续计划。未知炉次 => 404，
    # 未开炉 / 已结束等状态冲突 => 409，结束分钟早于本次开始 => 422；
    # 失败不改状态。
    try:
        return store.finish_furnace_run(
            payload.draft_id, payload.actual_end_minute
        )
    except store.StoreError as exc:
        _raise_store_error(exc)


# ---------------- 已结束炉次实测温度质检与同版工艺阻断 ----------------


_QUALITY_BODY_SCHEMA = {
    "type": "object",
    "required": ["temperatures"],
    "properties": {
        "temperatures": {
            "title": "实测温度",
            "description": (
                "1～20 个 0～200°C 有限数，依次代表实际开始后的 5 分钟恒温；"
                "档数 ×5 须等于实际时长（实际结束 − 实际开始分钟）"
            ),
            "type": "array",
            "items": {"type": "number", "minimum": 0, "maximum": 200},
            "minItems": 1,
            "maxItems": 20,
        },
    },
}


@app.put(
    "/api/furnace-runs/{draft_id}/quality",
    response_model=None,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _QUALITY_BODY_SCHEMA}},
        }
    },
)
async def put_run_quality(draft_id: int, request: Request):
    # 提交或整份替换一个已结束炉次的实测温度与质检结论（不合格也保存）。
    # 未知炉次 => 404；未结束（未开炉 / 进行中）或输入非法（非 1～20 个
    # 0～200°C 有限数、档数 ×5 不等于实际时长）=> 422；失败不改数据。
    # 按归档冻结配方核验连续保温与全程耐受，不核验升降温及末温。
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8") or "null")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="请求体不是合法的 JSON。") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="请求体必须是 JSON 对象。")
    if "temperatures" not in body:
        raise HTTPException(
            status_code=422,
            detail="缺少必填字段 temperatures（1～20 个实测温度）。",
        )
    try:
        return store.submit_run_quality(draft_id, body["temperatures"])
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.get("/api/furnace-runs/{draft_id}/quality")
def get_run_quality(draft_id: int):
    # 读取一个炉次的质检记录：未知炉次 404；尚无质检记录 404
    try:
        return store.get_run_quality(draft_id)
    except store.StoreError as exc:
        _raise_store_error(exc)


@app.get("/api/recipe-blocks")
def get_recipe_blocks():
    # 读取当前全部同版工艺阻断：配方 ID、冻结版本与来源炉次（由质检记录
    # 即时推导；替换实测后按新结论增减，同版其他失败来源仍在则继续阻断）
    return store.list_recipe_blocks()


@app.get("/healthz")
def healthz():
    return {"ok": True}


# 静态页面（/）
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
