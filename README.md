# 共炉保温编排台

本地单页应用：为 2～3 种需要在同一台炉子里分别完成连续保温的材料，
编排一条满足全部约束的**最短炉温设定曲线**；长度相同取温度序列
**字典序最小**的方案。

支持**执行记录续炉判定**：可输入 1～20 个已经执行的 5 分钟炉温档位，
逐档核验合法后，以最后已执行温度接续，在 20 档总上限内编排最短且回到
20°C 的后续曲线；首个违规档即拒绝续排。

- 后端：Python + FastAPI（求解核心见 `app/solver.py`，纯标准库 BFS；
  配方 / 炉次 / 归档 / 作废用 SQLite 持久化，见 `app/db.py`、`app/store.py`；
  已归档炉次只读电费试算见 `app/electricity.py`，原曲线复用只读核验见
  `app/reuse.py`，归档快照离线 HTML 工艺单导出见 `app/exports.py`；
  单台炉已归档炉次开炉计划见 `GET/PUT /api/furnace-plan`，计划首项的
  开炉 / 结束登记（不控制设备）见
  `GET /api/furnace-runs`、`POST /api/furnace-runs/start|finish`；
  已结束炉次实测温度质检与同版工艺阻断见 `app/quality.py` 与
  `PUT/GET /api/furnace-runs/{id}/quality`、`GET /api/recipe-blocks`）
- 前端：原生 HTML / CSS / JavaScript（`static/`，无任何前端构建与框架，
  页面分为「编排求解 / 材料配方 / 炉次草稿」三个标签页；已归档炉次详情中
  含原曲线复用检查、电费试算卡片与单份 HTML 工艺单下载）

## 一条命令启动

需要 Docker（含 Compose 插件）。

```bash
docker compose up --build -d
```

启动后浏览器访问：

- 页面：<http://localhost:8000/>
- 健康检查：<http://localhost:8000/healthz>
- 接口文档（FastAPI 自带）：<http://localhost:8000/docs>

停止：

```bash
docker compose down
```

端口在 `docker-compose.yml` 的 `ports` 中修改（宿主端口在冒号左侧）。

### 不用 Docker（可选）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 业务规则

### 材料（2～3 种）

- 保温下限 / 上限：40～180°C 的整十数，下限不高于上限；
- 连续保温时长：5、10 或 15 分钟（对应 1、2、3 个连续档位）；
- 耐受上限：不低于保温上限、至多 200°C 的整十数。

### 炉温曲线

- 炉温初态 20°C；
- 每档持续 5 分钟，设定温度为 20～200°C 的整十数；
- 首档与初态温差、相邻档温差均不超过 20°C；
- 末档必须回到 20°C；
- 所有档位温度都不得超过**任何**材料的耐受上限；
- 只有连续达到所需时长（5 分钟一档）的档位全部落在该材料的保温区间
  `[下限, 上限]` 内，该材料才算达标；
- 不同材料可以共用同一档位；材料一旦达标，后续档位对它不再有要求。

### 目标

- 总档数 ≤ 20 的前提下，求档数最少的完整曲线；
- 最短解有多条时，取温度序列字典序最小（例如先升 40°C 排在先升 60°C
  之前）；
- 有解：展示曲线图、各材料**首次达标**的连续档位区间、逐档明细表，
  以及被多种材料共用的档位；
- 无解：页面给出明确的中文原因，**不展示任何部分方案**；
- 修改任意输入后，旧结果立即清除。

### 执行记录续炉判定（可选）

- 在「已执行档位」中输入 **1～20 个**已经执行的 5 分钟炉温档位
  （20～200°C 的整十数，逗号/空格分隔）；HTTP 请求中对应
  `executedTemperatures` 字段（数组，可省略）。
- 从初态 20°C 起，按下列规则**逐档**核验已执行记录：
  - 温度必须是 20～200°C 的整十数；
  - 首档与初态、相邻档温差均不超过 20°C；
  - 每档都不得超过任何材料的耐受上限；
  - **一旦违规，立即指出首个违规档位（1-based）及原因，不给出任何续排**
    （HTTP 422，`detail` 为中文说明）。
- 合法记录逐档累计各材料的**连续**保温进度：
  - 档位落在保温区间内则进度 +1，达到所需档数即达标；
  - 档位离开区间时，**未达标者进度归零**，**已达标者保持达标**；
  - 记录中途回到 20°C 不影响合法性，后续仍可继续编排。
- 续排规则：
  - 以**最后一个已执行温度**为起点，已用档数**计入 20 档总上限**；
  - 求总档数最短、末档回到 20°C 的后续曲线；等长时取后续温度序列
    字典序最小；
  - 各材料**首次达标**区间按完整曲线（已执行 + 后续）统一编号，允许
    横跨已执行与后续档位；
  - 若已全部达标且末档已在 20°C，后续曲线为**空**；
  - 若总档数 20 以内无可行后续，返回 `solvable:false` 并明确提示，
    不展示部分曲线。

## 共享材料配方与炉次草稿（SQLite 持久化）

除了上面的即时编排，服务还支持把**材料参数保存为配方**，让多个**炉次草稿**
按顺序共用 2～3 个现有配方；核准的炉次方案可以**归档冻结**。数据保存在
SQLite（默认 `/data/planner.db`），Compose 以命名卷 `planner-data` 挂载，
容器重启后仍可读取。

### 材料配方 `/api/recipes`

- 字段与即时编排的材料**完全一致并复用同一套校验**：
  `name`（可留空，≤20 字）、`holdLow` / `holdHigh`（40～180°C 整十数，
  下限不高于上限）、`duration`（5/10/15 分钟）、`tolerance`（不低于保温
  上限、至多 200°C 的整十数）。
- 支持**创建、读取、整份修改**（PUT 为全量替换）。配方创建时版本为 1，
  每次成功修改版本 +1；**非法修改返回 422，不写入任何数据**（配方版本、
  参数历史、引用草稿均保持原样）。
- 每个配方带有**状态**：`active`（可用，默认）/ `disabled`（停用）。
  新建配方与旧数据库中已有的配方一律为可用；状态保存在 SQLite 中，
  重启后保留。`GET /api/recipes` 始终列出**全部**配方并附 `status`。
- **停用 / 恢复**：`POST /api/recipes/{id}/disable`、
  `POST /api/recipes/{id}/enable`（未知配方返回 404；重复停用 / 恢复
  幂等返回 200）。状态切换**不改变材料参数、不增加配方或草稿版本、
  不记参数历史、不重算任何炉次**；停用的配方仍可整份修改（保持停用），
  原有引用草稿照常重算与归档，已归档方案不受影响。恢复后可再次被新炉次
  引用。

#### 配方参数历史与旧版恢复

- **回看历史**：`GET /api/recipes/{id}/versions` 按 **version 升序**列出
  该配方的全部历史版本，每项含 `version`、`name` 与全部材料参数
  （`holdLow` / `holdHigh` / `duration` / `tolerance`）；配方不存在返回
  **404**。
- 历史**只追加**：新配方从版本 1 记起；**每次成功的整份修改（PUT）都追加
  新版本**；停用与恢复可用状态**不记**参数历史。升级本版本前的旧数据库
  只对每个配方**补记当前 version**（更早的参数无法追溯），**重复启动不
  重复补记**；历史随现有 SQLite 卷（`planner-data`）跨重启保留。
- **恢复旧版**：`POST /api/recipes/{id}/restore`，请求体携带目标历史
  版本 `version` 与查看历史时看到的当前版本 `currentVersion`。
  - 当前版本已变化（期间发生过普通修改或恢复）→ **409**，响应体附
    `currentVersion`，需重新读取后再恢复；
  - 配方不存在、或目标历史版本不存在 → **404**；
  - 成功 → **复制旧版的名称与全部材料参数**作为新的当前参数，当前
    **version +1**（恢复版本 1 也会生成新版本，例如当前 v3 恢复到 v1
    后变为 v4，v4 参数与 v1 相同）并**追加一条历史**，**保持当前可用
    状态**（停用中恢复仍为停用）；随后**按普通修改的原规则在同一事务里
    重算全部活动引用草稿**（版本各 +1、无解清除旧曲线并记录原因），
    **已归档炉次不改**。响应结构与 PUT 相同（`recipe` +
    `recomputedDrafts`）。
- **恢复的事务与并发语义**：与 PUT 一样先锁内取快照、**锁外求解**
  （求解期间三个 GET 仍立即返回上一整次已提交的完整状态，不等求解），
  再在**单条事务**里复核并提交——参数历史、配方与草稿**一起提交**，
  求解或写库异常均回滚、全部不生效。但恢复**只有一次机会、不重试**：
  若一次**普通修改先于恢复提交**（哪怕恢复已经完成求解），恢复一律返回
  **409**，**不得重试后覆盖新修改**；求解期间出现引用该配方的新炉次、
  草稿被归档或其他共享配方被修改，同样 409 失败且不留任何改动。

### 炉次草稿 `/api/drafts`

- 创建时按**顺序**给出 `recipeIds`：必须是 **2～3 个互不相同的现有配方
  ID**；引用一经创建**不可更改**（没有修改引用的接口）。
- 新建炉次只能引用**可用**状态的配方：引用任一**已停用**配方 → **422**
  （`detail` 指明配方 ID），**不保存炉次**；前端下拉同样只列出可用配方，
  页面尚未刷新时由服务端拦截。停用与新建并发时按某一先后顺序串行生效：
  停用先生效，则新建失败。
- 创建即**从 20°C 空记录求解**（复用原 BFS 规则），草稿**版本从 1 开始**；
  结果（有解时的完整曲线，或无解时的中文原因）与当时的材料参数快照一同
  保存。**无解草稿同样保存**，只是不保留任何曲线。
- **修改某配方时，所有引用它的“活动”草稿在同一事务里一起重算，版本各
  +1**：有解则写入新结果，无解则**清除旧曲线并记录原因**。已归档炉次不
  再受配方修改影响；**已作废炉次保留记录但同样不参与重算**（见下文
  「作废不再执行的炉次」）。
- **重算中的读取行为（不等待本次求解）**：`PUT` 耗时主要在重算求解；
  **无论走哪条重算分支**（常规的锁外求解，或连续竞争后的兜底路径），
  求解期间都**不持读锁、不写任何数据**；此时 `GET /api/recipes`、
  `GET /api/drafts`、`GET /api/drafts/{id}` 仍可立即读到**上一整次已提交
  的完整状态**（旧配方参数 + 各草稿旧参数快照与旧结果严格同版），
  **绝不会读到“新参数 + 旧结果”之类的未提交半成品**。待全部受影响草稿
  求解完成后，配方新参数、新版本与全部草稿的新参数快照、结果（或无解
  原因）、新版本才在**同一事务里一起生效**；`PUT` 仍等整体提交后才返回，
  响应结构（`recipe` + `recomputedDrafts`）、版本增量与「无解清除旧曲线」
  的行为均不变。求解或写库异常时本次修改整体回滚、全部不生效。
- **新建中的读取行为（不等待本次求解）**：`POST /api/drafts` 的求解同样
  **不持读锁、不写任何数据**；求解期间 `GET /api/recipes`、`GET /api/drafts`、
  `GET /api/drafts/{id}` 照常立即读到**上一整次已提交的完整状态**，新草稿
  在求解与保存整体完成前**不会出现在列表或详情中**（`POST` 仍等求解并
  保存后才返回 201，返回即含与同版参数快照对应的完整结果或中文无解原因）。
  若求解期间引用配方被并发**修改**，新建会作废旧结果、按最新参数重新求解
  后再保存（引用版本与参数、结果严格同版，不会错配）；若引用配方被并发
  **停用**且停用先生效，新建以 **422** 失败且**不保存任何草稿**。求解或
  写库异常同样不会留下新草稿。
- **重算与新建重叠时**：配方修改（`PUT`）提交前会复核引用它的活动草稿
  集合，重算期间建成的新草稿**必须纳入**同一事务一起重算（参数快照、
  结果与版本一并更新）；反过来，新建保存前会复核引用配方版本，若修改
  已先生效则按新参数重新求解。二者按某一先后顺序串行生效，任何读取都
  不会看到“新参数 + 旧结果”的混合态。
- 并发修改共享配方、并发创建引用炉次、停用/恢复与归档、作废，仍与修改按
  **某一先后顺序串行生效**：求解期间若有他次写操作先生效，本次重算结果会
  整体作废并按最新状态重新求解（不会漏算新草稿、不会覆盖他次修改、不会
  改写已归档冻结方案、不会回写已作废炉次）。
- **连续竞争时的读取行为**：若共享配方被连续修改、或不断出现引用该配方
  的新炉次，重算（或新建）的快照复核可能连续失效（默认 5 次）；此后转入
  兜底路径，但求解**仍然不持读锁**——上述三个 GET 在整个求解期间照常
  立即返回**上一整次已提交的完整状态**，不被阻塞，也读不到未提交参数。
  兜底路径只额外持有一把**写操作专用互斥锁**：其他写操作（修改 / 新建 /
  停用 / 归档）在它上面排队，使本次写操作的快照、求解与提交之间不再插入
  他次写操作，从而一次提交成功、严格落在他次修改之后；`PUT` 与
  `POST /api/drafts` 依旧等整体提交后才返回（单进程部署，无后台任务、
  无新服务）。
- 炉次读取（列表 / 详情）包含：`status`（`active` / `cancelled` /
  `archived`）、引用配方与引用时版本 `recipeVersions`、草稿 `version`、
  求解所用 `materials` 参数快照、`solvable`、`result`（完整求解结果）或
  `reason`（无解原因），以及 `archivedAt` / `cancelledAt`（未归档 / 未
  作废时为 `null`）。活动草稿的参数与结果一定是同一次求解写入的，
  **不会读到“新参数 + 旧结果”的混合态**；已作废草稿保留作废前的参数
  快照与结果或无解原因（作废不重算）。

### 归档核准方案 `POST /api/drafts/{id}/archive`

请求体 `{"version": N}` 携带**你所查看的草稿版本**：

- 版本与当前不一致（期间配方被改过）→ **409**，响应体附 `currentVersion`，
  请重新读取最新参数与结果后再归档；
- 草稿当前**无解** → **422**（中文原因），**不改变任何状态**；
- 已归档 → 409；归档与配方修改并发时，由服务端按**某一先后顺序串行生效**，
  后到的归档会收到 409，先到的归档冻结旧版本；
- 成功 → 状态变为 `archived`，**冻结当时的材料参数、草稿版本与完整求解
  结果**；此后配方再怎么改，只影响其余未归档炉次。

### 作废不再执行的炉次 `POST /api/drafts/{id}/cancel`

不再执行的炉次草稿可作废：**保留记录而非删除**。请求体 `{"version": N}`
携带**你所查看的草稿版本**：

- 炉次 ID 未知 → **404**；
- 版本与当前不一致（期间配方被改过、或已被作废过）→ **409**，响应体附
  `currentVersion`；
- 已归档 → **409**；已作废 → **409**（重复作废幂等拒绝）；
- **可行与无解草稿均可作废**；失败（404/409）不改变任何数据；
- 成功 → 状态变为 `cancelled`、**version +1**、记录作废时间
  `cancelledAt`；原引用 `recipeIds` / `recipeVersions`、`materials` 参数
  快照、`result` 或 `reason` **全部保留**；此后**不再允许归档**，也
  **不能恢复为活动草稿**（没有恢复接口），并不再被配方整份修改 / 历史
  恢复重算。

作废状态保存在 SQLite 中，重启后保留；旧库中的未归档炉次升级后一律
仍为 `active`。作废与重算、归档并发时按**某一先后顺序串行生效**：

- **作废先提交**：重算提交复核时发现活动草稿集合漂移，整批作废重算，
  **已经算出的结果不会回写到已作废炉次**；
- **重算先提交**：草稿版本已 +1，携带旧版本的作废收到 **409**；
- **归档先提交**：作废收到 **409**（反之，作废先提交则归档收到 409）。

### 前端：切换炉次时的请求编排（响应不串用）

「炉次草稿」详情区对查看 / 刷新 / 归档三类请求做了严格编排，避免慢响应
串用（接口字段、状态码与冻结规则均不变，全部为前端行为）：

- **只认当前选择的最新请求**：每次「查看」或「刷新」都生成新一代请求，
  详情、曲线、提示与归档按钮只接受最新一代的响应。先看甲再看乙时，晚
  返回的甲（无论成功、404/500 等失败还是网络异常）一律作废，**不会覆盖
  乙的详情或提示**。
- **等待期间仍可自由切换炉次**（包括“在甲等归档时切到乙、再切回甲”），
  也可再次点刷新（旧请求自动作废）。
- **查看或刷新一发起，旧详情立即失去归档资格**：在目标详情**成功读回
  之前**，归档按钮始终禁用（显示“详情加载中…”）；读取失败或网络异常时
  保持禁用并提示重试，杜绝把归档发到一份已经过期的详情上。
- **归档绑定点击那一刻的炉次 ID 与 `version`**：提交后即使立刻切到别的
  炉次，该归档请求仍照常发往服务端、照常生效；但它的成功 / 409 / 422 /
  网络异常**不会改写新炉次的详情、曲线、提示或按钮**（仅在成功时后台静默
  刷新列表状态）。新炉次读回后，完全按其自身状态（活动 / 已归档、版本）
  决定按钮。
- **切走再回到同一炉次**：回到甲（或对甲再次「刷新」）会生成新一代详情
  请求；此前那笔归档**晚到的回包对甲同样作废**——既不会清空 / 覆盖新读回
  的详情与曲线，也不会插入旧的成功或 409/422 提示，更不会代替用户再触发
  一次详情 GET。它只释放在途状态、按当前有效详情恢复归档按钮。若旧归档
  已成功，列表状态在后台静默同步；甲的最新状态以新 GET 读回的内容为准。
- **留在原炉次、期间没有重新查看或刷新**：归档成功显示冻结方案与成功
  提示，按钮转为“已归档”；409（版本过期 / 已归档）或 422（当前无解）
  都会**重新读取同一炉次**，并把中文原因**保留在页面上**（409 同时提示
  当前版本号）；网络失败不改详情，提示可重试。
- **同一炉次归档请求未结束前不得重复提交**（归档中按钮禁用并按炉次 ID
  去重，切走再回来也不解锁）；不同炉次的归档互不影响。归档按钮始终只按
  **当前有效详情**恢复：详情加载中、读取失败（含网络异常）或已归档时
  一律禁用。

## 求解算法

在档位最多 20、温度只有 19 个整十值的规模下，直接做广度优先搜索：

- 状态 = `(当前温度, 各材料连续保温进度)`；进度为“当前已连续处于其区间
  内的档数”，达标后记为完成态；
- 从零编排：从初态 `(20, 全 0)` 出发；带执行记录时：先逐档核验并重放
  记录得到 `(最后已执行温度, 当前进度)`，再以该状态为起点；
- 按温度**升序**扩展下一档（温差 ≤ 20、不超过耐受上限）。BFS 保证
  档数最少，同层按温度升序入队保证温度序列字典序最小；
- 目标状态：当前温度回到 20°C 且所有材料均已完成；
- 扩展深度（已执行档数 + 后续档数）超过 20 即停止；搜不到目标即为无解。
- 另含一个保守的无解预检（如保温下限高于所有耐受上限的最小值），
  仅在无执行记录时使用，用于给出更具体的原因；预检通过不代表必然有解，
  最终以 BFS 为准。

## API

`POST /api/schedule`

请求体字段：

- `materials`（必填，2～3 项）：每种材料的名称与温度参数；
- `executedTemperatures`（可选）：1～20 个已执行档位温度的整数数组；
  省略或传 `[]` 表示从零编排。

其余接口（配方与炉次管理，均返回 JSON）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/recipes` | 列出全部配方（含 `status`：`active` / `disabled`） |
| POST | `/api/recipes` | 创建配方（201，版本从 1 开始，默认可用） |
| GET | `/api/recipes/{id}` | 读取单个配方（含状态） |
| GET | `/api/recipes/{id}/versions` | 按 version 升序列出参数历史（版本、名称与全部材料参数；未知配方 404） |
| POST | `/api/recipes/{id}/restore` | 携带 `version`（目标历史版）与 `currentVersion`（查看时当前版）恢复旧版：当前已变化 409 / 配方或历史版本不存在 404；原子重算活动引用草稿，不改归档 |
| PUT | `/api/recipes/{id}` | 整份修改配方；原子重算引用它的活动草稿（版本 +1）；停用配方也可修改并保持停用 |
| POST | `/api/recipes/{id}/disable` | 停用配方（200；未知 404；不改参数/版本，不重算草稿） |
| POST | `/api/recipes/{id}/enable` | 恢复配方为可用（200；未知 404） |
| GET | `/api/drafts` | 列出全部炉次（含状态/参数/版本/结果或无解原因） |
| POST | `/api/drafts` | 按 2～3 个不同现有配方 ID 创建炉次并从空记录求解（201；引用已停用配方 422） |
| GET | `/api/drafts/{id}` | 读取炉次详情（归档后返回冻结快照；作废后返回保留快照与作废时间） |
| POST | `/api/drafts/{id}/archive` | 携带查看版本归档（过期/已作废 409、无解 422） |
| POST | `/api/drafts/{id}/cancel` | 携带查看版本作废（保留记录；未知 404；过期/已归档/已作废 409） |
| POST | `/api/drafts/{id}/reuse-check` | 已归档炉次原曲线复用检查（只读当前配方，不重新求解；未知 404 / 未归档 422） |
| POST | `/api/drafts/{id}/electricity` | 已归档炉次电费试算（只读冻结曲线，不保存；未知 404 / 未归档或参数非法 422） |
| GET | `/api/drafts/{id}/sheet` | 下载已归档炉次的单份离线 HTML 工艺单（UTF-8 HTML 附件；未知 404 / 未归档 422；只读归档快照，不写库） |
| GET | `/api/furnace-plan` | 读取单台炉开炉计划（version + 排段，按开始分钟、ID 排序；空计划版本 1） |
| PUT | `/api/furnace-plan` | 批量提交 1～20 项改动（非负整数 Unix 开始分钟，null 移除，未提及保留；同批可交换；版本过期/重叠/改时移除进行中项/重排已结束项 409、未知炉次 404、未归档/非法输入 422；成功整批写入、版本 +1） |
| GET | `/api/furnace-runs` | 按开炉顺序读取执行记录与当前占炉（`current` 进行中记录或 null，`runs` 含炉次 ID、计划起止、实际起止、状态；未结束结束分钟为 null） |
| POST | `/api/furnace-runs/start` | 开炉登记（不控制设备）：提交炉次 ID、所读计划 version 与实际开始分钟，只能启动计划首项；201；旧 version / 非首项 / 已有进行中 / 每炉次重复执行 / 归档版本被质检阻断 409（阻断附配方与来源炉次）、未知炉次 404、非法分钟 422；成功冻结计划起止、计划 version +1 |
| POST | `/api/furnace-runs/finish` | 结束登记：提交进行中炉次 ID 与结束分钟；保存记录、释放占炉、移除计划项与计划 version +1 同一事务生效，不挪动后续计划；状态冲突 409、未知炉次 404、结束早于开始 422 |
| PUT | `/api/furnace-runs/{id}/quality` | 提交或整份替换已结束炉次的实测温度与质检结论（每炉次一份，不合格也保存；未知炉次 404，未结束或输入非法 422，失败不改数据） |
| GET | `/api/furnace-runs/{id}/quality` | 读取该炉次的质检记录（未知炉次 / 尚无质检记录 404） |
| GET | `/api/recipe-blocks` | 读取当前全部同版工艺阻断（配方 ID、冻结版本、来源炉次；由质检记录即时推导） |

### 从零编排（可直接复制执行）

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ]
  }'
```

返回（HTTP 200，节选）：

```json
{
  "solvable": true,
  "hasExecuted": false,
  "temperatures": [40, 60, 80, 60, 40, 20],
  "executedTemperatures": [],
  "continuationTemperatures": [40, 60, 80, 60, 40, 20],
  "executedCount": 0,
  "continuationCount": 6,
  "stepCount": 6,
  "totalMinutes": 30,
  "alreadyComplete": false,
  "toleranceCap": 150,
  "windows": [
    {"material": "材料A", "startStep": 2, "endStep": 3, "minutes": 10},
    {"material": "材料B", "startStep": 3, "endStep": 3, "minutes": 5}
  ]
}
```

### 带执行记录续炉（可直接复制执行）

已执行 `[40, 60, 80]` 三档：材料 A 已连续保温 2 档（区间 60～100），
材料 B 已保温 1 档（区间 80～120）。续排只需降温回 20°C：

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 60, 80]
  }'
```

返回（HTTP 200，节选）：

```json
{
  "solvable": true,
  "hasExecuted": true,
  "temperatures": [40, 60, 80, 60, 40, 20],
  "executedTemperatures": [40, 60, 80],
  "continuationTemperatures": [60, 40, 20],
  "executedCount": 3,
  "continuationCount": 3,
  "stepCount": 6,
  "totalMinutes": 30,
  "alreadyComplete": false,
  "steps": [
    {"index": 1, "temperature": 40, "deltaFromPrev": 20, "executed": true,  "phase": "executed"},
    {"index": 4, "temperature": 60, "deltaFromPrev": -20, "executed": false, "phase": "continuation"}
  ]
}
```

字段说明（续炉相关）：

| 字段 | 含义 |
| --- | --- |
| `hasExecuted` | 是否带已执行记录 |
| `executedTemperatures` / `executedCount` | 已执行档位温度与档数 |
| `continuationTemperatures` / `continuationCount` | 后续曲线温度与档数（可为空数组） |
| `alreadyComplete` | 已执行记录结束时是否已全部达标且回到 20°C（此时后续为空） |
| `steps[].executed` / `steps[].phase` | 每档属于 `executed`（已执行）还是 `continuation`（后续） |
| `windows` | 各材料首次达标区间，按**完整曲线**统一编号（允许横跨两段） |

### 已全部达标且回到 20°C —— 后续为空

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 60, 80, 100, 100, 120, 100, 80, 60, 40, 20]
  }'
```

返回（节选）：`"solvable": true, "alreadyComplete": true,
"continuationTemperatures": [], "continuationCount": 0`。

### 执行记录违规 —— HTTP 422，指出首个违规档

第 2 档由 40°C 跳到 80°C（温差 40°C > 20°C）：

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 80]
  }'
```

```json
{"detail": "已执行记录第 2 档温度 80°C 与上一档 40°C 温差 40°C，超过 20°C 上限。"}
```

超过耐受上限（所有材料耐受上限最小值为 150°C，第 7 档 160°C 违规）：

```bash
curl -sS -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d '{
    "materials": [
      {"name": "材料A", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
      {"name": "材料B", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
    ],
    "executedTemperatures": [40, 60, 80, 100, 120, 140, 160]
  }'
# {"detail":"已执行记录第 7 档温度 160°C 超过所有材料耐受上限的最小值 150°C。"}
```

### 无可行后续 —— HTTP 200，solvable=false

已执行档数 + 最短必要后续超过 20 档时（HTTP 200，便于页面区分“无解”与
“参数错误”）：

```json
{"solvable": false, "reason": "已执行 20 档后，在 20 档总上限内找不到满足剩余材料并回到 20°C 的后续曲线，无法续排。"}
```

参数非法（材料参数错误、已执行记录为空/超过 20 个/非整十数/越界等）
时返回 HTTP 422，`detail` 为中文错误说明。响应中**不会**附带任何部分
曲线。

### 配方与炉次草稿（可直接复制执行）

```bash
# 1) 创建两个材料配方（版本均从 1 开始，默认可用）
curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"钢甲","holdLow":60,"holdHigh":100,"duration":10,"tolerance":150}'
# {"id":1,"name":"钢甲","holdLow":60,...,"status":"active","version":1,"createdAt":"...","updatedAt":"..."}

curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"铜乙","holdLow":80,"holdHigh":120,"duration":5,"tolerance":160}'

# 2) 创建炉次草稿：按顺序引用 2 个不同现有配方，从 20°C 空记录求解
curl -sS -X POST http://localhost:8000/api/drafts \
  -H 'Content-Type: application/json' \
  -d '{"name":"十月首炉","recipeIds":[1,2]}'
```

返回（HTTP 201，节选）：

```json
{
  "id": 1,
  "name": "十月首炉",
  "status": "active",
  "recipeIds": [1, 2],
  "recipeVersions": {"1": 1, "2": 1},
  "version": 1,
  "materials": [
    {"name": "钢甲", "holdLow": 60, "holdHigh": 100, "duration": 10, "tolerance": 150},
    {"name": "铜乙", "holdLow": 80, "holdHigh": 120, "duration": 5,  "tolerance": 160}
  ],
  "solvable": true,
  "result": { "solvable": true, "temperatures": [40, 60, 80, 60, 40, 20],
              "stepCount": 6, "totalMinutes": 30, "steps": [ ... ],
              "windows": [ ... ], "sharedSteps": [ ... ], "materials": [ ... ] },
  "reason": null,
  "createdAt": "...", "updatedAt": "...", "archivedAt": null
}
```

### 修改配方 → 引用草稿一起重算（版本 +1）

```bash
curl -sS -X PUT http://localhost:8000/api/recipes/1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"钢甲","holdLow":70,"holdHigh":100,"duration":10,"tolerance":150}'
```

```json
{
  "recipe": { "id": 1, "holdLow": 70, "version": 2, "...": "..." },
  "recomputedDrafts": [
    { "id": 1, "status": "active", "version": 2, "solvable": true,
      "materials": [ { "holdLow": 70, "...": "..." } ],
      "result": { "...": "与 materials 同版的完整结果" } }
  ]
}
```

非法修改（如保温下限高于上限、耐受上限低于保温上限、非整十数等）返回
**422 且不改变任何数据**：配方版本不增加，引用草稿也不会被重算。
新参数导致草稿无解时，草稿仍保存：`version` 增加、`solvable:false`、
`result:null`、`reason` 为中文原因。

### 回看参数历史并恢复旧版（可直接复制执行）

接着上例（配方 1 已 PUT 过一次，当前 `version:2`，另有一个引用它的炉次
草稿 1）：

```bash
# 按 version 升序查看历史：创建 v1 与每次成功整份修改都会列出；
# 停用 / 恢复可用状态不出现在历史里
curl -sS http://localhost:8000/api/recipes/1/versions
```

```json
[
  {"version": 1, "name": "钢甲", "holdLow": 60, "holdHigh": 100,
   "duration": 10, "tolerance": 150},
  {"version": 2, "name": "钢甲", "holdLow": 70, "holdHigh": 100,
   "duration": 10, "tolerance": 150}
]
```

```bash
# 恢复为历史 v1：version 填目标历史版本，currentVersion 填查看时的当前版本
curl -sS -X POST http://localhost:8000/api/recipes/1/restore \
  -H 'Content-Type: application/json' \
  -d '{"version":1,"currentVersion":2}'
```

返回（HTTP 200，节选）：恢复复制 v1 的名称与参数，当前版本 +1 变为 **v3**，
并按原规则重算全部**活动**引用草稿（草稿版本同步 +1），已归档炉次不变；
配方若正处于停用状态，恢复后**仍保持停用**。

```json
{
  "recipe": { "id": 1, "name": "钢甲", "holdLow": 60, "holdHigh": 100,
              "duration": 10, "tolerance": 150, "status": "active",
              "version": 3, "...": "..." },
  "recomputedDrafts": [
    { "id": 1, "status": "active", "version": 3, "solvable": true,
      "materials": [ { "holdLow": 60, "...": "..." } ],
      "result": { "...": "与 materials 同版的完整结果" } }
  ]
}
```

再次查看历史会看到恢复也追加了一条（v3 与 v1 参数相同）：

```bash
curl -sS http://localhost:8000/api/recipes/1/versions
# [{"version":1,...,"holdLow":60,...},
#  {"version":2,...,"holdLow":70,...},
#  {"version":3,...,"holdLow":60,...}]
```

冲突与不存在的情况：

```bash
# 期间配方又被改过（当前已是 v4），仍带 currentVersion=3 恢复 → 409
curl -sS -i -X POST http://localhost:8000/api/recipes/1/restore \
  -H 'Content-Type: application/json' \
  -d '{"version":1,"currentVersion":3}'
# HTTP/1.1 409 Conflict
# {"detail":{"detail":"配方已过期：你查看的是版本 3，当前为版本 4，请重新读取后再恢复。",
#            "currentVersion":4}}

# 目标历史版本不存在 → 404
curl -sS -i -X POST http://localhost:8000/api/recipes/1/restore \
  -H 'Content-Type: application/json' \
  -d '{"version":99,"currentVersion":4}'
# HTTP/1.1 404 Not Found
# {"detail":"配方 1 的历史版本 99 不存在。"}

# 配方不存在 → 404（查看历史与恢复一致）
curl -sS -i http://localhost:8000/api/recipes/999/versions
# HTTP/1.1 404 Not Found
# {"detail":"配方 999 不存在。"}
```

恢复与普通修改一样在**锁外求解、单事务整体提交**：求解期间 `GET` 三个
读取接口仍立即返回上一整次已提交状态；历史、配方与草稿一起提交，求解或
写库异常全部回滚。若一次普通修改**先于恢复提交**，即使恢复已完成求解也
返回 **409**，服务端**不会重试**，新修改不会被覆盖——重新读取历史与
`currentVersion` 后再发起恢复即可。

### 停用配方 → 拒绝引用 → 恢复

```bash
# 停用配方 1：只切换状态，材料参数、配方/草稿版本不变，不重算任何炉次
curl -sS -X POST http://localhost:8000/api/recipes/1/disable
# {"id":1,"name":"钢甲",...,"status":"disabled","version":2,...}

# 停用期间新建炉次引用它 → 422，指明配方 ID，不保存炉次
curl -sS -X POST http://localhost:8000/api/drafts \
  -H 'Content-Type: application/json' \
  -d '{"name":"十月二炉","recipeIds":[1,2]}'
# 422：{"detail":"配方 ID 1 已停用，不能用于新建炉次。"}

# GET /api/recipes 仍列出全部配方并附状态
curl -sS http://localhost:8000/api/recipes
# [{"id":1,...,"status":"disabled",...},{"id":2,...,"status":"active",...}]

# 恢复为可用，之后可再次被新炉次引用
curl -sS -X POST http://localhost:8000/api/recipes/1/enable
# {"id":1,"name":"钢甲",...,"status":"active","version":2,...}

# 未知配方的状态操作 → 404
curl -sS -X POST http://localhost:8000/api/recipes/999/disable
# 404：{"detail":"配方 999 不存在。"}
```

停用不改变已有炉次：引用它的活动草稿在配方修改时照常重算、可正常归档，
已归档方案保持冻结；停用的配方本身也仍可整份修改（PUT）并保持停用。

### 归档：携带所查看的版本

```bash
# 正常归档（当前查看的就是 v2）
curl -sS -X POST http://localhost:8000/api/drafts/1/archive \
  -H 'Content-Type: application/json' -d '{"version":2}'
# 200，节选：
# {"id":1,"status":"archived","version":2,
#  "materials":[...冻结快照...],"result":{...冻结完整结果...},
#  "archivedAt":"2026-10-01T08:00:00Z", ...}

# 期间配方被改过（草稿已到 v3），仍带 v2 归档 → 409
curl -sS -X POST http://localhost:8000/api/drafts/1/archive \
  -H 'Content-Type: application/json' -d '{"version":2}'
# 409：{"detail":{"detail":"炉次草稿已过期：你查看的是版本 2，当前为版本 3，请重新读取后再归档。",
#                 "currentVersion":3}}

# 当前无解时归档 → 422，状态不变
# 422：{"detail":"材料「钢甲」的保温下限 170°C 已高于所有材料耐受上限的最小值 160°C，..."}
```

归档成功后再次 `PUT /api/recipes/1` 只影响其他**未归档**炉次；
`GET /api/drafts/1` 永远返回归档时冻结的版本、参数与结果。

### 作废：携带所查看的版本（可直接复制执行）

```bash
# 正常作废（当前查看的就是 v1）：状态变为 cancelled、版本 +1、记录作废时间，
# 引用、参数快照与求解结果全部保留
curl -sS -X POST http://localhost:8000/api/drafts/1/cancel \
  -H 'Content-Type: application/json' -d '{"version":1}'
# 200，节选：
# {"id":1,"status":"cancelled","version":2,
#  "recipeIds":[1,2],"recipeVersions":{"1":1,"2":1},
#  "materials":[...作废前快照...],"result":{...原完整结果...},
#  "cancelledAt":"2026-10-04T08:00:00Z","archivedAt":null, ...}

# 无解草稿同样可作废（reason 保留，result 仍为 null）

# 期间配方被改过（草稿已到 v3），仍带 v2 作废 → 409
curl -sS -X POST http://localhost:8000/api/drafts/1/cancel \
  -H 'Content-Type: application/json' -d '{"version":2}'
# 409：{"detail":{"detail":"炉次草稿已过期：你查看的是版本 2，当前为版本 3，请重新读取后再作废。",
#                 "currentVersion":3}}

# 重复作废 / 已归档后作废 → 409；未知炉次 → 404；均不改变任何数据
curl -sS -i -X POST http://localhost:8000/api/drafts/1/cancel \
  -H 'Content-Type: application/json' -d '{"version":2}'
# HTTP/1.1 409 Conflict
# {"detail":{"detail":"该炉次已作废，不能重复作废。","currentVersion":2}}
curl -sS -i -X POST http://localhost:8000/api/drafts/999/cancel \
  -H 'Content-Type: application/json' -d '{"version":1}'
# HTTP/1.1 404 Not Found
# {"detail":"炉次草稿 999 不存在。"}
```

作废后该炉次**不再允许归档**（归档返回 409），也不再被
`PUT /api/recipes/{id}` 或 `POST /api/recipes/{id}/restore` 重算——
重算只覆盖其余活动草稿；`GET /api/drafts` 与 `GET /api/drafts/1` 继续
返回该炉次（`status:"cancelled"` 与 `cancelledAt`），重启后状态保留。
作废与重算并发时按先后顺序生效：作废先提交，则重算不会把新结果回写到
它；重算先提交（版本已 +1），则旧版本的作废收到 409。

### 已归档炉次电费试算 `POST /api/drafts/{id}/electricity`

对**已归档**炉次的冻结温度曲线做只读电费试算：不保存试算、不改动方案；
配方之后被修改或停用都不影响试算（始终读取归档时冻结的曲线与档数）。

请求体字段：

- `startMinute`（必填）：首档开始时刻距**当天 00:00** 的分钟数，
  `0～1439` 的整数（例如 1438 = 当天 23:58）；
- `prices`（必填）：按 `00～23` 时排列的 **24** 个电价（元/kWh），
  每天原样重复；每项为非负有限数；
- `powers`（必填）：与冻结曲线**等长**的逐档功率（kW），每项为非负
  有限数；每档 5 分钟内功率恒定，`kWh = 功率 × 5 / 60`。

分摊与舍入：

- 档位跨小时（含跨午夜）时，按实际落入各小时的分钟数分摊电量与费用，
  小时边界自然也是午夜边界（电价按天重复）；
- **逐档费用各自四舍五入到两位小数**；
- **总费用先汇总未舍入的逐档费用，再四舍五入到两位**（因此总费用可能
  与逐档舍入值之和相差 1 分，页面会明确标注）；总电量不另行舍入。

状态码：

- 炉次 ID 未知 → **404**（无论请求体是否合法）；
- 炉次存在但**未归档**（活动草稿，含当前无解）→ **422**；
- 已归档但参数非法（开炉分钟越界 / 非整数、电价不是 24 个、含负数 /
  NaN / Infinity、功率为空或长度与冻结曲线不一致等）→ **422**，
  `detail` 为中文说明。

可直接复制执行（假设炉次 1 已归档，冻结曲线为 6 档
`[40,60,80,60,40,20]`，23:58 开炉，每档 60 kW；00 时电价 0.8、
23 时 0.3、其余 0.5 元/kWh）：

```bash
curl -sS -X POST http://localhost:8000/api/drafts/1/electricity \
  -H 'Content-Type: application/json' \
  -d '{
    "startMinute": 1438,
    "prices": [0.8, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
               0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
               0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.3],
    "powers": [60, 60, 60, 60, 60, 60]
  }'
```

首档 23:58～24:00 有 2 分钟在 23 时（0.3 元）、3 分钟在次日 00 时
（0.8 元）：`(2×0.3 + 3×0.8) / 60 × 60 = 3.00` 元；其后各档在 00 时
均为 `5 × 0.8 = 4.00` 元。返回：

```json
{
  "draftId": 1,
  "version": 1,
  "startMinute": 1438,
  "stepCount": 6,
  "totalMinutes": 30,
  "steps": [
    {"index": 1, "temperature": 40, "minutes": 5, "kwh": 5.0, "cost": 3.0},
    {"index": 2, "temperature": 60, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 3, "temperature": 80, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 4, "temperature": 60, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 5, "temperature": 40, "minutes": 5, "kwh": 5.0, "cost": 4.0},
    {"index": 6, "temperature": 20, "minutes": 5, "kwh": 5.0, "cost": 4.0}
  ],
  "totalKwh": 30.0,
  "totalCost": 23.0
}
```

跨普通小时边界的例子（`startMinute: 58`，即 00:58 开炉）：首档
00:58～01:03 拆成 00 时 2 分钟 + 01 时 3 分钟，费用
`(2×0.8 + 3×0.5) = 3.10` 元，之后各档均在 01 时（0.5 元）为 2.50 元，
总费用 15.60 元。

前端在「炉次草稿」详情中，仅**已归档**炉次显示「归档炉次电费试算」
卡片（开炉分钟、24 个分时电价、与冻结曲线等长的逐档功率）。切换炉次、
刷新详情或编辑任一试算输入时都会**立即清除旧试算**；试算请求自带代际，
此前请求的迟到成功 / HTTP 错误回包与网络异常一律作废，不会回填到当前界面。

前端对等待期交互的编排（仅前端行为，接口字段、状态码、冻结曲线与计费规则
均不变）：

- **等待中编辑立即解锁**：试算请求发出后，一旦编辑开炉分钟、电价或功率，
  立即清除旧结果与错误提示并恢复按钮，可用新输入**再次试算，无需等待旧
  请求结束**；旧请求随后无论成功、HTTP 错误还是网络异常，都不回填结果 /
  提示，也不改变当前按钮。
- **切换炉次 / 刷新详情同样作废**：一发起查看或刷新，旧试算即作废。只有
  **当前已归档详情成功加载后**才可提交试算；详情加载中、读取失败（含网络
  异常）或炉次未归档时，试算卡片隐藏或按钮不可用，杜绝向过期 / 不存在的
  归档发请求。
- **提交去重**：每次有效提交在等待**自身响应**期间禁用按钮（“试算中…”），
  并在提交入口去重，防止重复提交；被作废的旧请求不占用这一状态。
- **切走后又回到原炉次**：旧请求晚到的回包对新视图同样作废（成功也不
  回填），一切以重新读回的归档详情为准。
- **当前请求完成后恢复操作**：成功展示逐档试算结果；HTTP 错误（404/422
  等）在错误条显示后端中文原因；网络失败不改动数据并可直接重试——恢复与否
  只取决于当前这一代自身是否结束，**不受旧请求是否已经结束影响**。

### 已归档炉次原曲线复用检查 `POST /api/drafts/{id}/reuse-check`

对已归档炉次回答一个问题：**冻结的原曲线还能不能用于各配方的当前参数**。
只读核验、不重新求解，也不改动配方、草稿或归档。

- 按炉次创建时的**原配方引用顺序**，在**同一读取时点**读取各配方的当前
  参数与状态，逐项核验归档冻结曲线；
- 连续保温沿用原规则：每档 5 分钟，档位温度落在 `[保温下限, 保温上限]`
  内则连续进度 +1，达到所需档数即达标；离开区间时未达标进度归零，
  已达标者保持达标；
- 每项返回：配方 ID、当前 `version` 与 `status`、保温是否达标
  （`holdOk`）、**首次达标**的连续档位区间 `window`（`startStep` /
  `endStep`，从 1 计，无则 `null`）、**首次超过耐受上限**的档号
  `toleranceBreachStep`（从 1 计，无则 `null`）；耐受检查覆盖整条曲线
  （达标后仍逐档检查）；
- **停用配方同样完成温度检查**，仅其状态会使整体判为不可复用；
- 全部材料保温达标、无任何超限且配方均为可用时，`reusable` 才为
  `true`；
- 炉次 ID 未知 → **404**；炉次存在但未归档 → **422**。

可直接复制执行（全新实例上依次执行：建配方 → 建炉次 → 归档 → 检查）：

```bash
# 1) 两个配方 + 一个引用它们的炉次草稿（创建即求解，版本均为 1）
curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"钢甲","holdLow":60,"holdHigh":100,"duration":10,"tolerance":150}'
curl -sS -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"铜乙","holdLow":80,"holdHigh":120,"duration":5,"tolerance":160}'
curl -sS -X POST http://localhost:8000/api/drafts \
  -H 'Content-Type: application/json' -d '{"name":"十月首炉","recipeIds":[1,2]}'

# 2) 归档炉次 1（冻结曲线为 6 档 [40,60,80,60,40,20]）
curl -sS -X POST http://localhost:8000/api/drafts/1/archive \
  -H 'Content-Type: application/json' -d '{"version":1}'

# 3) 复用检查：原曲线能否用于各配方的当前参数
curl -sS -X POST http://localhost:8000/api/drafts/1/reuse-check
```

第 3 步返回（HTTP 200）：

```json
{
  "draftId": 1,
  "version": 1,
  "stepCount": 6,
  "temperatures": [40, 60, 80, 60, 40, 20],
  "items": [
    {"recipeId": 1, "name": "钢甲", "version": 1, "status": "active",
     "holdOk": true, "window": {"startStep": 2, "endStep": 3},
     "toleranceBreachStep": null},
    {"recipeId": 2, "name": "铜乙", "version": 1, "status": "active",
     "holdOk": true, "window": {"startStep": 3, "endStep": 3},
     "toleranceBreachStep": null}
  ],
  "reusable": true
}
```

归档后若配方被修改（例如钢甲保温区间改为 90～100°C，曲线最高仅 80°C），
同一接口会给出逐项结论：`钢甲` 变为 `holdOk:false, window:null`，
整体 `reusable:false`；若某配方耐受上限被调低，则
`toleranceBreachStep` 给出首次超限档号（1 计）；配方被停用时该项温度
检查照常完成，但 `status:"disabled"` 使整体不可复用。

前端在「炉次草稿」的已归档详情中显示「原曲线复用检查」卡片：点击后逐项
展示配方 ID、当前版本与状态、保温是否达标、首次达标区间与首次超限档号，
并给出整体可复用结论。**切换炉次或刷新详情会立即清除结论**；检查请求自带
代际，旧响应（成功 / HTTP 错误 / 网络异常）一律作废，不会回填到当前界面，
等待期间按钮禁用、不重复提交。

### 下载已归档炉次工艺单 `GET /api/drafts/{id}/sheet`

把**已归档**炉次导出为可脱离服务打开的**单份 HTML 工艺单**：响应为
`Content-Type: text/html; charset=utf-8` 的 **UTF-8 HTML 附件**
（`Content-Disposition: attachment`），文件名含炉次 ID 与冻结版本，例如
`draft-1-v3-sheet.html`（`filename*` 同时给出中文名
`炉次1-v3-工艺单.html`）。

状态码：

- 炉次 ID 未知 → **404**（返回 JSON 中文原因，与其他炉次接口一致）；
- 炉次存在但**未归档**（活动草稿，含当前无解）→ **422**；
- 已归档 → **200**，返回 HTML 附件。

导出**只读归档快照，不写数据库**：全部数据取自归档时冻结的炉次名称、ID、
冻结版本、归档时间、按原引用顺序排列的配方 ID 与冻结配方版本、冻结材料
名称 / 保温上下限 / 时长 / 耐受上限、冻结求解结果中的首次达标区间（含起止
档）、总时长与按档号排列的温度表；**不读取当前配方、不重新求解**，后续的
配方修改、配方停用及服务重启都不会改变文档内容。文档内档号从 1 起，第 k
档标注 `[(k−1)×5, k×5)` 分钟。样式全部内嵌在 `<style>` 中，不依赖网络、
外部资源或脚本；炉次 / 材料等文本一律按原字符展示（HTML 转义），不能被
当作 HTML 执行。下载后的 `.html` 文件可直接双击离线打开，也适合打印归档。

可直接复制执行（炉次 1 已归档、冻结版本为 v1）：

```bash
# -O 按服务端文件名保存：draft-1-v1-sheet.html
curl -sS -OJ http://localhost:8000/api/drafts/1/sheet
# 也可用 -o 自行指定文件名
curl -sS http://localhost:8000/api/drafts/1/sheet -o 炉次1-工艺单.html

# 未知炉次 → 404
curl -sS -i http://localhost:8000/api/drafts/999/sheet
# HTTP/1.1 404 Not Found
# {"detail":"炉次草稿 999 不存在。"}

# 存在但未归档 → 422
curl -sS -i http://localhost:8000/api/drafts/2/sheet
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"该炉次尚未归档，工艺单只能导出已归档炉次的冻结快照。"}
```

响应头示例：

```http
HTTP/1.1 200 OK
content-type: text/html; charset=utf-8
content-disposition: attachment; filename="draft-1-v1-sheet.html";
  filename*=UTF-8''%E7%82%89%E6%AC%A11-v1-%E5%B7%A5%E8%89%BA%E5%8D%95.html
```

前端仅在「炉次草稿」详情区，于**当前已归档详情成功加载之后**显示
「下载 HTML 工艺单」按钮；详情加载中、读取失败（含网络异常）或炉次未归档
时按钮隐藏。下载请求的请求编排与归档、试算、复用检查同一口径：

- **绑定点击那一刻的炉次 ID**：提交后立刻切到别的炉次，旧下载照常完成但
  **不触发保存、不写提示**；按钮始终只反映当前炉次。
- **切换炉次或刷新详情即作废旧下载**：旧响应（成功 / 404 / 422 / 500）或
  网络异常一律作废，既不会触发浏览器下载，也不会在新界面写入成功 / 失败
  提示；同一炉次的下载在自身响应回来前按钮禁用并按炉次 ID 去重，不同炉次
  的下载互不影响。
- **失败显示后端中文原因（如未归档 422）或网络异常提示，按钮立即可重试**；
  导出为只读操作，失败与重试都不改变任何数据。

## 单台炉开炉计划（仅安排已归档炉次）

为**单台炉**维护一份**已归档炉次**的开炉计划：每炉次最多安排一段开炉
区间，区间时长不手填——始终取该炉次**归档冻结曲线的档数 × 5 分钟**，
不读当前配方、不重新求解；归档、草稿与配方都不受计划读写影响。计划存入
现有 SQLite 卷（`planner-data`），重启保留；旧库启动时自动初始化为
**空计划（版本 1）**。

接口（均返回 JSON）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/furnace-plan` | 读取整台炉计划：`version` 与排段（炉次 ID、开始/结束分钟；按开始分钟、ID 排序）；空计划版本为 1 |
| PUT | `/api/furnace-plan` | 批量提交查看后的 1～20 项改动；成功整批写入、版本 +1 并返回完整计划 |

`PUT` 请求体：

- `version`（必填，≥1 整数）：批量查看时读到的计划版本；
- `changes`（必填，1～20 项数组，同批炉次 ID 不得重复），每项：
  - `draftId`：炉次 ID（严格整数）；
  - `startMinute`：**非负整数 Unix 开始分钟**（如 29851680 =
    2026-10-04 08:00 UTC），表示设置 / 覆盖该炉次的开始时间；`null`
    表示**移除**该炉次（原本不在计划中时为空操作，版本仍 +1）。
  - **未在 `changes` 中提及的炉次全部保留**，因此一批可以只改一两段。

结束分钟返回为 `endMinute = startMinute + 档数 × 5`；按改动后的**全部**
排段 `[startMinute, endMinute)` 判冲突，**首尾相接允许**（前一段的结束
正好等于后一段的开始不冲突），严格重叠才拒绝。允许**同批交换**两段的
开始时间——按改动后的完整集合判冲突，不会把交换误判为重叠。

状态码（任何失败都**不改变计划、不增加版本**）：

- **409**：版本过期（响应体附 `currentVersion`，重新 GET 后再提交），
  或改动后存在排段重叠（中文说明冲突的两段炉次与区间）；**进行中炉次**
  （已开炉未结束）的计划项**不得改时或移除**（覆盖为相同开始分钟属无
  改动，允许），**已结束炉次不得重新排入**（其计划项已在结束时移除，
  对它传 `null` 为空操作）；
- **404**：改动中的炉次 ID 未知（包括超出 SQLite 有符号 64 位整数范围
  `[-9223372036854775808, 9223372036854775807]`、无法入库查询的 ID，如
  `9223372036854775808`，一律按未知炉次处理，不截断、不转浮点）；
- **422**：炉次存在但**未归档**（活动 / 无解 / 已作废草稿，`null`
  移除同样要求已归档）；或 `startMinute` 超出 SQLite 可存储的整数上限
  `9223372036854775807`（服务原样存储，**不截断、不转为浮点数**，返回
  中文原因）；或请求体非法（版本 <1、改动非 1～20 项、ID 重复、开始
  分钟为负数 / 小数 / 布尔 / 字符串等）。

并发与隔离：整批改动在**单条事务**里提交，同版本并发 PUT 只有**一批**
成功，其余收到 **409**；任何 GET 都不会读到提交到一半的批次，看到的
永远是某一整次提交的完整计划。

### 读取计划（可直接复制执行）

```bash
curl -sS http://localhost:8000/api/furnace-plan
```

从未提交过时返回空计划（版本 1）：

```json
{"version": 1, "items": []}
```

有排段时按**开始分钟、再按炉次 ID** 排序，例如（炉次 1 冻结曲线 6 档 =
30 分钟、炉次 2 为 4 档 = 20 分钟）：

```json
{
  "version": 2,
  "items": [
    {"draftId": 1, "startMinute": 29851680, "endMinute": 29851710},
    {"draftId": 2, "startMinute": 29851710, "endMinute": 29851730}
  ]
}
```

### 创建 / 覆盖 / 移除（可直接复制执行）

接着上面的归档例子（炉次 1、2 已归档），基于查看版本 1 一次安排两段、
首尾相接：

```bash
curl -sS -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "changes": [
      {"draftId": 1, "startMinute": 29851680},
      {"draftId": 2, "startMinute": 29851710}
    ]
  }'
```

成功返回 **200** 与完整计划（版本 +1 变为 2）。之后只改炉次 2、炉次 1
未提及则保留；用 `null` 移除炉次 2：

```bash
# 覆盖：只提交要改的炉次，其余保留
curl -sS -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 2, "changes": [{"draftId": 2, "startMinute": 29851860}]}'

# 移除：startMinute 传 null（不在计划中也是空操作）
curl -sS -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 2, "startMinute": null}]}'
```

### 同批交换开始时间

两段互换开始时间时放在**同一批**里：服务按改动后的完整集合判冲突，交换
本身合法（下例两段各 30 分钟，08:00 与 09:00 互换）：

```bash
# 假设当前：炉次 1 在 29851680（08:00）、炉次 2 在 29851740（09:00）
curl -sS -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 4,
    "changes": [
      {"draftId": 1, "startMinute": 29851740},
      {"draftId": 2, "startMinute": 29851680}
    ]
  }'
```

> 注意分两次提交通常会先撞上重叠（把炉次 1 移到 09:00 时炉次 2 还在
> 09:00）；交换请用同一批完成。

### 版本过期与时间重叠 —— HTTP 409

```bash
# 别人已先成功提交（当前版本已到 3），仍带 version=2 提交
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 2, "changes": [{"draftId": 3, "startMinute": 29851680}]}'
# HTTP/1.1 409 Conflict
# {"detail":{"detail":"开炉计划已过期：你查看的是版本 2，当前为版本 3，请重新读取后再提交。",
#            "currentVersion":3}}

# 排段重叠：炉次 1 已占 [29851680, 29851710)，炉次 3 若 30 分钟长且
# 从 29851690 开始，区间严格相交（相接不报错）
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 3, "startMinute": 29851690}]}'
# HTTP/1.1 409 Conflict
# {"detail":"炉次 1（29851680～29851710）与炉次 3（29851690～29851720）的开炉时间重叠，请调整后重新提交。"}
```

未知炉次 → **404**；未归档炉次（含 `null` 移除）或非法入参（改动 0 项 /
超过 20 项、同批 ID 重复、负数 / 小数 / 布尔开始分钟等）→ **422**：

```bash
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 999, "startMinute": 0}]}'
# HTTP/1.1 404 Not Found
# {"detail":"炉次草稿 999 不存在。"}

curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 4, "startMinute": 0}]}'
# HTTP/1.1 422 Unprocessable Entity（炉次 4 还是活动草稿）
# {"detail":"炉次 4 尚未归档，开炉计划只能安排已归档炉次。"}
```

整数超出 SQLite 有符号 64 位范围时同样给出明确错误（任何失败都整批回滚，
**不截断、不转浮点**，计划与版本不变）：

```bash
# 未知整数炉次 ID（含超出有符号 64 位范围者）→ 404
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 9223372036854775808, "startMinute": 0}]}'
# HTTP/1.1 404 Not Found
# {"detail":"炉次草稿 9223372036854775808 不存在。"}

# 已归档炉次的开始分钟超出 SQLite 可存储上限 → 422（中文原因）
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 1, "startMinute": 9223372036854775808}]}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"炉次 1 的开始分钟 9223372036854775808 超出可存储范围（最大 9223372036854775807），请提供更小的非负整数。"}

# 上限值 9223372036854775807 本身可原样存储，照常接受（200，版本 +1）
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 1, "startMinute": 9223372036854775807}]}'
```

## 开炉与结束登记（HTTP 登记，不控制设备）

开炉 / 结束**只做登记**，服务不控制任何设备。登记数据（`furnace_runs`
表）与计划同在现有 SQLite 卷（`planner-data`）里，**跨重启保留**；旧库
启动时自动初始化为空（没有正在占炉的炉次）。归档及以上所有 HTTP 合同
保持不变。

约束：

- **只能启动计划首项**：按开始分钟、再按炉次 ID 排序的第一段；
- **每炉次仅执行一次**：已结束炉次不能再次开炉，也不能重新排入计划；
- **同时仅一项进行中**：一炉未结束不能开下一炉；
- 所有时间都是 **SQLite 可存的非负整数 Unix 分钟**（最大
  `9223372036854775807`，不截断、不转浮点）；实际开始**不早于计划开始**
  且**不早于上一炉次的实际结束**；实际结束**不早于本次实际开始**；
- 开炉成功：写入 `running` 记录并**冻结当时的计划起止**，计划 version
  **+1**（计划项保留）；
- 结束成功：保存结束分钟、置 `finished`、**释放占炉**、**移除计划项**与
  计划 version **+1** 在**同一事务一起生效**，**不自动挪动后续计划**；
- 开炉、结束与计划修改的并发按**请求先后**串行生效：旧 version 或状态
  冲突一律 **409**，未知炉次 **404**，非法分钟 **422**；任何失败都**不
  改状态、不增加版本**。

### 读取记录与当前占炉（可直接复制执行）

```bash
curl -sS http://localhost:8000/api/furnace-runs
```

`runs` 按**开炉顺序**（内部自增 ID）返回全部执行记录；`current` 为当前
进行中记录（至多一条），没有则为 `null`。未结束时 `actualEndMinute` 为
`null`：

```json
{
  "current": {
    "id": 1,
    "draftId": 1,
    "planStartMinute": 29851680,
    "planEndMinute": 29851700,
    "actualStartMinute": 29851680,
    "actualEndMinute": null,
    "status": "running",
    "planVersion": 2,
    "startedAt": "2026-10-04T08:00:00Z",
    "endedAt": null
  },
  "runs": [ /* 同上结构，含已结束记录 */ ]
}
```

### 开炉：启动计划首项（可直接复制执行）

请求体：`draftId`（计划首项炉次 ID）、`version`（GET 计划时读到的
version）、`actualStartMinute`（实际开始 Unix 分钟）：

```bash
# 先读计划，拿到 version 与首项（假设 version=2，首项为炉次 1，
# 计划 29851680～29851700；炉次 1、2 此前都未执行过）
curl -sS http://localhost:8000/api/furnace-plan

curl -sS -i -X POST http://localhost:8000/api/furnace-runs/start \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 1, "version": 2, "actualStartMinute": 29851685}'
# HTTP/1.1 201 Created
# {"id":1,"draftId":1,"planStartMinute":29851680,"planEndMinute":29851700,
#  "actualStartMinute":29851685,"actualEndMinute":null,"status":"running",
#  "planVersion":2,"startedAt":"...","endedAt":null}
```

成功后 GET 计划可见**计划项仍在**、version 已 +1（变为 3）。开炉期间
计划修改受到限制：

```bash
# 改时 / 移除进行中项：整批拒绝，409，计划与版本不变
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "changes": [{"draftId": 1, "startMinute": 29851900}]}'
# HTTP/1.1 409 Conflict
# {"detail":{"detail":"炉次 1 正在进行中，不能移除或修改其计划开始时间；如需改排，请先结束该炉次。",
#            "currentVersion":3}}

# 携带开炉前的旧 version / 启动非首项 / 已有进行中再开 / 同一炉次再开：
curl -sS -i -X POST http://localhost:8000/api/furnace-runs/start \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 2, "version": 2, "actualStartMinute": 29851700}'
# HTTP/1.1 409 Conflict（version 过期、且炉次 2 不是首项、炉次 1 进行中）

# 实际开始早于计划开始（或早于上一炉次实际结束）：422，不改状态
curl -sS -i -X POST http://localhost:8000/api/furnace-runs/start \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 1, "version": 3, "actualStartMinute": 29851679}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"实际开始分钟 29851679 早于该炉次的计划开始 29851680，不能提前开炉。"}

# 未知炉次：404
curl -sS -i -X POST http://localhost:8000/api/furnace-runs/start \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 999, "version": 3, "actualStartMinute": 29851680}'
# HTTP/1.1 404 Not Found
# {"detail":"炉次草稿 999 不存在。"}
```

### 结束：保存、释放、移除计划项、版本 +1（可直接复制执行）

请求体只需**进行中炉次 ID** 与 `actualEndMinute`（无需计划 version）：

```bash
curl -sS -X POST http://localhost:8000/api/furnace-runs/finish \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 1, "actualEndMinute": 29851720}'
# {"id":1,"draftId":1,"planStartMinute":29851680,"planEndMinute":29851700,
#  "actualStartMinute":29851685,"actualEndMinute":29851720,"status":"finished",
#  "planVersion":2,"startedAt":"...","endedAt":"..."}
```

结束后（同一事务）：

- `GET /api/furnace-runs`：该记录 `status=finished`、结束分钟已保存，
  `current` 回到 `null`（占炉释放，可开下一炉）；
- `GET /api/furnace-plan`：炉次 1 的计划项已删除、version 再 +1；**后续
  排段的开始分钟原样保留，不自动挪动**——若上一炉实际拖期导致新首项的
  计划开始早于上次实际结束，开炉会收到 **422**，需先用 `PUT
  /api/furnace-plan` 改排（进行中项的限制此时已解除）。

```bash
# 未开炉就结束 / 重复结束：409；未知炉次：404
curl -sS -i -X POST http://localhost:8000/api/furnace-runs/finish \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 2, "actualEndMinute": 29851720}'
# HTTP/1.1 409 Conflict
# {"detail":"炉次 2 尚未开炉，无法结束。"}

# 结束早于本次实际开始：422，不改状态
curl -sS -i -X POST http://localhost:8000/api/furnace-runs/finish \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 1, "actualEndMinute": 29851684}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"结束分钟 29851684 早于本次实际开始 29851685，结束不能早于开始。"}

# 已结束炉次不能重新排入计划，也不能再次开炉：均为 409
curl -sS -i -X PUT http://localhost:8000/api/furnace-plan \
  -H 'Content-Type: application/json' \
  -d '{"version": 4, "changes": [{"draftId": 1, "startMinute": 29852000}]}'
# HTTP/1.1 409 Conflict
# {"detail":{"detail":"炉次 1 已经开炉执行并结束，不能重新排入开炉计划。",
#            "currentVersion":4}}
```

请求体非法（`version<1`、分钟为负数 / 小数 / 布尔 / 字符串等）按统一
参数校验返回 **422**，中文说明首个非法字段。

## 已结束炉次实测质检与同版工艺阻断

对**已结束**炉次提交**实测温度**做质检：按该炉次**归档冻结**的配方参数
核验**连续保温**与**全程耐受**（**不核验升降温及末温**）；未达标或超限的
（配方 ID, 冻结版本）被**阻断**，同版工艺的后续开炉登记被拒绝（409）。
质检记录保存在现有 SQLite 卷（`planner-data`）的 `run_quality` 表中，
**跨重启保留**；旧库启动时自动初始化为空表。**配方修改 / 停用不改质检
记录与阻断**，开炉计划与归档也不受质检读写影响。

规则：

- **提交实测** `PUT /api/furnace-runs/{id}/quality`：请求体
  `temperatures` 为 **1～20 个 0～200°C 的有限数**（允许小数，拒绝
  布尔 / 字符串 / NaN / Infinity），依次代表**实际开始后**的 5 分钟
  恒温；**档数 ×5 须等于实际时长**（实际结束 − 实际开始分钟）。每炉次
  只保存**一份**，重复提交**整份替换**实测与结论，**不合格也保存**；
- **逐项结论**按归档**引用顺序**返回：配方 ID、**冻结版本**、**首次达标
  区间**（`window`，档号从 1 起，无则 `null`）与**首次超限档号**
  （`toleranceBreachStep`，从 1 起，无则 `null`）；连续保温沿用原规则
  （落在 `[保温下限, 保温上限]` 内连续进度 +1、达到所需档数达标、离开
  区间未达标进度归零），耐受检查覆盖全部实测档；
- **阻断**：任一配方**未达标**（`window` 为 `null`）或**超限**
  （`toleranceBreachStep` 非 `null`），其（配方 ID, 冻结版本）即被阻断，
  来源为该炉次；阻断由质检记录**即时推导**——替换实测后本炉次的阻断按
  新结论增减，**同版其他失败来源仍在则继续阻断**；
- **开炉拦截**：`POST /api/furnace-runs/start` 按**归档冻结版本**核对
  当前阻断，命中即 **409** 并附被阻断配方与来源炉次；质检与开炉按
  **请求先后**串行生效——**先开炉者仍可结束**（进行中的炉次不受此后
  提交的阻断影响），**阻断先提交则不能开炉**；
- **状态码**：未知炉次 **404**；未开炉 / 进行中（未结束）、实测数量或
  取值非法、档数 ×5 不等于实际时长 **422**；任何失败都**不改数据**
  （原有质检记录、阻断、计划与版本均不变）。

### 提交实测并读取结论（可直接复制执行）

接续「开炉与结束登记」的例子：炉次 1 引用配方 1（钢甲 60～100°C /
10 分钟 / 耐受 150°C）与配方 2（铜乙 80～120°C / 5 分钟 / 耐受 160°C），
已归档（冻结版本均为 1）并实际执行 30 分钟（6 档）后结束：

```bash
# 提交 6 档实测（6×5 = 30 分钟 = 实际时长）：两配方均达标、无超限
curl -sS -X PUT http://localhost:8000/api/furnace-runs/1/quality \
  -H 'Content-Type: application/json' \
  -d '{"temperatures": [40, 60, 80, 80, 60, 40]}'
```

返回（HTTP 200）：按引用顺序给出配方 ID、冻结版本、首次达标区间与
首次超限档号（无则 `null`），`passed` 为整体结论：

```json
{
  "draftId": 1,
  "actualStartMinute": 29851680,
  "actualEndMinute": 29851710,
  "temperatures": [40, 60, 80, 80, 60, 40],
  "items": [
    {"recipeId": 1, "version": 1,
     "window": {"startStep": 2, "endStep": 3}, "toleranceBreachStep": null},
    {"recipeId": 2, "version": 1,
     "window": {"startStep": 3, "endStep": 3}, "toleranceBreachStep": null}
  ],
  "passed": true,
  "createdAt": "2026-10-04T09:00:00Z",
  "updatedAt": "2026-10-04T09:00:00Z"
}
```

实测只看保温与耐受：**允许升降温跳变、末档不必回到 20°C**（例如
`[0, 100, 100, 100, 100, 0]` 同样合格）；实测值可以是 `62.5` 这样的
小数。读取与替换：

```bash
# 读取该炉次质检记录（未知炉次 / 尚无记录 → 404）
curl -sS http://localhost:8000/api/furnace-runs/1/quality

# 整份替换：实测与结论一起换新（createdAt 保留，updatedAt 更新）
curl -sS -X PUT http://localhost:8000/api/furnace-runs/1/quality \
  -H 'Content-Type: application/json' \
  -d '{"temperatures": [40, 62.5, 80, 80, 60, 40]}'
```

### 不合格 → 阻断同版工艺后续开炉（可直接复制执行）

炉次 2（同版配方，冻结版本同为 1）结束后提交实测：第 3、4 档 200°C
超过两配方耐受上限，且无任何连续保温达标区间——**不合格也保存**：

```bash
curl -sS -X PUT http://localhost:8000/api/furnace-runs/2/quality \
  -H 'Content-Type: application/json' \
  -d '{"temperatures": [40, 60, 200, 200, 60, 40]}'
# {"draftId":2,...,"items":[
#   {"recipeId":1,"version":1,"window":null,"toleranceBreachStep":3},
#   {"recipeId":2,"version":1,"window":null,"toleranceBreachStep":3}],
#  "passed":false,...}

# 当前阻断清单：配方 1、2 的版本 1 均被阻断，来源为炉次 2
curl -sS http://localhost:8000/api/recipe-blocks
# {"blocks":[
#   {"recipeId":1,"version":1,"sourceDraftIds":[2]},
#   {"recipeId":2,"version":1,"sourceDraftIds":[2]}]}
```

此时同版工艺（引用配方 1、2 且归档冻结版本同为 1）的炉次 3 排入计划后
**不能开炉**：

```bash
curl -sS -i -X POST http://localhost:8000/api/furnace-runs/start \
  -H 'Content-Type: application/json' \
  -d '{"draftId": 3, "version": 6, "actualStartMinute": 29851740}'
# HTTP/1.1 409 Conflict
# {"detail":{
#    "detail":"同版工艺已被质检阻断，不能开炉：配方 1 版本 1（来源炉次 2）；配方 2 版本 1（来源炉次 2）。",
#    "currentVersion":6,
#    "blocks":[{"recipeId":1,"version":1,"sourceDraftIds":[2]},
#              {"recipeId":2,"version":1,"sourceDraftIds":[2]}]}}
```

把炉次 2 的实测**替换**为合格后，该炉次对阻断的贡献移除；若同版还有
其他失败来源（例如炉次 3 之后也不合格），阻断**继续存在**，直到所有
来源都替换为合格：

```bash
curl -sS -X PUT http://localhost:8000/api/furnace-runs/2/quality \
  -H 'Content-Type: application/json' \
  -d '{"temperatures": [40, 60, 80, 80, 60, 40]}'
curl -sS http://localhost:8000/api/recipe-blocks
# {"blocks":[]}   —— 无其他来源时阻断解除，同版炉次可再开炉
```

质检与开炉按**请求先后**生效：炉次先开炉（进行中）后其版本才被阻断的，
**仍可正常结束**；阻断先提交的，后续同版开炉一律 409。

### 质检状态码（失败均不改数据）

```bash
# 未知炉次 → 404
curl -sS -i -X PUT http://localhost:8000/api/furnace-runs/999/quality \
  -H 'Content-Type: application/json' -d '{"temperatures": [40]}'
# HTTP/1.1 404 Not Found
# {"detail":"炉次草稿 999 不存在。"}

# 未开炉 / 进行中（未结束）→ 422
curl -sS -i -X PUT http://localhost:8000/api/furnace-runs/4/quality \
  -H 'Content-Type: application/json' -d '{"temperatures": [40, 60, 80, 80, 60, 40]}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"炉次 4 尚未开炉，质检需要已结束的开炉记录。"}

# 档数 ×5 ≠ 实际时长（炉次 1 实际 30 分钟，只给了 5 档）→ 422
curl -sS -i -X PUT http://localhost:8000/api/furnace-runs/1/quality \
  -H 'Content-Type: application/json' -d '{"temperatures": [40, 60, 80, 80, 60]}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"实测温度档数 ×5（25 分钟）须等于实际时长 30 分钟（实际开始 29851680、实际结束 29851710）。"}

# 取值非法（空数组 / 超过 20 档 / 超出 0～200°C / 布尔 / 字符串 / NaN 等）→ 422
curl -sS -i -X PUT http://localhost:8000/api/furnace-runs/1/quality \
  -H 'Content-Type: application/json' -d '{"temperatures": [40, 60, 201, 80, 60, 40]}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":"第 3 档实测温度 201 必须是 0～200°C 的有限数。"}
```

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由与参数校验（schedule / recipes / drafts / archive / cancel / 复用检查 / 电费试算 / 工艺单下载 / 单台炉开炉计划 / 开炉结束登记 / 实测质检与同版阻断）
│   ├── solver.py      # 规则常量、校验与 BFS 求解（配方与草稿复用）
│   ├── reuse.py       # 已归档炉次原曲线复用检查（只读核验，不重新求解）
│   ├── quality.py     # 已结束炉次实测温度质检（按归档配方核验保温与耐受，不核验升降温及末温）
│   ├── electricity.py # 已归档炉次电费试算（跨小时/午夜分摊、逐档与总费用舍入）
│   ├── exports.py     # 已归档炉次单份离线 HTML 工艺单渲染（纯归档快照，样式内嵌）
│   ├── db.py          # SQLite 连接、建表（含 furnace_runs 登记表、run_quality 质检表）、全局读锁与写操作互斥锁
│   └── store.py       # 配方 / 炉次草稿 / 归档 / 作废 / 归档曲线只读 / 复用检查 / 工艺单快照 / 开炉计划 / 开炉结束登记 / 实测质检与同版阻断 的事务化读写
├── static/
│   ├── index.html     # 页面（编排求解 / 材料配方 / 炉次草稿 三个标签页）
│   ├── styles.css     # 样式
│   └── app.js         # 表单、请求、结果与 SVG 曲线渲染
├── Dockerfile
├── docker-compose.yml # 含命名卷 planner-data → /data，持久化 planner.db
├── requirements.txt
└── README.md
```

数据库路径：环境变量 `PLANNER_DB`（Compose 中为 `/data/planner.db`）；
裸跑且 `/data` 不可写时自动落到仓库内 `data/planner.db`。
