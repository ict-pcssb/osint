# 贵州旅行单智能体 M01 开发与测试报告

2026 年 11 月 26 日，北京时间。**M01 按用户确认的调整后口径通过。** 桌面源码快照已确认为开发基线，现有 Python 3.13.11 环境的依赖已锁定并复核，调整后的 M1-08 已通过。开发停在 M01。

## 用户确认的验收调整与复测

- 以报告第 1 节的桌面源码快照为准。该目录没有 Git 元数据，使用已记录的源码指纹识别版本。
- [本地依赖锁](../../requirements-lock-windows-py313.json)记录项目实际使用的 Windows Python 3.13.11 环境、37 个已安装包的精确版本及航班包源码提交。配置审计逐项比较锁与当前环境，另用 `pip check` 验证包间依赖。两项均退出 0。锁定和复查只读取已安装的软件包，本轮没有安装或升级。此锁针对当前 Windows 运行环境，尚未做全新环境重建。
- M1-08 现在验收临时 SQLite 测试底座。复测验证 A/B 用相同记录 ID 时仍各读各的数据、跨账号读写被拒绝、两次运行均在结束后回滚并清空测试写入。
- **M03 必过项。** 使用项目实际登录入口和非生产数据库，以 A/B 两个测试账号验证应用层跨账号读写被拒绝、账号本人可以正常保存和读取、测试写入可回滚且复跑无残留。该项须有实际命令和证据，未通过不能放行 M03。项目当前尚无这些入口，本轮未运行此项。

| 收尾复测 | PASS | FAIL | BLOCKED | SKIPPED | 退出码 |
| --- | ---: | ---: | ---: | ---: | ---: |
| M01 离线底座，含调整后的 M1-08 | 8 | 0 | 0 | 0 | 0 |
| 现有搜索回归 | 5 | 0 | 0 | 0 | 0 |

从项目根目录用现有解释器执行以下命令。旧项目回归、真实模型、供应商和浏览器结果沿用首次测试记录，本轮未重跑。

```powershell
& .\.venv-flight\Scripts\python.exe -B scripts/travel_agent/audit_baseline.py --output docs/travel_agent/capability_manifest.json
& .\.venv-flight\Scripts\python.exe -B scripts/travel_agent/run_module.py M01 --report docs/travel_agent/evidence/m01-closeout/report.json
& .\.venv-flight\Scripts\python.exe -B scripts/travel_agent/run_module.py M01 --suite new-regression --report docs/travel_agent/evidence/new-regression-closeout/report.json
& .\.venv-flight\Scripts\python.exe -I -B -m pip check
```

收尾新增锁文件及两个复测证据目录，更新 M1 测试入口、锁审计和能力清单。若撤回本轮收尾，只撤回这些变更及本次报告修订，保留首次测试记录。

## 首次执行记录

下文保留首次执行时的范围和证据。旧口径的阻塞和退出码属于当时的结果，当前验收以本页开头的用户确认与复测结果为准。

## 每个模块的测试记录

M01 至 M12 每个模块执行后都在项目的 docs/travel_agent 目录交付可直接阅读的测试记录。记录每项用例的命令、环境、预期、实际、状态与原因，写明历史回归结果、验收结论及回滚范围。JSON 和日志随文档作为证据，后续复测在模块文档中补记结果。M01 的测试记录即本文件。


## 1 模块范围与修改目标

- 唯一修改目标为 `C:/Users/谭友俊/Desktop/ict/guizhou-travel-agent`。这是用户指定文档所在的可读源码快照。目录无 Git 元数据，分支、HEAD、上游来源和未提交差异待确认。按 M01 第六章记录源码指纹 `5bb8b6a7ff1134d8868cd7ecd703059157d6cb761f85d9798317934766bdd23c`。指纹覆盖原有 Python 入口、原有测试、提示词、依赖声明、忽略规则及 MCP 脱敏摘要。逐文件 SHA-256 见能力清单。
- `C:/Users/谭友俊/Desktop/ict/samples-1124` 是只读参考。分支 `feat/ui-reference-samples-1124`，提交 `3a612de1c924b67b5ec554b50080c106ab01a3d4`。开始与结束均发现相同 3 个既有未跟踪文件。参考回归采用提交导出的独立副本。副本包含已提交源码，未纳入上述未跟踪文件、原项目 `.env` 或 `.git`。ZIP SHA-256 为 `06c773357f3addded8a737ba0d6277fc175b1d55acca555490d9e9f33f61b8c1`。
- 以 M01 全文、00A 第一章与第四章第 1–3 节、00B 第二章第 1、4–7 节、00D 安全、成本、观测及回滚要求为验收依据。00C 的输出示例用于理解后续接口，当前只登记对应入口缺口。

## 2 原文与实际结果对照

| 原文关卡 | 实际结果 | 状态 |
| --- | --- | --- |
| M1-01 六种证据来源，历史与本轮独立 | 核对注册、握手、有效业务数据、旧项目、新 Agent 联通、目标环境。只有握手时业务有效字段保持未证实。旧 32/35 和 48/48 不计入新 Agent 通过数。 | PASS |
| M1-02 默认离线、工具替身、次数与时限 | 固定天气成功返回。实际项目 HTTP 客户端、模型传输入口、DNS、直接套接字和子进程触发发送前拦截。提供工具/供应商分开计数、统一重试和回退预算、截止检查，以及免费、已有额度、收费、未知四类。所有真实调用模式均关闭。 | PASS |
| M1-03 北京时间 | UTC 和 UTC+8 使用独立 Python 进程分别验证，宿主偏移为 0 和 +08:00。固定时间为 2026-11-26 10:00 +08:00，两次均得到明天 11 月 27 日、周六 11 月 28 日。 | PASS |
| M1-04 测试库守卫、写入 spy、脱敏 | 非测试 DSN、路径越界、未声明 SQLite 连接及未声明写入被拦截。假认证头、Key、URL 查询、手机号和姓名均从输出中移除，保留 trace ID 和错误类别。Web 构建产物和 API 响应扫描待实际入口。 | PASS |
| M1-05 四态统计 | 固定用例验证 PASS、FAIL、BLOCKED、SKIPPED，以及缺条件阻断、未执行数和分母。当前报告的正式跳过数为 0。 | PASS |
| M1-06 版本、MCP 路径、参数审计 | 固定错误夹具验证声明/锁/运行时漂移、缺 Windows 解释器、缺参数映射。基线审计另列真实环境缺口，搜索和航班本地声明可读，网站协议握手与供应商语义均待验。 | PASS |
| M1-07 新旧回归分离 | 新项目搜索 5/5。旧副本回归收集 34 个对象，30 PASS、4 个模块加载 BLOCKED。旧固定回放 48/48 通过。详见第 7 节。 | 新项目 PASS，旧参考部分 BLOCKED |
| M1-08 应用数据库及账号隔离 | 实际应用鉴权和数据库缺失，正式集成 0 项执行。独立磁盘 SQLite 夹具通过 A/B 同 ID 读写、跨账号拒绝和同一场景两次事务回滚。 | 实际应用 BLOCKED，夹具 PASS |
| 00D 可观测性 | 脱敏 trace 夹具区分完成、部分完成、失败、提交响应丢失，定位阶段、工具、耗时、重试、证据与写入。当前业务入口仍待接入观测。 | 夹具 PASS |
| 00D 版本可复现 | 已记录模型 ID、提示和适配器哈希、工具输入 Schema 哈希、参考 POI 目录哈希。传递依赖未锁定，最终 Skill 注册表、业务 Schema、规则版本及目标环境待相应阶段核验。 | BLOCKED |

## 3 新增文件与路径映射

| 路径 | 用途 |
| --- | --- |
| `tests/travel_agent/m01/test_baseline.py` | M1-01 和 M1-06 的证据与配置断言 |
| `tests/travel_agent/m01/test_guards.py` | M1-02、M1-03、M1-04 的网络、时间、存储和脱敏断言 |
| `tests/travel_agent/m01/test_reporting.py` | M1-05 四态报告断言 |
| `tests/travel_agent/m01/test_observability.py` | 四类追踪夹具断言 |
| `tests/travel_agent/m01/test_store_fixture.py` | 隔离 SQLite 事务夹具 |
| `tests/travel_agent/support/{audit,guards,reporting,store}.py` | 测试支持代码，固定输入放在对应测试内 |
| `scripts/travel_agent/{run_module,audit_baseline}.py` | 模块执行器、分层报告和只读配置审计 |
| `docs/travel_agent/{baseline.md,capability_manifest.json}` | 当前报告和能力版本清单 |
| `docs/travel_agent/evidence/` | 本轮脱敏报告、拦截日志、执行环境、退出码及两种宿主时区结果 |

当前 Web、API、存储和业务契约的建议目录仍为待映射目标。现有代码中可定位的入口如下。

| 职责 | 新项目入口 | 只读参考或实际缺口 |
| --- | --- | --- |
| 主 Agent、聊天和模型 | `travel_agent.py:main`、`Conversation.ask`、`complete` | Python CLI，主模型 `qwen4-max`；现有模型路径使用 `DASHSCOPE_API_KEY` 与 `DASHSCOPE_BASE_URL`。后者未设置时，现有代码可能从旧项目配置读取服务地址。 |
| 天气 | `travel_agent.py:weather`、`get_destination_weather` | 主 Agent 中有天气函数。POI 目录目前从兄弟项目读取。 |
| 搜索 | `travel_search_mcp.py:search_travel_sources` | 独立 stdio 服务，Exa 后 AnySearch；主 Agent 当前仅声明天气工具。 |
| 航班 | `flights_mcp/server.py:search_flights` 与 `resolve_airports_tool` | 独立 stdio 服务。当前进程内可读工具名 `search_flights`、`resolve_airports`。 |
| Parallel 搜索 | `.mcp.json` 的 `parallel-search` | 单独的远程登记。此次未执行握手，工具名来自历史手册。 |
| Web 启动、首页、渲染 | 新快照无 Web 启动入口或前端 | 旧项目 `server.py:main`、`index` 与 `static/index.html` 仅作参考。 |
| 行程持久化、账户鉴权 | 新快照无应用实现或数据库配置 | 旧项目 `travel_store.py`、`mysql_store.py`、`web_auth.py` 仅作参考。 |
| 其他 Skill 与测试 | 新项目现有测试 `test_travel_search_mcp.py` | 新旧 Skill 路径及禁用情况见能力清单。 |

## 4 测试底座与环境核对

搜索和航班登记均指向 `.venv-flight/Scripts/python.exe`，Python 3.13.11。搜索以 `travel_search_mcp.py` 启动，航班以 `-m flights_mcp` 启动。两个本机绝对解释器路径均可用，换主机需重新核对。搜索直接版本 MCP 2.2.0、httpx2 2.16.2、Pydantic 2.13.5 与声明一致。航班包 1.1.0 从本机 Git 源目录安装，安装目录内 10 个 Python 源文件均与干净源码逐字节一致，源码 HEAD 与声明 `d96ef277d7473929f629526963e3867c003b6a8f` 一致。Windows 实际 tzdata 为 2026.6，声明未固定版本。未发现独立传递依赖锁文件。

另一套 `.venv` 使用 Python 3.10.19、MCP 1.30.0、httpx2 2.13.1、Pydantic 2.12.3。两套环境的 MCP 返回字段分别为 `structuredContent/isError` 和 `structured_content/is_error`，报告在能力清单中记录配置实际选用的 2.x 环境。这解释了 T23 和 T199 的差异。历史 S103 中已装 agent-framework-core 1.26.0 与旧项目声明 1.19.0 的差异属于历史环境。本轮旧副本选用已有的 Python 3.12.15 环境，其中 agent-framework-core 1.19.0、MCP 1.30.0、OpenAI 2.54.0 与旧项目相应声明一致。旧 Node 路书锁存在，本轮未运行 Node。

离线执行器启动时清除未列入必要系统变量的环境项。测试在已审阅的 Python 入口上拦截网络与子进程。Windows asyncio 的内部 self-pipe 单独限制为库自身的回环 socketpair。该守卫用于受审阅的测试进程，操作系统级网络隔离及应用运行时接入留待目标环境。SQLite 夹具只使用独立临时目录，限制文件名和路径。观测输出移除聊天原文与凭据字段。生产数据库、模型和供应商调用计数均为 0。

## 5 实际命令

下列命令的执行日期为 2026-11-26，工作目录为新项目根，除旧套件逻辑工作目录由执行器切换到 ZIP 副本。各次具体时间、解释器绝对路径、命令、测试范围和退出码见对应 JSON。

```powershell
& .\.venv-flight\Scripts\python.exe -B scripts/travel_agent/run_module.py M01 --report docs/travel_agent/evidence/m01/report.json
& .\.venv-flight\Scripts\python.exe -B scripts/travel_agent/run_module.py M01 --suite new-regression --report docs/travel_agent/evidence/new-regression/report.json
& .\.venv-flight\Scripts\python.exe -B scripts/travel_agent/audit_baseline.py --output docs/travel_agent/capability_manifest.json
foreach ($mode in 'model','live','e2e') {
    & .\.venv-flight\Scripts\python.exe -B scripts/travel_agent/run_module.py M01 --mode $mode --report "docs/travel_agent/evidence/$mode/report.json"
}
```

先写断言后执行 `unittest discover`，初跑因支持代码尚未创建返回 1。之后逐组验证证据审计、报告、守卫、SQLite 夹具及观测测试。两种宿主时区用 `TZ=UTC0` 与 `TZ=CST-8`，将 `tests/travel_agent/m01` 与 `tests/travel_agent` 加入 `PYTHONPATH` 后，分别执行 `python -B -m unittest test_guards.GuardTests.test_beijing_clock_is_host_independent -v`，均返回 0。

M01 第六章所列 Git 只读命令已从新项目根各自执行。 `status --short --branch`、三条 `rev-parse`、`log -1 --format="%H %cI"` 及按锁和声明文件限定的 `ls-files` 均返回 128，原因是目录没有 Git 元数据。 `git diff --stat` 返回 129，原因相同。旧项目 Git 检查和 ZIP 导出返回 0。

旧测试的实际工作副本位于既有 Python 3.12.15 有写权限的工作区临时目录。首次放在系统 TEMP 的副本无法写入，确认权限差异后改用工作区目录。下面记录最终实际命令。执行器验证 ZIP 内提交标记和已跟踪文件，再以 `unittest discover -s tests/travel_core -v` 的收集规则运行旧回归，以等价的默认 `__main__` 入口运行 `scripts/travel_eval/evaluate.py`。

```powershell
$legacyPython = 'C:/Users/谭友俊/Doubao/chats/2026-10-01/new-chat/.tmp/ict-online-pinned/Scripts/python.exe'
$legacy = 'C:/Users/谭友俊/Doubao/chats/2026-10-01/new-chat/.tmp/guizhou-m01-legacy-7c161ad83dbb43078d086e835029d3ca'
& $legacyPython -B scripts/travel_agent/run_module.py M01 --suite legacy-unit --legacy-copy "$legacy/source" --report "$legacy/unit/report.json"
& $legacyPython -B scripts/travel_agent/run_module.py M01 --suite legacy-replay --legacy-copy "$legacy/source" --report "$legacy/replay/report.json"
```

## 6 本轮测试结果

| 测试层 | 计划 | 执行 | PASS | FAIL | BLOCKED | SKIPPED | 退出码 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| M01 单元、静态、报告契约及本地存储集成 | 9 | 8 | 8 | 0 | 1 | 0 | 2 |
| 新项目历史功能回归 | 5 | 5 | 5 | 0 | 0 | 0 | 0 |
| 旧项目历史回归参考 | 34 | 30 | 30 | 0 | 4 | 0 | 2 |
| 旧项目固定回放参考 | 48 | 48 | 48 | 0 | 0 | 0 | 0 |
| 真实模型门禁 | 1 | 0 | 0 | 0 | 1 | 0 | 2 |
| 真实供应商只读门禁 | 1 | 0 | 0 | 0 | 1 | 0 | 2 |
| 浏览器端到端门禁 | 1 | 0 | 0 | 0 | 1 | 0 | 2 |

通过率的计划分母是计划数，已执行分母是 PASS + FAIL。旧项目回归的 34 是 unittest 收集对象数，其中四个是模块导入错误占位。真实模型与供应商门禁仅检查授权状态，浏览器门禁仅记录当前缺少页面与端到端入口。

## 7 历史功能回归与证据

- 本轮新项目 5 项覆盖 Exa 命中、发布日期区分、超时回退、双错误以及域名/排除链接。数据来自原测试中的模拟工具，供应商层未执行。
- 旧副本的 `test_account_api`、`test_account_security`、`test_guidebook_fallback`、`test_report_format_api` 因缺 `PIL` 无法导入。旧要求为 `Pillow>=12,<14`。未指定专用 MySQL 实例，当前 Docker Linux 引擎不可连接，旧账号测试也缺数据库条件。其他 30 项通过。独立固定回放 48 条全部通过。
- 历史资料中 32/35 和 48/48 属于当时的运行。历史 CLI 青岩记录可定位到 `qwen4-max` 和一次 Open-Meteo 工具消息，尚缺当前运行、统一 trace ID、UI 和验收断言。其他供应商的历史业务记录与握手描述分别保留在能力清单。

## 8 余项与放行阻塞

1. **应用实际存储和鉴权。** 需要含实际鉴权与持久化入口的目标源码，以及该应用对应的非生产数据库、两个测试身份或创建夹具的权限。取得材料后重跑 M1-08。
2. **版本可复现。** 需要确认目标分支、提交或确认以本报告签名的快照为准，并提供对应的依赖锁。当前两项声明文件已经校验哈希，锁定传递依赖仍待补齐。
3. **旧参考测试完整度。** 旧测试所需 Pillow 环境和专用 MySQL 未准备。它们影响历史参考覆盖。当前未安装依赖、发起付费或供应商调用、部署服务，未执行 `travel_agent.py --qingyan-test`。
4. **配置和观测边界。** 两项本机 MCP 解释器采用绝对路径。现有 CLI 可输出包含对话的调试 trace，应用侧尚未接入本轮的脱敏设施。测试日志中只存脱敏结构。浏览器和供应商端到端留待对应模块。

## 9 回滚范围

只撤回本报告第 3 节列出的新增文件和本轮生成的隔离副本。测试 SQLite 连接已关闭，临时库随夹具清理。独立旧副本输出仅在工作区临时目录，本轮脱敏报告保存在新项目 `docs/travel_agent/evidence/`。回滚时可保留报告用于复现。无需重置工作树、还原数据库或修改旧项目。

## 10 首次验收结论

**BLOCKED。** 六项底座断言、两种宿主时区验证、本地 SQLite 夹具、脱敏追踪夹具以及现有搜索回归均通过。M1-08 应用实库测试与完整依赖锁仍是阻塞。停在 M01，等待用户验收确认。

