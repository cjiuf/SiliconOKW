# 定点标度审计（2026-11-20）

## 为什么做这个

燃油那个 bug（`CCountryFuelStatus+0x08` 是 **Q15 = 32768**，代码却按 1e5 除）说明一件事：

> **标度错了，绝对值全错，但比值仍然是对的。**

`fuel_ratio` 当时是 0.72089，引擎 0.72086 —— 比值正确完全不能证明标度正确。所以凡是只靠"比例/趋势/自洽"验证过的读点，都要重新用**绝对值**对一口。

审计方法只有两种：

1. **有引擎真值** → 控制台 `get_var`（注意参数化变量，见 `DYNAMIC_VARS_PRACTICAL.md`）；
2. **没有引擎真值** → 用游戏文件里的硬常量对拍（建筑造价、装备造价、修正值、基础产能）。

## 结论表

| 读点 | 字段 | 标度 | 怎么验的 | 结论 |
|---|---|---|---|---|
| `api.py` 工厂三兄弟 | `EC_CIV/MIL/NAVAL_FACTORIES` | 1e5 | 各州建筑求和 46 / 32 / 13 | ✅ |
| `api.py` 政治点数 | `PO_POLITICAL_POWER` | 1e5 | `get_var political_power` = 130 | ✅ |
| `api.py` 党派支持率 | 党对象 `+0x88` | 1e5×100 | `get_var highest_party_popularity` = 0.40 | ✅ |
| `api.py` 稳定度基数 | `CO_STABILITY_BASE` | 1e5 | `get_var stability` = 0.81（基数 0.75 + 党派 0.06） | ✅ |
| `api.py` 战争支持基数 | `CO_WARSUPPORT_BASE` | 1e5 | **无引擎对照**（`war_support` 不是动态变量，`get_var` 回 0 是"名字不存在"） | ⚠️ 0.30 + `war_support_factor` 0.05 = 0.35，按 HOI4 的加法约定 |
| `api.py` 核弹 | `CO_NUKES` → `+0x18` | **1e8** | 控制台 `nukes 3` → 读回 3 → 写回 0 | ✅ |
| `api.py` 燃油 / 油箱容量 | `CO_FUEL_STATUS+0x08/+0x10` | **Q15 32768** | `get_var fuel_k` 352.87 / `max_fuel_k` 489.5 | ✅（本轮修正） |
| `api.py` 全部修正值 | 修正数组 | 1e5 | `political_power_daily` 2.324 = 2.0×(1+0.162)，`command_power_daily` 0.33091 同理 | ✅ |
| `game.py` 战略资源 | `CCountryResources+0x60` 步长 0x10 | 1e5 | `get_var resource_produced@steel` = **304**、`@aluminium` = **217**；本读取器同为 304 / 217 | ✅ |
| `game.py` 产线单位成本 | `ML_UNIT_COST` | 1e5 | 步兵装备 0.5 IC，与产线自洽（见下一行） | ✅ |
| `game.py` 产线日产出 | `ML_OUTPUT` | 1e5 | 单船坞线读 2.935 = 2.5 基数 × 1.174，而 `industrial_capacity_dockyard_factor` 正好 **+0.174** | ✅ 但**单位是 IC/天，不是件/天** |
| `game.py` 产线 `+0x38` | `ML_EFF_COST` | 1e5 | 四条同价（440）潜艇线读 42.31 / 438.31 / 394.31 / 350.31，全是"在建的那一艘已投入多少 IC" | ✅ 但**不是成本**，已改名 `unit_progress_ic` |
| `game.py` 建造队列 | `BL_PROGRESS/COST/RATE` | 1e5 | 民工造价 `cost_ic` = 10800.0，与游戏里民工造价完全一致；`days_left` 与之自洽 | ✅ |
| `game.py` 装备库存 | 库存数组 | 1e5 | `get_var num_equipment@infantry_equipment` = 903 = 库存表步兵行 | ✅ |
| `game.py` 师携带装备 | `CArmy+0x368` | 1e5 | `get_var num_equipment_in_armies@*` 六项全对（24900 / 889 / 346 / 605 / 692 / 0） | ✅ |
| `game.py` 指挥点 | `CO_COMMAND_POWER` | 1e5 | `get_var command_power` = 14.11539 | ✅ |
| `game.py` 科技进度 / 研究速度 | `TE_PROGRESS` / `RS_SPEED` | 1e5 | **无引擎对照** | ⚠️ 未验 |
| `diplomacy.py` 战争目标计时 | `+0x18/+0x20/+0x28` | 1e5 | **无引擎对照** | ⚠️ 未验 |
| `game.py` 州抵抗 / 顺从 | `ST_RESISTANCE/COMPLIANCE` | 1e5 | 本版本读出来是 `null` | n/a |

## 这轮审计抓到的问题

1. **`effective_cost` 名不副实**（已改名 `unit_progress_ic`）
   它读的是"当前在建单位已投入的 IC"，不是"修正后的单价"。所有观察值都 ≤ `unit_cost`，四条潜艇线就是四个不同完工阶段的船体。名字起错会让人把它当价格用。

2. **`output_per_day` 是 IC/天，不是件/天**（已加 `output_units_per_day`）
   验证链条很硬：单船坞线 2.935 IC/天 ÷ 2.5（船坞基础产能）= 1.174，而 `industrial_capacity_dockyard_factor` 在这个存档里正好是 **+0.174**。两个独立来源对上，说明标度没错、只是单位被误读。

3. **reload 后的陈旧对象**
   中途有一次 `hoi4_resources` 读出 34 / 184 / 254（正确值 41 / 217 / 304，全都差 0.83 倍），紧接着再读就正常了。**原因没有查实**（怀疑是 `mcp_load` 之后那次调用拿到的是旧对象/旧指针，但没验证）。**在那之前，先按"reload 后第一次读数不可信"处理：复读一次再采信。**

4. **燃油（本轮之前已修）**
   最典型的一例：Q15 被当成 1e5，整体偏低 3.0518 倍，而比值一直是对的。

## 没有引擎真值的读点怎么办

- 战争支持：`war_support` 不在动态变量注册表里，`get_var` 对它回 0 是"名字不存在"，不是"值是 0"。目前按 HOI4 的加法约定算（基数 + 修正），**没有独立验证**，用的时候要记住这一点。
- 科技进度、战争目标计时：同理，只能靠"结构语义"推断，没有绝对值对拍。

这两处如果要硬验，只能等有实际进度/计时的时候，用"天数 × 每天进度"这种弱对照，或者反汇编引擎的 `get_var` 分支。

## 结论

- 有引擎真值的读点：**全部对上**（含本轮新发现的资源口径）。
- 标度类错误目前只有燃油一例，已修。
- 语义类错误两例（`effective_cost`、`output_per_day` 的单位），已修。
- 剩下两个 ⚠️ 是无真值可对，已在代码注释里写明，不假装验过。
