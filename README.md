# SiliconOKW — Hearts of Iron IV (1.19.5.0) Windows 原生 MCP 自动化工具集

<p align="center">
  <b>简体中文</b> · <a href="README_EN.md">English</a>
</p>

> [!IMPORTANT]
> **项目说明**
> - 本仓库大部分代码靠 **vibe coding** 完成。
> - **德国史实线已经过充分验证**。
> - 如有发现工具 Bug 或特定游戏版本的内存偏移量错误，**欢迎提交 Issue 与 PR**，会尽量修复。

<p align="center">
  <b>面向 Windows x64 <code>Hearts of Iron IV 1.19.5.0</code> 的原生内存观测与合规指令自动化平台</b><br>
  <sub>Win32 纯只读实时采集 · 引擎原生 <code>CCommandQueue</code> 合规发令 · 双模式 MCP（Full 174 合规工具 / Compact 5 工具）</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/HOI4-1.19.5.0%20(Operation%20Postern)-2b3440?style=for-the-badge">
  <img src="https://img.shields.io/badge/Platform-Windows%20x64%20PE-3d6b4a?style=for-the-badge">
  <img src="https://img.shields.io/badge/MCP-Full%20174%20%7C%20Compact%205-c8a04a?style=for-the-badge">
  <img src="https://img.shields.io/badge/Acceleration-Rust%20PyO3%20%2B%20Rayon-a85520?style=for-the-badge">
  <img src="https://img.shields.io/badge/License-MIT-3d6b4a?style=for-the-badge">
</p>

---

## 1. 项目简介

**SiliconOKW** 是一套面向 **Windows Steam 版《钢铁雄心 IV》`1.19.5.0`（`Operation Postern v1.19.5.0.7c6c`）** 的 MCP 自动化与内存调用工具集。

本项目通过 **Win32 `ReadProcessMemory` 零停表纯只读采集** 与 **游戏原生 `CCommandQueue` 玩家命令队列发令**，让大模型像真实玩家一样读取战局并发出合规指令：

- **非作弊路径**：所有操作均构造游戏原生 `CCommand` 并通过引擎自身的 `CanExecute` 校验，禁用控制台与键鼠画面点击注入。
- **双模式 MCP 服务端**：支持 **Full 模式（174 个细粒度合规工具，`hoi4_debug_*` 默认关闭）** 与 **Compact 5 工具极简模式（Schema < 1k tokens）**，零第三方 MCP SDK 依赖。
- **可选 Rust 原生加速**：内置 `native/hoi4_fastmem`（PyO3 + Rayon 多核 Win32 内存扫描），缺失时自动无缝回落纯 Python/numpy 路径。

---

## 2. 还没做的功能

以下模块目前尚未实现或仅覆盖部分能力，欢迎 PR 补齐：

- **情报机构**（创建、升级、特工招募与情报行动）
- **实验设施**（特别工程项目 / 设施建设与科研）
- **部分外交选项**（目前重点覆盖了宣战、造借口、贸易等核心路径，通用多态外交动作尚未全量封装）

---

## 3. 核心架构

```text
┌──────────────────────────────────────────────────────────────────────┐
│  MCP Client (Claude Code / Claude Desktop / Cursor / Custom Agent)  │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ JSON-RPC 2.0 over stdio
┌─────────────────────────────────▼────────────────────────────────────┐
│  clausewitz.py / mcp/server.py                                       │
│    • Full 模式：174 个细粒度合规 hoi4_* 工具                          │
│    • Compact 模式：5 工具 + turnengine（简报 / Plan DSL / 红点门禁）   │
└──────────────────┬───────────────────────────────┬───────────────────┘
                   │ 纯只读观测                     │ 原生命令队列发令
┌──────────────────▼────────────────┐ ┌────────────▼───────────────────┐
│  calling/ (核心调用层)             │ │  calling/cmdwin.py & winject   │
│    • mem.py / fastmem.py (Rust)   │ │    • 原生 ctor + CanExecute    │
│    • game.py / offsets_win.py     │ │    • fake_vtable 防跨堆释放     │
│    • armyops_win / airops_win     │ │    • post_drained 回读核验      │
│    • navalops_win / trade_win     │ │                                │
│    • events_win / peacewin        │ │                                │
│    • combat / movemicro / frontd  │ │                                │
│    • macros / housekeep / reddots │ │                                │
└──────────────────┬────────────────┘ └────────────┬───────────────────┘
                   │ ReadProcessMemory              │ CCommandQueue::Post
┌──────────────────▼───────────────────────────────▼───────────────────┐
│  hoi4.exe  (Hearts of Iron IV 1.19.5.0 Windows x64 PE)              │
└──────────────────────────────────────────────────────────────────────┘
```

### 已实现的兜底与工程护栏
- **代码级运营兜底**：内置红点门禁（`reddots`）、`housekeep` 常规管家、`rebalance_production` 产能配平、`upgrade_template_preset` 换编挪厂联动与 `strategic_health` 战略体检，防止模型空转或摆烂。
- **战斗计划与安全防崩**：支持原生 `CompletePlan` / `COrderNewFrontCommand`（`0xD57D90` 敌对连通区匹配）、单条/整军转守（`hoi4_execute_plan(executing=false)`）、按敌国隔离前线、`hoi4_move_divisions(spread=true)` 自动分散撤军、0 师空订单清理（防 `0xB2E62E` 崩溃）与跨堆释放防护（`fake_vtable`）。

---

## 4. 快速开始

详细安装与排障请参见 [INSTALL.md](INSTALL.md)。

### 1) 环境要求
- Windows 10 / 11 x64
- Python 3.11+
- Steam 版 Hearts of Iron IV **`1.19.5.0`**（进程名 `hoi4.exe`）
- 与游戏运行在同一 Windows 用户权限级别（同级无需管理员）

### 2) 安装依赖与自检

```powershell
python -m pip install -r requirements.txt

# 游戏未安装在默认 Steam 目录时指定环境变量
$env:HOI4_PATH = "D:\SteamLibrary\steamapps\common\Hearts of Iron IV"

# 环境自检
python clausewitz.py doctor

# 游戏读档后纯只读查看当前战局
python clausewitz.py status
```

### 3) 挂载到 MCP 客户端

- **Compact 5 工具极简模式（推荐用于长局运营）**：
  ```powershell
  claude mcp add hoi4 -- python "D:\path\to\SiliconOKW\clausewitz.py" serve --compact
  ```
- **Full 174 工具全量模式**（全部 `hoi4_debug_*` 调试/作弊工具默认禁用且隐藏；仅限本地逆向调试时可设 `HOI4_MCP_ALLOW_DEBUG=1`，而 `hoi4_debug_eval` 需同时显式设置 `HOI4_MCP_ALLOW_EVAL=1`）：
  ```powershell
  claude mcp add hoi4 -- python "D:\path\to\SiliconOKW\clausewitz.py" serve
  ```
- 更多 JSON 配置模板见 [mcp/config/](mcp/config/)。

### 4) 可选：构建 Rust 内存扫描加速扩展

```powershell
powershell -File native/hoi4_fastmem/build_win.ps1
```

不编译也能运行，`calling/fastmem.py` 会自动回落到纯 Python/numpy 路径。

### 5) 使用前一定要看

1. **保持游戏窗口未最小化**：HOI4 最小化后模拟时钟会冻结，且 D3D 线程挂起恢复存在风险；发令前可先读取窗口状态。
2. **读档后先清理空订单**：读档后、解暂停前执行 `hoi4_purge_orders`，消除历史遗留的 0 师订单（防 `0xB2E62E` 崩溃）。
3. **写命令串行发送并回读核验**：遇 `-32001` 超时不代表失败，先用纯只读工具核验状态再决定是否重试。
4. **切档/重开前先停后台微操**：先调用 `hoi4_clear_front_directive()`，确认 `frontd` 处于 `idle` 状态。

---

## 5. 目录结构

| 路径 | 说明 |
|---|---|
| `clausewitz.py` | 统一入口：`serve`（`--compact`）、`doctor`、`status`、`brief`、`tools`、`tables`、`shell` |
| `calling/` | 核心内存读写、游戏对象模型、PE `1.19.5.0` 偏移表 `offsets_win.py`、命令表与运营兜底模块 |
| `mcp/` | 零依赖 stdio MCP 服务端 `server.py` 与 Windows 客户端配置模板 |
| `native/` | `hoi4_fastmem`（Rust Win32 扫描扩展）与 `hoi4_recalc`（PE 偏移校验工具） |
| `research/` | PE `1.19.5.0` 反汇编参考笔记 `WINDOWS_1.19.5.md`、离线反汇编工具链与回归测试 `research/windows/test_*.py` |
| `docs/` | 策略参考提示词 `STRATEGY_PROMPT.md` 与定点数/动态变量说明 |
| `tools/` | 独立分发压缩包构建脚本 `build_bundle.py` |

---

## 6. 离线回归测试

在 Windows 环境下无需启动游戏即可运行离线回归测试（提交 PR 前建议先跑一遍；其中依赖本地编译产物 `hoi4_recalc.exe` 或首次运行缓存 `techids.pkl` 的两项需先完成对应初始化）：

```powershell
python -c "import pathlib, subprocess, sys; ts=sorted(pathlib.Path('research/windows').glob('test_*.py')); rs=[(t.name, subprocess.run([sys.executable, str(t)]).returncode) for t in ts]; print(f'PASS {sum(r==0 for _,r in rs)}/{len(rs)}'); sys.exit(any(r!=0 for _,r in rs))"
```

---

## 7. 许可协议与免责声明

- 本项目采用 [MIT License](LICENSE) 开源。欢迎通过 Issue 反馈 Bug 或特定版本偏移问题，欢迎 PR 补全尚未实现的功能模块。
- **安全说明**：MCP 服务端默认关闭全部 `hoi4_debug_*` 调试工具（`hoi4_debug_eval` 需显式双重环境变量解锁）；仓库中附带的 `.pkl` 缓存均为基于本地游戏文件生成的离线索引，如有安全顾虑可直接删除并由脚本从你自己的游戏安装目录重建。
- **免责声明**：本项目仅用于**单机模式下的 AI 自动化研究与技术交流**，不包含《钢铁雄心 IV》游戏本体、DLC 解锁工具或任何受版权保护的游戏原始资源；严禁用于多人联机作弊或任何违反游戏服务条款的用途。使用本项目涉及进程内存读写，请提前备份存档并自行承担相应风险。
