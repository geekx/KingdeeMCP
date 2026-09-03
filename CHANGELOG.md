# 更新日志 (Changelog)

本项目所有重要变更都会记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 当前 PyPI 版本：`0.3.0`（见 `pyproject.toml`）。本文件按功能里程碑汇总，未单独打 git tag。

---

## [Unreleased]

### 修复：AIP-04/05 的忠告生成了，但没人知道要看它

`Dispatcher.act()` 早就把判断层的忠告塞进 `out["advisories"]`，PR 里也写了
"经 `Dispatcher.act` 的 `advisories` 交出"——这句话在代码层面是真的，但
**从没有任何文档告诉 Claude 要去读它**：`kd_act` 的工具说明没提，`SKILL.md`
没提，也没有一条测试验证这个字段真的会出现在返回体里。于是这个功能对使用它
的 agent 是不可见的——写出来了，但没人会用。

同一个病，第三次犯：装完丢失的失败日志、说反话的审计器，现在是没人知道该看
的忠告字段。都是"代码本身没问题，但外部没人能发现它在工作"。

新增 `tests/test_base_ontology.py::TestActAdvisories` 5 条集成测试，端到端
验证 `advisories` 字段：destructive 动词（如 delete）必带 AIP-04，走「操作」
接口且未给 `operation` 的动词（如 close）必带 AIP-05，给了就不再提醒，
无忠告时字段整个不存在（不必分辨"空数组"和"没有意见"）。

`kd_act` 的工具说明与 `SKILL.md` 都补上了：有 `advisories` 时先念给用户听、
再执行，不要静默略过。

## [0.3.0]

### 新增：AIP Logic 判断层（`aip/`）

「这一步能不能做」此前有四种答法，各长在一处（`check_*` 抛异常、
`availability()` 返回 dict、`validate_profile` 追加 errs/warns、saga 守卫
又一套）。四份实现回答同一个问题，于是可以彼此矛盾而没人发现。

现收拢成声明式、以本体为参数的纯函数：

- **一次给全部理由，不短路**——三个前置条件本来是三个来回，现在是一个。
- **「不知道」不等于「可以」**——`Decision.undetermined` 与 `allowed` 分开，
  事实不全时 `allowed` 为 `False`。旧 `availability()` 在状态未知时返回
  `enabled: True`，只读该字段的调用方会直接走下去。
- **纯函数**，不发请求、不读文件、不看时钟，故可独立运行。

新增规则 AIP-04（不可逆需人工确认）、AIP-05（二开单操作编码），
经 `Dispatcher.act` 的 `advisories` 交出。不新增 MCP 工具：
`kd_describe(what='logic')`。

### 新增：`kd-logic`，判断层可脱离模型独立运行

冷启动约 130 ms，零 token。退出码 0/1/2/3 可直接用于脚本编排。
`kd-logic serve` 可挂成本地 HTTP 端点（仅监听回环——判断层不做鉴权）。

### 破坏性变更：包结构

`base/` `aip/` `saga/` `pipeline/` `indexlayer/` `harness/` `wikiskill/`
由仓库顶层目录收进 `src/kingdee_ontology/` 命名空间。

**原因**：这些包此前**根本没被打进 wheel**（`packages` 只列了
`src/kingdee_mcp`），`pip install` 装不到；而且 `base`、`pipeline` 这类
名字也不能就这么上 PyPI，会和别的包撞名。

- `from base.ontology import …` → `from kingdee_ontology.base.ontology import …`
- `python -m base.server` → `python -m kingdee_ontology.base.server`
  （或直接用新入口 `kingdee-ontology`）
- `operation_audit` 由 `tools/ontology/` 移入包内——它被 `Dispatcher` 直接
  导入，是运行期代码，此前只靠 conftest 补 `sys.path` 才导得到。

### 变更：`pyodbc` 移出必装项

只被 4 个可选的 SQL Server 目录探查工具用到，却是全链路里唯一需要现场
编译的依赖。改为 `pip install kingdee-mcp[sql]`。导入处早已惰性并带安装
提示，不影响任何现有调用。

### 新增：离线包（内网／隔离网段）

金蝶云星空常部署在开不了外网的机器上，所以离线安装是刚需而非锦上添花。
`python3 tools/package/build_offline.py` 造两种产物：

- **`kd-logic.pyz`**（157 KB，单文件，**不用装**）——自带 PyYAML 纯 Python
  实现与本体注册表，在连 PyYAML 都没有的解释器上照样跑。
- **`wheelhouse/`**（39 个 wheel + 安装脚本）——`pip install --no-index` 装全套。
  认平台，可 `--platform win_amd64 --python-version 3.11` 跨平台造。

`MANIFEST.json` 带每个文件的 SHA256：离线传输靠 U 盘和邮件附件，传坏了要能发现。

打 tag 时由 `.github/workflows/release-offline.yml` 自动造好挂到 Release——
内网机器连 clone 都做不到，不能要求对方自己构建。

造这个包时踩到并修掉的三处：

- `zipapp` 的 `main=` 生成的入口是 `cli.main()`，**返回值被丢掉，退出码恒为 0**
  ——于是「不可以」和「事实不全」都被当成「可以」，恰好是最坏的坏法。
  改为自己写 `__main__.py`。
- 注册表原用 `Path.read_text` 读，打进 zip 后包目录不是真实目录，直接失败。
  改用 `importlib.resources`（两种情形都认），文件系统只作兜底。
- 构建时拒绝任何 `.so`/`.pyd`：zipimport 加载不了，混进去只会在没网的
  那台机器前面才炸。

### 修复：编排的起点不再只有销售订单

后端从来没这个限制——示例租户里「采购收货入库」就起于采购订单。是界面把一个
**默认值**演成了约束：预置步骤里硬编码一条 `SAL_SaleOrder` 的下推，而页面上
没有任何地方能换起点。

新增「起点」控件：补全输入框（认中文名、`form_id`、别名，56 个可选）＋
一排常用起点 chips（按可下推目标数排序，链头最可能是一件事的开头）。
换起点会清空原有步骤——两条链混在一起产出的配置过不了校验——但**可撤销**，
静默丢掉别人的活是不可接受的。认不出的输入会明确报错，而不是默默不动。

一处概念分家：`cSel`（当前位置，`commitSteps()` 会把它推到步骤链末端）与
`startNoun`（这件事从哪张单开始，加多少步都不变）此前是同一个变量。起点栏若
跟着 `cSel` 走，加一步就会显示成下游单。回归测试用变异验证过：退回旧语义后
那条测试确实失败。

### 修复：装完之后失败日志静默失效

`src/kingdee_mcp/server.py` 里 7 处 `from scripts.failure_log import FailureLogger`
全在 `try/except: pass` 里。`scripts/` 不在 wheel 里，于是**每一个 pip 安装的部署**
中，被文档称作「记忆层核心」的失败日志都悄悄不工作——不报错、不留痕，谁也不会发现。

`failure_log.py` 只依赖标准库、且被服务端在运行期导入，故移入
`kingdee_ontology/failure_log.py`。同时清掉 `server.py` 里两处早已失效的
`sys.path.insert`（绝对导入之后它们什么也不做，装成 wheel 后指向的目录压根不存在）。

**这是打包守卫自己的洞**：wheel 里装了两个包，守卫只扫了 `kingdee_ontology`。
现已扫描全部已交付的包，并新增一条专门查 `try/except` 里的不可交付导入——
用 except 兜住并不能让一个装不到的模块变得可用，只会让失效变得更难发现。
两种形态都验过：把导入改回 `scripts.` 时守卫确实报错。

### 修复：审计器 AT-07 说了一件与代码相反的事

AT-07 是**无条件**发出的，文案写死「legacy 的 5 个 push 工具尚未接入，仍无法校验
某条下推是否合法」。可它们早就接上了：`_post_raw` 的 push 分支是所有下推路径的
唯一咽喉点，在那里查注册表，并在 `KINGDEE_STRICT_LINKS=1` 时阻断；另有 6 个工具
在返回体里附带链接状态。

一条与代码相反的审计发现比不报更糟——它教人忽略审计输出。这已是第二次
（前一次是 F-1，记在 `04-audit-trail.md`），病根相同：**断言了一件没去查的事**。

现改为去 AST 里核实接线，并如实报告真正剩下的事实：默认只提示、不阻断
（这是刻意的——未登记只说明本表不全，不代表该转换关系不存在，贸然阻断会挡掉
二开账套里真实可用的下推）。

新增 `tests/test_auditor_accuracy.py`：不看措辞，只做变异测试——把咽喉点的查表
拆掉、把严格开关去掉，AT-07 的结论必须跟着升级为 warning。结论不随代码变的检查项，
不是审计，是背书。

### 新增：打包保护（`tests/test_packaging.py`）

打包缺陷对普通测试是隐形的——conftest 补了 `sys.path`，于是「只在源码树
里导得到」的模块照样过测试，装成 wheel 才 `ModuleNotFoundError`。
现分两层守：静态检查（导入来源、`sys.path` 补丁、判断层依赖闭包）+
真建 wheel 真装进干净 venv 真跑。两层都进了 CI。

---

## [Unreleased]

### Added（新增）

- **远程传输支持（HTTP / SSE / Streamable HTTP）**：`main()` 新增 `--transport`（stdio/sse/streamable-http，默认 stdio）、`--host`、`--port` 参数，并支持同名环境变量 `KINGDEE_MCP_TRANSPORT` / `KINGDEE_MCP_HOST` / `KINGDEE_MCP_PORT`。现在可将服务以 SSE（`/sse`）或 Streamable HTTP（`/mcp`）模式运行，便于部署到服务器或网关平台远程调用、免客户端安装。兼容老版本 mcp（<1.9 不支持 streamable-http 时自动回退 sse）。

---

## [0.2.1] - 2026-08-05

### Fixed（修复 · 文档）

- **修正改用账号密码登录的原因说明**：README「从 0.1.0 升级的破坏性变更」与 `server.py` `_login` docstring 原写为「为避免第三方应用授权的 APP 白名单限制、公有云/私有云通用」，该原因有误。正确原因：账号密码(ValidateUser) 以真实用户身份执行 WebAPI、携带该用户自身业务权限（含数据权限控制）；第三方应用授权(LoginByAppSecret) 以应用身份登录、不携带真实用户权限，报表等依赖数据权限的查询会受应用授权范围限制。无功能/行为变更。

---

## [0.2.0] - 2026-08-05

### Changed（变更）

- **版本对齐与重新发版**：本地源码自 2026-07-19 起已包含 86 工具、账号密码登录、SQL Server 探查等大量更新，但 PyPI 上仍停留在 2026-03-25 发布的旧 `0.1.0`（仅 13 工具 + AppSecret 登录）。本版将 PyPI 包与源码对齐，统一为 `0.2.0`。
- **README / 文档同步**：PyPI 展示的 README 已更新为 86 工具 + 账号密码登录的说明（此前 PyPI 长期展示旧版文档）。

### Added（新增 · 相对 PyPI 旧 0.1.0）

- 86 个工具（生产 / 成本 / 资产 / 审计 / 采购 / 销售 / 库存 / 工作流 / 元数据 / 系统 / 统计等 13 大业务域）。
- 4 个 SQL Server 探查工具（`kingdee_discover_tables` / `kingdee_discover_columns` / `kingdee_describe_table` / `kingdee_discover_metadata_candidates`），需配置 `MCP_SQLSERVER_*`。
- 元数据动态查询（`get_bill_template` / `validate_bill` / `refresh_metadata`），元数据本地缓存。
- MCP 使用日志系统、错误自描述、强制 HTTP/1.1 解决金蝶 WebAPI 偶发 502。
- **财务报表查询工具 `kingdee_query_report`**：通过专用 `GetSysReportData`（KDSReportAPIService）端点查询总账/财务报表，与单据查询（ExecuteBillQuery）分属不同服务。已实测确认科目余额表 `GL_RPT_AccountBalance`、总账账龄分析表 `GL_AgingSchedule`（无 `RPT_` 前缀）；其余报表 formId 待逐张查证。内层过滤参数（账簿/年度/期间/科目等）由调用方按账套透传，避免臆造字段名。

### Breaking（破坏性变更）

- **登录方式变更**：移除第三方应用授权登录（`LoginByAppSecret`），改为仅账号密码（`ValidateUser`）。旧环境变量 `KINGDEE_APP_ID` / `KINGDEE_APP_SEC` **已失效**，须改用 `KINGDEE_PASSWORD`。详见 README「从 0.1.0 升级的破坏性变更」。

### Fixed（修复 · 文档）

- README「常见问题」新增 `uvx` 启动报 `No module named 'mcp.server.fastmcp'` 的排查（清缓存或改用 `pip install` + `python -m kingdee_mcp.server`）。

---

## [0.1.0] - 2026-07-19

### Added（新增）

- **13 大业务域、共 86 个工具**，覆盖生产制造、成本核算、固定资产、审计合规、采购、销售、库存、工作流审批、基础资料、元数据探查、系统查询等。
- **生产制造模块**：生产订单查询/保存/提交/审核、MRP 运算结果、生产计划、生产汇报、生产入库、生产领料下推（`kingdee_query_production_*`、`kingdee_save_production_order`、`kingdee_push_production_*` 等 12 个）。
- **成本核算模块**：材料成本、成本计算、成本趋势、实际 vs 标准成本对比、完工产品成本、成本调整单等（`kingdee_query_material_cost`、`kingdee_query_cost_calculation`、`kingdee_save_cost_adjustment` 等 12 个）。
- **固定资产模块**：资产卡片、资产折旧、资产盘点、资产调拨、资产新增（`kingdee_query_fixed_asset`、`kingdee_save_asset` 等 6 个）。
- **审计合规模块**：操作日志、变更日志、审核日志、复合「新建并审核」「下推并审核」工作流（`kingdee_query_operation_logs`、`kingdee_query_change_log`、`kingdee_create_and_audit`、`kingdee_push_and_audit` 等 7 个）。
- **元数据动态查询**：`kingdee_get_bill_template`（取已验证单据骨架）、`kingdee_validate_bill`（保存前校验，不真正落库）、`kingdee_refresh_metadata`（强制刷新），元数据落盘缓存到 `~/.workbuddy/kingdee_metadata_cache/`。
- **SQL Server 探查工具 4 个**：`kingdee_discover_tables`、`kingdee_discover_columns`、`kingdee_describe_table`、`kingdee_discover_metadata_candidates`（配置 `MCP_SQLSERVER_*` 环境变量后可用）。
- **MCP 使用日志系统**：记录工具调用，便于审计与排查。
- **权限架构设计方案**文档（`docs/permission-architecture.md`）。
- **TEST_GUIDE 与生产工作流 e2e** 测试，回归网覆盖核心链路。

### Changed（变更）

- **登录方式改为仅账号密码（ValidateUser）**，移除第三方应用授权（原 `LoginByAppSecret`）。配置只需 `KINGDEE_SERVER_URL`、`KINGDEE_ACCT_ID`、`KINGDEE_USERNAME`、`KINGDEE_PASSWORD` 四项，**不再需要 AppID / AppSecret**。
- `kingdee_view_bill` 返回结果精简，只保留常用字段，降低 token 消耗。
- `kingdee_query_bills` 等查询类工具的过滤与返回结构优化。
- 批量操作（提交/审核/反审核/删除）真正批量化，减少循环请求。
- 错误自描述：接口失败时返回更可读的中文错误，便于 AI 与用户定位。
- 强制使用 HTTP/1.1 解决金蝶 WebAPI 偶发 502 问题。
- 文档/示例/脚本/测试整体同步。

### Fixed（修复）

- **销售报价单（SAL_Quotation）保存报"含税单价不能小于等于0"的真正根因 = 字段顺序**：本二开账套金蝶 Save 对字段顺序敏感，`FQUOTATIONFIN`（财务信息，含结算币别/含税标志）必须排在 `FQUOTATIONENTRY`（分录）之前，否则分录单价被算成 0。`kingdee_save_bill` 新增防御性排序（所有非 ENTRY 键统一前置），`BILL_TEMPLATES` 补 `FQUOTATIONFIN` 并置于 ENTRY 之前。
- 修正 `kingdee_push_bill` API 格式、`_post_raw` 入参与生产下推（`push_production`）参数。
- 补全若干缺失函数，修复批量操作的一致性。

### Removed（移除）

- 第三方应用授权登录（`LoginByAppSecret`）及相关 `APP_ID` / `APP_SEC` 环境变量、配置项。

---

## 历史提交摘要（按时间倒序，供溯源）

| 提交 | 说明 |
|------|------|
| `f58c2a7` | fix: 销售报价单 Save 字段顺序校正 + 新增 API 中心快照 |
| `c3fbaf5` | feat: 登录改为仅账号密码(ValidateUser)，移除第三方应用授权 |
| `a5b17e5` | chore: 忽略运行时日志/工具缓存/记忆目录/临时文件 |
| `6145af1` | test: 新增 TEST_GUIDE 和生产工作流 e2e |
| `a61d845` | chore: 同步文档/示例/脚本/测试 |
| `5c742a0` | feat: 元数据动态查询 + view 结果精简 |
| `6b7b58d` | docs: 添加权限架构设计方案 |
| `529a55e` | feat: 添加 MCP 使用日志系统 |
| `5bbc744` | fix: 批量操作真批量化 + 补全缺失函数 + 修正 push_production 参数 |
| `04d4eba` | feat: 新增 MRP/生产计划/生产汇报查询工具 |
| `96f7f93` | feat: 新增审计合规模块 API |
| `354e339` | feat: 新增生产、成本、资产管理等模块 API |
| `e034152` | feat: 复合工作流工具 + 错误自描述 + e2e 回归网 |
| `6b754c3` | fix: 强制使用 HTTP/1.1 解决金蝶 WebAPI 502 问题 |
| `30b2a19` | feat: 新增 4 个 SQL Server 探查工具 |

完整历史见 [GitHub Commits](https://github.com/WaHaiLong/KingdeeMCP/commits/main)。
