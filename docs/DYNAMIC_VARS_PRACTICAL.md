# 正常游戏会用到的动态变量 — 覆盖清单

来源：`calling/dynvar.walk_registry()` 从 PE 注册表 `0x33DF1D0` 活走出的 **265 个
country 作用域变量**（与游戏写进 `documentation/dynamic_variables_documentation.md`
的是同一份）。这里只挑出**实际会拿来做决策**的那些，其余（约 200 个
`ai_strategy_*` / `ai_attitude_*` / `total_equipment_produced_*` / 列表型）不列。

图例：
- ✅ **native** — `hoi4_script_variable` 直接返回，且已交叉验证
- 🔶 **有源未实现** — 底层数据我们已经在读，只是还没接进 `script_variable`
- ❌ **无来源** — 需要新的反汇编

> **关于控制台 `get_var`（2026-11-20 最终结论）**
>
> 它**是**引擎的真值。输出形如 `name = 州名 (数字)` 时，**括号里的数字就是值**，
> 州名只是游戏把一个整数也拿去当州 id 渲染了。证据：`get_var num_ships` 回
> `上阿迪杰 (39)`，而我们逐舰队相加（12+10+2+8+6+1）正好 **39**；`num_core_states`
> 回 24，与 `29 州 − 5 个非核心` 完全一致。
>
> ⚠️ 但**工厂类**存在未解差异：`get_var num_of_{civilian,military,naval}_factories`
> 回 38/29/10，而我们按「各州建筑等级求和」得到 46/32/13（存档解析也是 46/32/13）。
> 差在哪还不清楚（可能引擎只算核心州、或只算未损坏/已完工的）。**这 3 个变量以原生
> 读取器为准，且该差异已记录待查。**
>
> 唯一不能碰的：`pc_total_score` / `pc_current_score`——没有和平会议时求值会断言退出。

---

## 经济 / 工业

| 变量 | 状态 | 来源 / 备注 |
|---|---|---|
| `num_of_civilian_factories` | ✅ 46 | `Country.summary()`，已用「各州 industrial_complex 求和」验证 |
| `num_of_military_factories` | ✅ 32 | 同上（arms_factory 求和） |
| `num_of_naval_factories` | ✅ 13 | 同上（dockyard 求和） |
| `num_of_factories` | ✅ 91 | 三者之和 |
| `num_of_available_civilian_factories` | ✅ 46 | 民工总数 − 建造队列占用（队列空则全闲置） |
| `num_of_available_military_factories` | ✅ 12 | 军工厂 − 产线占用，与 `overview.mil_free` 一致 |
| `num_of_available_naval_factories` | ✅ 3 | 船坞 − 海军产线占用，与告警「3/13」一致 |
| `num_of_civilian_factories_available_for_projects` | ✅ 46 | 同民工闲置（建造可用） |
| `num_of_owned_factories` | ✅ 91 | civ 46 + mil 32 + dock 13；引擎同为 91 |
| `num_of_controlled_factories` | ✅ 91 | 同上（GER 自有=控制） |
| `num_battalions` | ✅ 315 | 30 个师按模板累加：**253 线列 + 62 支援连**；引擎同为 315（支援连也算） |
| `num_of_{civilian,military,naval}_factories_in_cores@Tag` | ✅ 35 / 28 / 10 | **带 @ 参数**：`@Tag` 必填，指"我核心州里、由 @Tag 控制的那部分工厂"。无参数时引擎回 0。引擎 `@GER` 三个值 35/28/10 与本读取器**逐位一致**；`@FRA` 两者都 0 |
| `num_equipment@type` | ✅ 903（步兵） | **带 @ 参数**：某类装备的库存量。无参数时引擎回 0（不是库存总和！）。引擎 `@infantry_equipment` = 903 = 库存表步兵行 |
| `num_equipment_in_armies@type` | ✅ 24900（步兵） | **带 @ 参数**：师里实际携带的装备量，按 `CArmy+0x368` 逐师累加。引擎 6 项全对：步兵 24900 / 支援 889 / 火炮 346 / 摩托化 605 / 轻坦 692 / 轻坦_archetype 0 |
| `num_equipment_in_armies_k@type` | ✅ 24.9 | 同上 ÷1000；引擎 24.89999 |
| `fuel_k` | ✅ 352.874 | `Country.fuel`，**Q15(32768) 定点**；引擎 352.87 |
| `max_fuel_k` | ✅ 489.5 | `fuel_capacity/1000`，引擎 489.5 完全一致 |
| `fuel_ratio` | ✅ 0.72089 | `fuel / fuel_capacity`；引擎 0.72086 |

## 人力 / 军事

| 变量 | 状态 | 来源 / 备注 |
|---|---|---|
| `manpower` | ✅ 1299723 | 当前可用人力 |
| `manpower_k` | ✅ 1299.723 | 同上 ÷1000 |
| `max_manpower` | ✅ 1725376 | `Country.manpower_total`（未扣三军占用） |
| `max_available_manpower` | ✅ 1299723 | 当前可用（= `manpower`） |
| `num_divisions` | ✅ 30 | `CCountry+0x290` 师数组 |
| `num_armies` | ✅ 1 | `order_groups()`；**注意 `CO_ARMIES` 装的是师，不是军** |
| `num_of_nukes` | ✅ 0 | `CCountry+0x1310` |
| `num_deployed_planes` | ✅ 1004 | 全部联队 planes 求和；**924 是排除运输机后的数**（overview 告警口径），差 80 架运输机 |
| `num_ships` | ✅ 39 | 各特遣舰队 ships 求和（12+10+2+8+6+1） |
| `casualties` / `casualties_k` | ❌ | 战争统计容器未定位 |
| `garrison_manpower_need` | ❌ | 占领系统 |

## 政治 / 指挥

| 变量 | 状态 | 来源 / 备注 |
|---|---|---|
| `political_power` | ✅ 130 | |
| `command_power` | ✅ 14.11539 | `CCountry+0x1F0`，定点 1e5 |
| `stability` | ✅ 0.81（精确） | base 0.75 + 修正 0.0 + 执政党 0.4×0.15 = **0.81**，与引擎 `get_var` 逐位一致；`stability_parts` 可审计每一项 |
| `political_power_daily` | ✅ 2.324 | `2.0 × (1 + political_power_factor)`，与 `get_var` 一致 |
| `command_power_daily` | ✅ 0.33092 | `0.4 × (1 + command_power_gain_mult)`，与 `get_var` 0.33091 差 1e-5（舍入） |
| `highest_party_popularity` | ✅ 0.40 | `CPoliticalStatus+0x20` 四个党派取最大；实测 .35/.10/.40/.15 = 1.00，与引擎 0.40 一致 |
| `legitimacy` / `power_balance_*` | ❌ | 子系统 |

## 研究

| 变量 | 状态 | 来源 |
|---|---|---|
| `num_researched_technologies` | ✅ 48 | `researched()` 长度，与 `hoi4_researched` 一致 |
| `amount_research_slots` | ✅ 4 | `research_slots()` 长度 |
| `original_research_slots` | ✅ 4 | 同 `amount_research_slots`（GER 无加成时两者相等） |

## 外交 / 战争

| 变量 | 状态 | 来源 |
|---|---|---|
| `num_faction_members` | ✅ 1 | `faction_members()`，与 `hoi4_faction` 一致 |
| `num_subjects` | ✅ 0 / 11 / 2 | `CDiplomacyStatus+0x20` 里 `CSubjectRelation` 且 `+0x10` 是自己。GER 0、ENG 11（AST CAN NZL SAF OMA RAJ JOR PAL MAL BRM KUW）、FRA 2（LEB SYR）、JAP 1（MAN）、RAJ 0（它自己是 ENG 的附属国）——**全部是 vanilla 1936 的真实附属关系** |
| `surrender_progress` | 🔶 | `hoi4_wars()` 已返回 |
| `any_war_score` | ❌ | 战争分数 |
| `{enemies,alliance}_strength_ratio` | ❌ | 需全局军力对比 |

## 州 / 占领

| 变量 | 状态 | 来源 |
|---|---|---|
| `num_owned_states` | ✅ 29 | `CCountry+0x478`（**自有**州列表）。旧版用 `CO_STATES` 是错的，见下面一行 |
| `num_core_states` | ✅ 24 | 自有州 29 − `CState+0x2F0==1` 的非核心（吞并）州 5 个；引擎同为 24，FRA 为 32 |
| `num_controlled_states` | ✅ 29 | `CCountry+0x460`（=`CO_STATES`，**控制**州列表） |
| `num_occupied_states` | ✅ 0（GER）/ 2（ITA） | **控制 − 自有**：控制列表里不在自有列表中的州。ITA 1936 正占着埃塞俄比亚，这就是活样本。引擎文档原文 "states occupied by nation" |
| `core_resistance` / `core_compliance` | ❌ | 占领系统（`hoi4_state_detail` 目前回 null） |

---

## 带 `@` 参数的变量（新发现，2026-11-20）

这一批不是"名字对不上"，而是**名字后面要接参数**，光看注册表看不出来：

```
num_equipment_in_armies@infantry_equipment      # 师里有多少步兵装备
num_equipment@infantry_equipment                # 库存里有多少
num_of_civilian_factories_in_cores@GER          # 我核心州里、GER 控制的那部分
```

没有参数时**引擎自己回 0**——所以早先"引擎回 0、语义不明"的结论是错的，0 是"缺参数"的正常回答。

参数的解析规则（已用引擎逐条对拍）：

- 写**具体型号**（`light_tank_equipment_1`）→ 只算这一型号；
- 写**原型**（`infantry_equipment`）→ 算该原型下所有型号；
- 写**不存在的名字** → 0。例：`light_tank_equipment` 在 1.19 已被注释掉（坦克改走底盘制），所以是 0；而 `light_tank_equipment_1` 是 692。

**权威文档在游戏安装目录里**：`documentation/dynamic_variables_documentation.md`（48 KB，按 global/country/state/unit_leader/… 分节，逐条给说明和示例）。以后判断一个变量"是什么"应该先读它，比反汇编快得多。

## 现状汇总

- **47 个注册表变量 ✅ native**（另加 `fuel` 别名，共 48 项；全部交叉验证过）——占 265 条的 17.7%
- 剩余 **218** 个：`ai_*` 35、`total_equipment_produced_*` 39、`total_constructed_*` 19、`has_*` 10（合计 103 个，实战基本用不到），其余约 115 个是列表/作用域引用与杂项
- 约 8 个 🔶 有现成数据源，接上即可
- 其余 ❌ 需要针对子系统做反汇编

## 新增一个读取器前的检查清单

1. **先找独立来源**。不能拿控制台 `get_var` 当基准（见文首）。
2. 交叉验证至少两种口径（例：工厂数 = 各州建筑求和；师数 = 师数组长度 = `hoi4_army_status`）。
3. 警惕同名不同物：`CO_ARMIES` 是师数组（`num_armies` 第一版因此错了 30 倍）。
4. 任何会触发引擎断言的变量（`pc_total_score` / `pc_current_score`）不得进控制台路径。
5. **先读游戏自带的 `documentation/dynamic_variables_documentation.md`**：每条都有说明和示例，比反汇编快一个数量级。
6. **"引擎回 0" 先怀疑缺参数**：`num_*@type` / `num_*@Tag` 这一族没有参数就是 0，不代表变量坏了。
