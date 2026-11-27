# 安装与挂载（SiliconOKW / Windows HOI4 1.19.5.0）

这份文档只讲**怎么把它跑起来**。项目介绍与未完成功能见 [`README.md`](README.md)。

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11 x64 |
| Python | 3.11+（`python --version` 能跑即可） |
| 游戏 | Hearts of Iron IV **1.19.5.0**（Steam），进程名 `hoi4.exe` |
| 权限 | 与游戏**同一个用户**运行；不需要管理员（同用户即可 OpenProcess） |
| 依赖 | `numpy`、`Pillow`（见 `requirements.txt`） |

```powershell
cd <解压目录>
python -m pip install -r requirements.txt
```

游戏路径默认从 Steam 库自动发现；装在非标准位置就设环境变量：

```powershell
setx HOI4_PATH "D:\SteamLibrary\steamapps\common\Hearts of Iron IV"
```

---

## 2. 目录

```
calling/          核心调用层 + 命令表/符号表
mcp/server.py     MCP 服务端（Full 174 合规工具 / --compact 5 工具模式）
clausewitz.py     入口：python clausewitz.py serve [--compact]
native/           可选 Rust 扫描扩展与偏移校验源码
research/         PE 逆向工具链 + WINDOWS_1.19.5.md 反汇编笔记 + rtti_win.pkl + 回归测试
docs/             策略提示词与技术说明
```

> `calling/objindex.pkl`（50 MB）、`loc.pkl`、`evdb_win.pkl` 这些**故意没带**，
> 首次用到时会自动重建（几秒到几十秒）。

---

## 3. 挂到 MCP 客户端

服务端是**标准 stdio JSON-RPC**，入口统一走 `clausewitz.py`：

```powershell
python <目录>\clausewitz.py serve
```

### 3.1 Claude Code

```powershell
claude mcp add hoi4 -- python "D:\path\to\SiliconOKW\clausewitz.py" serve --compact
```

或直接写 `%USERPROFILE%\.claude.json`（Full 模式去掉 `"--compact"` 即可）：

```json
{
  "mcpServers": {
    "hoi4": {
      "command": "python",
      "args": ["D:\\path\\to\\SiliconOKW\\clausewitz.py", "serve", "--compact"],
      "env": { "HOI4_PATH": "D:\\SteamLibrary\\steamapps\\common\\Hearts of Iron IV" }
    }
  }
}
```

### 3.2 Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`，内容同上。

### 3.3 其它客户端（通用）

```json
{
  "name": "hoi4",
  "transport": "stdio",
  "command": "python",
  "args": ["<目录>\\clausewitz.py", "serve"],
  "env": {
    "HOI4_PATH": "<游戏目录>",
    "CLAUSEWITZ_CACHE": "<目录>\\calling"
  }
}
```

仓库里也放了模板：`mcp/config/windows-claude-code.json`、`mcp/config/windows-generic.json`。

改完配置**重启客户端**，工具列表里应出现 `hoi4_status`、`hoi4_overview` 等。

---

## 4. 第一次跑：按这个顺序自检

游戏先启动并**读进存档**（主菜单里没有国家对象，工具会报 `could not determine
the played country`）。

| 步骤 | 调用 | 期望 |
|---|---|---|
| 1 | `hoi4_status` | 有 `pid` / `image_base` / `version: 1.19.5.0` / `player: GER` |
| 2 | `hoi4_division_selftest` | `ok: true`，`identity_count == tag2index_count` |
| 3 | `hoi4_research_available` | 返回科技列表（第一次可能要等 10~60 秒建库） |
| 4 | `hoi4_overview` | 有 alerts、工厂数、师数、生产/建设队列 |
| 5 | `hoi4_date` | `source` 应为 `clock`；不是就先看第 6 节 |

⚠️ **读档后、解暂停前**必须先 `hoi4_purge_orders`，确认没有 0 师订单。

---

## 5. 必需的操作纪律

1. **游戏窗口必须在前台且未最小化。** 最小化会让整个模拟冻结（实测 5 小时墙钟
   = 0 游戏日）；而且注入要劫持渲染线程，后台窗口会死在 D3D 恢复里。
2. **一次只发一个写命令**，发完先 `hoi4_status`（纯读、极便宜）确认进程还活着。
3. **`-32001 Request timed out` 不等于失败**，务必读回验证再决定是否重发。
4. 换档 / 读档 / 重开游戏之前，先 `hoi4_clear_front_directive()`，确认
   `front_directives` 的 `armed_count == 0`、`worker.state == "idle"`。
5. 控制台命令、`instant_wargoal`、裸 `g.call` 都是作弊/危险路径，正常对局不要用。

---

## 6. 常见故障

| 现象 | 原因 / 处理 |
|---|---|
| `hoi4 process not found` | 游戏没开，或进程名不是 `hoi4.exe`；也可能是别的用户启动的 |
| `could not determine the played country` | 还在主菜单/载入中，读档完成后再调 |
| `VirtualAllocEx failed, GetLastError=5` | **进程已经死了**，不是权限问题，别重试 |
| `ReadProcessMemory(...) 299` | `ERROR_PARTIAL_COPY` = 目标进程已退出 |
| `hoi4_date.source` 不是 `clock` | 时钟地址没锁定，日期可能滞后；别拿它做日期门 |
| `pause_in_hours` 报 `stalled` / `clock_unreliable` | 窗口最小化，或 game.log 成批刷新导致跳变；降到 1~2 速重试 |
| 工具列表是空的 | 客户端没重启，或 `clausewitz.py` 路径写错（要用绝对路径） |
| 中文乱码 | 终端编码问题，工具输出本身是 UTF-8 |

---

## 7. 卸载

不写注册表、不改游戏文件。删掉目录、移除客户端配置里的 `hoi4` 条目即可。
运行期只会在 `calling/` 下留几个 json/pkl 缓存（已 gitignore）。
