"""The macro game layer. What a player actually interacts with.

本模块为瘦身后的薄 Facade：具体业务领域方法已拆入 `calling/domains/` 子包，
并通过显式多继承组合到 `Game`（彻底移除循环 `setattr` 猴子补丁与 `_resolve2` 覆写），
对外 100% 保持全部模块级偏移常量、辅助函数与 `Game` 方法签名向后兼容。
"""
from __future__ import annotations

import os
import re  # noqa: F401
import struct  # noqa: F401
import sys  # noqa: F401
import time
from typing import Optional  # noqa: F401

from api import HOI4, Country  # noqa: F401
from commands import CommandLayer  # noqa: F401
from explore import cstring  # noqa: F401
from housekeep import HousekeepMixin
import legality  # noqa: F401
from objindex import ObjectIndex
import offsets as O
import rtti_win as R  # noqa: F401
import session_state as _SS

from domains import (
    FocusMixin,
    PoliticsMixin,
    ResearchMixin,
    EconomyMixin,
    EconomyBuildMixin,
    OverviewMixin,
    StatsVarsMixin,
    MilitaryLandMixin,
    MilitaryLeadersMixin,
    BattleplanMixin,
    BattleplanOrdersMixin,
    DiplomacyWarMixin,
    AirNavyMixin,
    ResolverMixin,
)
from domains._shared import (
    BG_LEVEL as BG_LEVEL,
    BG_TYPE_ID as BG_TYPE_ID,
    BL_BUILDING as BL_BUILDING,
    BL_COST as BL_COST,
    BL_OWNER as BL_OWNER,
    BL_PROGRESS as BL_PROGRESS,
    BL_RATE as BL_RATE,
    BT_ID as BT_ID,
    BT_KEY as BT_KEY,
    BT_NAME as BT_NAME,
    BUILDING_REF_VTABLE as BUILDING_REF_VTABLE,
    CH_KEY as CH_KEY,
    CH_NAME as CH_NAME,
    CO_ARMIES as CO_ARMIES,
    CO_ARMY_UPGRADES as CO_ARMY_UPGRADES,
    CO_CAPITAL_STATE as CO_CAPITAL_STATE,
    CO_COMMAND_POWER as CO_COMMAND_POWER,
    CO_CONT_FOCUS_PAL as CO_CONT_FOCUS_PAL,
    CO_COUNTRY_INTEL as CO_COUNTRY_INTEL,
    CO_DECISIONS as CO_DECISIONS,
    CO_DIPLOMACY as CO_DIPLOMACY,
    CO_DIV_TEMPLATES as CO_DIV_TEMPLATES,
    CO_EQUIPMENT_TYPES as CO_EQUIPMENT_TYPES,
    CO_FACTION as CO_FACTION,
    CO_FLEETS as CO_FLEETS,
    CO_FOCUS_COMPLETED as CO_FOCUS_COMPLETED,
    CO_FOCUS_PROGRESS as CO_FOCUS_PROGRESS,
    CO_FOCUS_TREE as CO_FOCUS_TREE,
    CO_INTEL_AGENCY as CO_INTEL_AGENCY,
    CO_OCCUPATION as CO_OCCUPATION,
    CO_OPERATIONS as CO_OPERATIONS,
    CO_OWNED_STATES as CO_OWNED_STATES,
    CO_POLITICS as CO_POLITICS,
    CO_PRODUCTION as CO_PRODUCTION,
    CO_PROGRAM_STATUS as CO_PROGRAM_STATUS,
    CO_PROJECT_POOL as CO_PROJECT_POOL,
    CO_RELATIONS_ARR as CO_RELATIONS_ARR,
    CO_RESOURCES as CO_RESOURCES,
    CO_RESOURCE_ORIGINS as CO_RESOURCE_ORIGINS,
    CO_STATES as CO_STATES,
    CO_SUBUNITS as CO_SUBUNITS,
    CO_TECH_STATUS as CO_TECH_STATUS,
    CO_THEATRES as CO_THEATRES,
    CO_TRADE_ROUTES as CO_TRADE_ROUTES,
    DECISION_NAME as DECISION_NAME,
    DP_RELATIONS as DP_RELATIONS,
    DS_ACTIVE as DS_ACTIVE,
    DS_ALL as DS_ALL,
    DS_RUNNING as DS_RUNNING,
    DS_TARGETED as DS_TARGETED,
    DS_VISIBLE as DS_VISIBLE,
    DT_NAME as DT_NAME,
    ET_ID as ET_ID,
    ET_NAME as ET_NAME,
    EV_TYPE_ID as EV_TYPE_ID,
    FACTION_MEMBERS as FACTION_MEMBERS,
    FL_NAME as FL_NAME,
    FN_CAN_RESEARCH as FN_CAN_RESEARCH,
    FN_DYNAMIC_VAR as FN_DYNAMIC_VAR,
    FP_COMPLETED as FP_COMPLETED,
    FP_COUNTRY as FP_COUNTRY,
    FP_CURRENT as FP_CURRENT,
    FP_DAILY as FP_DAILY,
    FP_ROOT_FOCUSES as FP_ROOT_FOCUSES,
    FRONT_SNAPSHOT_PATH as FRONT_SNAPSHOT_PATH,
    FT_FOCUSES as FT_FOCUSES,
    HANDLE_OFFSETS as HANDLE_OFFSETS,
    HERE as HERE,
    IDEA_NAME as IDEA_NAME,
    ML_EFF_COST as ML_EFF_COST,
    ML_FACTORIES as ML_FACTORIES,
    ML_MAX_EFFICIENCY as ML_MAX_EFFICIENCY,
    ML_OUTPUT as ML_OUTPUT,
    ML_OWNER as ML_OWNER,
    ML_PRIORITY as ML_PRIORITY,
    ML_TOTAL_PRODUCED as ML_TOTAL_PRODUCED,
    ML_UNIT_COST as ML_UNIT_COST,
    ML_VARIANT as ML_VARIANT,
    NF_AVAIL_TRIGGER_N as NF_AVAIL_TRIGGER_N,
    NF_FILTERS as NF_FILTERS,
    NF_ICON as NF_ICON,
    NF_ID as NF_ID,
    NF_KEY as NF_KEY,
    NF_LOCKEY as NF_LOCKEY,
    NF_MUTEX as NF_MUTEX,
    NF_NAME as NF_NAME,
    NF_PREREQ as NF_PREREQ,
    OBJINDEX_CACHE_PATH as OBJINDEX_CACHE_PATH,
    PL_ACTIVE_IDEAS as PL_ACTIVE_IDEAS,
    PL_ALL_IDEAS as PL_ALL_IDEAS,
    PL_PARTIES as PL_PARTIES,
    PL_PP as PL_PP,
    PL_RULING as PL_RULING,
    PS_CONSTRUCTION as PS_CONSTRUCTION,
    PS_LINES as PS_LINES,
    PS_MIL_LINES as PS_MIL_LINES,
    PS_VARIANTS_ACTIVE as PS_VARIANTS_ACTIVE,
    PS_VARIANTS_ALL as PS_VARIANTS_ALL,
    REL_INDEX as REL_INDEX,
    RESOURCE_NAMES as RESOURCE_NAMES,
    RES_ARRAY as RES_ARRAY,
    RES_STRIDE as RES_STRIDE,
    RS_CURRENT as RS_CURRENT,
    RS_INDEX as RS_INDEX,
    RS_OWNER as RS_OWNER,
    RS_PROGRESS as RS_PROGRESS,
    RS_REQUIRED as RS_REQUIRED,
    RS_SPEED as RS_SPEED,
    ST_BUILDINGS as ST_BUILDINGS,
    ST_BUILDING_LEVELS as ST_BUILDING_LEVELS,
    ST_BUILDING_MAX as ST_BUILDING_MAX,
    ST_COMPLIANCE as ST_COMPLIANCE,
    ST_ID as ST_ID,
    ST_PROVINCES as ST_PROVINCES,
    ST_RESISTANCE as ST_RESISTANCE,
    SU_CATEGORY as SU_CATEGORY,
    SU_KEY as SU_KEY,
    TE_ID as TE_ID,
    TE_PROGRESS as TE_PROGRESS,
    TE_TEMPLATE as TE_TEMPLATE,
    TS_ALL_TECHS as TS_ALL_TECHS,
    TS_PRODUCTION_LINES as TS_PRODUCTION_LINES,
    TS_RESEARCHED as TS_RESEARCHED,
    TS_SLOTS as TS_SLOTS,
    TT_ID as TT_ID,
    TT_NAME as TT_NAME,
    TT_RESEARCH_COST as TT_RESEARCH_COST,
    TT_YEAR as TT_YEAR,
    _AIR_SNAP as _AIR_SNAP,
    _CONV_PROGRESS as _CONV_PROGRESS,
    _VERIFIED_NAMES as _VERIFIED_NAMES,
    _arr as _arr,
    _count as _count,
    _count_by as _count_by,
    _wargoal_gate as _wargoal_gate,
    annotations as annotations,
    item_name as item_name,
)


class Game(
    HousekeepMixin,
    FocusMixin,
    PoliticsMixin,
    ResearchMixin,
    EconomyMixin,
    EconomyBuildMixin,
    OverviewMixin,
    StatsVarsMixin,
    MilitaryLandMixin,
    MilitaryLeadersMixin,
    BattleplanMixin,
    BattleplanOrdersMixin,
    DiplomacyWarMixin,
    AirNavyMixin,
    ResolverMixin,
    HOI4,
):
    """The HOI4 handle plus the macro game mechanics."""

    _VT_RTTI = None

    _VT_SCAN = {}

    def __init__(self, pid=None):
        super().__init__(pid)
        self.cmd = CommandLayer(self)
        self._oi = None
        self._tech_by_name = None
        self._tech_by_id = None
        self._idea_by_name = None
        self._decision_by_name = None

    @property
    def oi(self) -> ObjectIndex:
        # 2026-11-06: the index holds absolute heap addresses. When the game is
        # restarted (crash reload, fresh campaign) and the MCP process survives, the
        # cached index still points at the OLD process: CTechnologyDatabase /
        # CBuildingDatabase resolved to None and every research/construction call
        # died with `TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'`.
        #
        # 2026-11-18: the pid is NOT enough.  In-game "Restart game" rebuilds the
        # whole world inside the SAME process (pid unchanged), so a pid-only cache
        # handed out dead CAirWing pointers from the previous campaign --
        # hoi4_air_mission then posted handles that no longer resolved, and the
        # engine died at hoi4.exe+0xC08724 (country lookup over a bad pointer).
        # Fingerprint the world with the CCurrentGameState pointer, which is
        # recreated on every "Restart game".
        pid = None
        try:
            pid = self.p.pid
        except Exception:
            pass
        world = None
        try:
            world = self.p.u64(self.va(getattr(O, "G_GAMESTATE", 0x33DD6B0)))
        except Exception:
            world = None
        _SS.sync(pid=pid, world_ptr=world)
        if (self._oi is None
                or getattr(self, "_oi_pid", None) != pid
                or (world and getattr(self, "_oi_world", None)
                    and world != self._oi_world)):
            self._oi = ObjectIndex(self.p, R.build()["vtables"],
                                   OBJINDEX_CACHE_PATH)
            self._oi.build()
            self._oi_pid = pid
            self._oi_world = world
            # Clear class-level vtable scan cache too so same-PID restarts never reuse freed pointers
            type(self)._VT_SCAN.clear()
            # per-process caches derived from the index
            self._tech_by_name = None
            self._tech_by_id = None
        return self._oi

    def reset_caches(self):
        """Drop every world-derived cache -- call after in-game "Restart game".

        `hoi4_debug_eval("g.reset_caches()")` replaces the old
        `type(g)._VT_SCAN.clear()` ritual and ALSO invalidates the RTTI object
        index, which is the cache that actually goes stale across a restart
        (the pid does not change, so `oi` would happily reuse dead pointers).
        """
        type(self)._VT_SCAN.clear()
        cleared_caches = _SS.invalidate(reason="reset_caches")
        self._oi = None
        self._oi_pid = None
        self._oi_world = None
        self._tech_by_name = None
        self._tech_by_id = None
        self._idea_by_name = None
        self._decision_by_name = None
        self._eq_by_id = None
        self._bld_by_key = None
        self._bld_by_id = None
        self._adv_pool_cache = None
        self._faction_objects_cache = None
        self._tags = None
        self._tag2idx = None
        self._tags_pid = None
        self._moddefs = None
        self._cmds = None
        self._date_retry_at = 0
        # Only CACHE ATTRIBUTES here.  `_win_date_addr` is a METHOD -- nulling it
        # shadowed the method and every date read died with
        # `TypeError: 'NoneType' object is not callable` (2026-11-18, right after
        # the first version of this method shipped).
        #
        # 2026-11-18 (BUG-H): the DATE caches are part of this too.  Loading the
        # 1936.08 save into the same process left `hoi4_date` reporting
        # 1936.01.01 forever: `api.date_raw`'s monotonic guards compare against
        # `_date_last` / `_date_high`, and a save load is exactly the one event
        # where the clock may jump backwards or forwards by months. The forward
        # guard then reverted the live 1936.07.31 reading to the old 1936.01.01
        # value ("more than 30 days between two reads is only believable when
        # game.log agrees" -- and game.log had stopped being written at load).
        for attr in ("_date_exact_cache", "_date_addr", "_date_last", "_date_high",
                     "_date_static", "_date_verified", "_date_pid",
                     "_date_unverified", "_win_date", "_win_date_pid",
                     "_win_date_last"):
            try:
                setattr(self, attr, None)
            except Exception:
                pass
        try:
            self._dead_date_addrs = set()
        except Exception:
            pass
        return {"cleared": True, "cleared_caches": cleared_caches}

    def rescan_objects(self):
        self.oi.build(force=True)

    @staticmethod
    def _settle(seconds=0.25):
        """Short wait so the game can process a command after it is queued."""
        time.sleep(seconds)

    def _c(self, tag) -> Country:
        return self.player() if tag in (None, "", "player") else self.country(tag)

    def _win_vt_cache(self, class_name):
        """The cached RTTI vtable-scan hits for one class, or None if absent.

        _VT_SCAN is keyed by (pid, class name); the key shape has changed before,
        so match on the last tuple element instead of assuming a position --
        BUT never return another process's hits.

        2026-11-17: the old version matched on `k[-1]` alone, so after a game
        restart the new Game object picked up the DEAD pid's array addresses.
        `_kick_tech_scan()` then thought the scan was already cached, never
        re-ran it, `_load_tech_db()` got stale pointers, `_tech_by_name` stayed
        None for the whole process and every research call died with
        "'NoneType' object has no attribute 'get'".  Research looked "broken"
        after every restart because of this.
        """
        cache = getattr(type(self), "_VT_SCAN", None) or {}
        pid = getattr(getattr(self, "p", None), "pid", None)
        if pid is not None:
            direct = cache.get((pid, class_name))
            # 2026-11-23 (C-2 根因修复): 空列表 [] 绝不能当成“已就绪缓存”，
            # 否则冷启动/重启瞬间扫出 0 条会永久锁死科技库直到 mcp_load 重启进程！
            if direct:
                return direct
        # 后台线程可能并发写入 _VT_SCAN，使用快照列表遍历避免 RuntimeError
        for k, v in list(cache.items()):
            if not (isinstance(k, tuple) and k and k[-1] == class_name):
                continue
            if len(k) >= 2 and isinstance(k[0], int) and pid is not None:
                if k[0] != pid:
                    continue          # hits belong to a previous game process
            if v:
                return v
        return None

    def _kick_tech_scan(self):
        """Start the CTechnologyTemplate scan in the background (20-40 s)."""
        if self._win_vt_cache("CTechnologyTemplate") is not None:
            return False
        pid = getattr(getattr(self, "p", None), "pid", None)
        run_key = (pid, "CTechnologyTemplate")
        flag = getattr(type(self), "_VT_SCAN_RUNNING", None)
        if flag is None:
            flag = set()
            type(self)._VT_SCAN_RUNNING = flag
        if run_key in flag:
            return False
        import threading

        def _work():
            try:
                self.win_scan_class("CTechnologyTemplate")
            except Exception:
                pass
            finally:
                flag.discard(run_key)

        flag.add(run_key)
        threading.Thread(target=_work, daemon=True).start()
        return True

    def _run_cmd(self, cmd):
        """Windows PE 命令入队（经 cmdwin.enqueue 补齐会话 tick/seq 并投递队列）。"""
        inv = getattr(self, "invalidate_read_scope", None)
        if callable(inv):
            inv()
        import cmdwin
        rv = cmdwin.enqueue(self, cmd)
        self._last_cmd_error = ""
        return rv

    def _read_stdstring_at(self, s):
        try:
            ptr, ln = self.p.u64(s), self.p.u64(s + 8)
            if ptr and 0 < ln < 512:
                return (self.p.try_read(ptr, ln) or b"").decode("utf-8", "replace")
        except Exception:
            pass
        return ""

    def _rtti_classes(self):
        if Game._VT_RTTI is None:
            import safe_cache
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            Game._VT_RTTI = safe_cache.load(
                os.path.join(root, "research", "windows", "rtti_win.pkl")
            )["classes"]
        return Game._VT_RTTI

    def _rtti_vt_rvas(self, class_name):
        """vtable RVAs for a class, tolerating namespace-mangled RTTI names.

        `rtti_win.pkl` keys are the raw MSVC type descriptors: a global class is
        ".?AVCFoo@@", but a namespaced one is ".?AVCFoo@NFoo@@".
        """
        cls = self._rtti_classes()
        out = list(cls.get(".?AV%s@@" % class_name) or [])
        if not out:
            pref = ".?AV%s@" % class_name
            for k, v in cls.items():
                if k.startswith(pref):
                    out.extend(v)
        return out

    def win_scan_class(self, class_name, use_cache=True):
        """Every live object whose vtable belongs to `class_name` (PE only).

        2026-11-23 (C-2 根因修复): 严禁将空扫描结果 `[]` 永久写入 `Game._VT_SCAN`。
        游戏重启或刚进主菜单时扫描可能暂时返回 0 条，旧代码缓存 `[]` 后导致
        `_win_vt_cache()` 永远返回 `[]`（is not None），科技库/模板库整会话死锁，
        只能重启 MCP 进程（mcp_load）才能救活。现在仅缓存非空命中；空结果只做 2s 防抖。
        """
        import time as _time
        p = self.p
        key = (p.pid, class_name)
        if use_cache:
            cached = Game._VT_SCAN.get(key)
            if cached:
                return cached
            empty_at = getattr(Game, "_VT_SCAN_EMPTY_AT", {}).get(key)
            if empty_at and (_time.monotonic() - empty_at) < 2.0:
                return []
        rvas = self._rtti_vt_rvas(class_name)
        if not rvas:
            return []
        import fastmem as _fm
        from objindex import _scan_regions_for_vptrs
        want = [p.base + r for r in rvas]
        raw_regs = _fm.enum_regions(p, writable=True, heap_only=True, merge_adjacent=True)
        if raw_regs is not None:
            regs = [{"start": s, "end": e} for s, e, _ in raw_regs]
        else:
            regs = p.regions(writable=True)
        hits = _scan_regions_for_vptrs(
            p, regs, want, max_hits=10_000_000, chunk_size=8 << 20
        )
        if hits:
            Game._VT_SCAN[key] = hits
            getattr(Game, "_VT_SCAN_EMPTY_AT", {}).pop(key, None)
        else:
            Game._VT_SCAN.pop(key, None)
            if not hasattr(Game, "_VT_SCAN_EMPTY_AT"):
                Game._VT_SCAN_EMPTY_AT = {}
            Game._VT_SCAN_EMPTY_AT[key] = _time.monotonic()
        return hits

    def win_arrays(self, obj, span=0x800, limit=400000):
        """CPdxArray containers inside `obj` (PE build).

        `probe.describe()` returns [] for every status object on the PE build —
        its element-class logic does not match the MSVC layouts — so this walks
        the data/cap/size/alloc quad manually and names element 0's class.
        """
        p = self.p
        out = []
        for off in range(0, span, 8):
            try:
                data = p.u64(obj + off)
            except Exception:
                break
            if not (0x10000 < data < 0x7FFFFFFFFFFF):
                continue
            try:
                cap = p.i32(obj + off + 8)
                size = p.i32(obj + off + 12)
            except Exception:
                continue
            if not (0 <= size <= cap <= limit) or size == 0:
                continue
            elem = None
            try:
                elem = self.oi.class_of(data)          # inline element objects
                if not elem:
                    e0 = p.u64(data)                    # array of pointers
                    if 0x10000 < e0 < 0x7FFFFFFFFFFF:
                        elem = self.oi.class_of(e0)
            except Exception:
                elem = None
            out.append({"offset": off, "size": size, "cap": cap, "elem": elem})
        return out

    # ------------------------------------------------- Standing Orders v1 薄封装
    def compile_standing_orders(self, profile, tag=None):
        """干跑编译校验 Standing Orders profile（纯只读 0 注入）。"""
        import autopilot as _ap
        return _ap.compile_profile(self, profile, tag=tag)

    def set_standing_orders(self, profile, dry_run=False, replace=True, tag=None):
        """声明、编译并激活 Standing Orders。"""
        import autopilot as _ap
        return _ap.set_standing_orders(self, profile, dry_run=dry_run, replace=replace, tag=tag)

    def clear_standing_orders(self, reason="", tag=None):
        """停用并清空当前会话的 Standing Orders。"""
        import autopilot as _ap
        return _ap.clear_standing_orders(self, reason=reason, tag=tag)

    def autopilot_status(self, tag=None, journal_limit=15):
        """查询当前会话 Standing Orders 状态与执行日志。"""
        import autopilot as _ap
        return _ap.status(self, tag=tag, journal_limit=journal_limit)

    def autopilot_tick(self, tag=None, dry_run=False):
        """立即执行一次 Standing Orders 确定性 tick。"""
        import autopilot as _ap
        return _ap.run_tick(self, tag=tag, dry_run=dry_run)

    def ack_autopilot(self, reason="", extend_max_days=False, tag=None):
        """只读签收自动驾驶心跳或熔断唤醒（0 引擎注入、免前台窗口）。"""
        import autopilot as _ap
        return _ap.acknowledge_wakeup(
            self, tag=tag, reason=reason, extend_max_days=extend_max_days
        )

    @staticmethod
    def strategy_catalog(domain=None):
        """列出 Standing Orders Strategy Registry 的全部可审计策略公式与参数。"""
        import autopilot as _ap
        return _ap.strategy_catalog(domain=domain)


# ============================================================================
#  历史测试夹具向后兼容 Mixin 别名（纯 Mixin 组合，不继承 HOI4.__init__）
# ============================================================================
class MacroMixin(
    FocusMixin,
    PoliticsMixin,
    ResearchMixin,
    EconomyMixin,
    EconomyBuildMixin,
    OverviewMixin,
    StatsVarsMixin,
    MilitaryLandMixin,
    MilitaryLeadersMixin,
    BattleplanMixin,
    BattleplanOrdersMixin,
    DiplomacyWarMixin,
    AirNavyMixin,
    ResolverMixin,
):
    """向后兼容别名：旧测试通过 `game.MacroMixin.<method>` 读取方法源码或绑定。"""


class MacroMixin2(EconomyBuildMixin, PoliticsMixin, ResolverMixin):
    """向后兼容别名：旧测试通过 `game.MacroMixin2.state_detail` 等直接引用。"""


class HandleMixin(ResolverMixin):
    """向后兼容别名：句柄解析方法已并入 `domains.resolver.ResolverMixin`。"""


class ExtraMixin(DiplomacyWarMixin, MilitaryLeadersMixin, ResolverMixin):
    """向后兼容别名：旧测试通过 `class _FakeDip(game.ExtraMixin)` 继承。"""


class MoveMixin(MilitaryLandMixin):
    """向后兼容别名：陆战微操委托方法已并入 `domains.military_land.MilitaryLandMixin`。"""


def _clear_game_vt_scan():
    Game._VT_SCAN.clear()
    empty_at = getattr(Game, "_VT_SCAN_EMPTY_AT", None)
    if isinstance(empty_at, dict):
        empty_at.clear()
    running = getattr(Game, "_VT_SCAN_RUNNING", None)
    if isinstance(running, set):
        running.clear()


_SS.register_cache("game._VT_SCAN", _clear_game_vt_scan)


__all__ = [
    "Country",
    "CommandLayer",
    "ExtraMixin",
    "Game",
    "HOI4",
    "HandleMixin",
    "HousekeepMixin",
    "MacroMixin",
    "MacroMixin2",
    "MoveMixin",
    "ObjectIndex",
    "ResolverMixin",
    "BG_LEVEL",
    "BG_TYPE_ID",
    "BL_BUILDING",
    "BL_COST",
    "BL_OWNER",
    "BL_PROGRESS",
    "BL_RATE",
    "BT_ID",
    "BT_KEY",
    "BT_NAME",
    "BUILDING_REF_VTABLE",
    "CH_KEY",
    "CH_NAME",
    "CO_ARMIES",
    "CO_ARMY_UPGRADES",
    "CO_CAPITAL_STATE",
    "CO_COMMAND_POWER",
    "CO_CONT_FOCUS_PAL",
    "CO_COUNTRY_INTEL",
    "CO_DECISIONS",
    "CO_DIPLOMACY",
    "CO_DIV_TEMPLATES",
    "CO_EQUIPMENT_TYPES",
    "CO_FACTION",
    "CO_FLEETS",
    "CO_FOCUS_COMPLETED",
    "CO_FOCUS_PROGRESS",
    "CO_FOCUS_TREE",
    "CO_INTEL_AGENCY",
    "CO_OCCUPATION",
    "CO_OPERATIONS",
    "CO_OWNED_STATES",
    "CO_POLITICS",
    "CO_PRODUCTION",
    "CO_PROGRAM_STATUS",
    "CO_PROJECT_POOL",
    "CO_RELATIONS_ARR",
    "CO_RESOURCES",
    "CO_RESOURCE_ORIGINS",
    "CO_STATES",
    "CO_SUBUNITS",
    "CO_TECH_STATUS",
    "CO_THEATRES",
    "CO_TRADE_ROUTES",
    "DECISION_NAME",
    "DP_RELATIONS",
    "DS_ACTIVE",
    "DS_ALL",
    "DS_RUNNING",
    "DS_TARGETED",
    "DS_VISIBLE",
    "DT_NAME",
    "ET_ID",
    "ET_NAME",
    "EV_TYPE_ID",
    "FACTION_MEMBERS",
    "FL_NAME",
    "FN_CAN_RESEARCH",
    "FN_DYNAMIC_VAR",
    "FP_COMPLETED",
    "FP_COUNTRY",
    "FP_CURRENT",
    "FP_DAILY",
    "FP_ROOT_FOCUSES",
    "FRONT_SNAPSHOT_PATH",
    "FT_FOCUSES",
    "HANDLE_OFFSETS",
    "HERE",
    "IDEA_NAME",
    "ML_EFF_COST",
    "ML_FACTORIES",
    "ML_MAX_EFFICIENCY",
    "ML_OUTPUT",
    "ML_OWNER",
    "ML_PRIORITY",
    "ML_TOTAL_PRODUCED",
    "ML_UNIT_COST",
    "ML_VARIANT",
    "NF_AVAIL_TRIGGER_N",
    "NF_FILTERS",
    "NF_ICON",
    "NF_ID",
    "NF_KEY",
    "NF_LOCKEY",
    "NF_MUTEX",
    "NF_NAME",
    "NF_PREREQ",
    "OBJINDEX_CACHE_PATH",
    "PL_ACTIVE_IDEAS",
    "PL_ALL_IDEAS",
    "PL_PARTIES",
    "PL_PP",
    "PL_RULING",
    "PS_CONSTRUCTION",
    "PS_LINES",
    "PS_MIL_LINES",
    "PS_VARIANTS_ACTIVE",
    "PS_VARIANTS_ALL",
    "REL_INDEX",
    "RESOURCE_NAMES",
    "RES_ARRAY",
    "RES_STRIDE",
    "RS_CURRENT",
    "RS_INDEX",
    "RS_OWNER",
    "RS_PROGRESS",
    "RS_REQUIRED",
    "RS_SPEED",
    "ST_BUILDINGS",
    "ST_BUILDING_LEVELS",
    "ST_BUILDING_MAX",
    "ST_COMPLIANCE",
    "ST_ID",
    "ST_PROVINCES",
    "ST_RESISTANCE",
    "SU_CATEGORY",
    "SU_KEY",
    "TE_ID",
    "TE_PROGRESS",
    "TE_TEMPLATE",
    "TS_ALL_TECHS",
    "TS_PRODUCTION_LINES",
    "TS_RESEARCHED",
    "TS_SLOTS",
    "TT_ID",
    "TT_NAME",
    "TT_RESEARCH_COST",
    "TT_YEAR",
    "annotations",
    "item_name",
]

# 显式标记私有兼容导出为已使用，保持 pyflakes 零噪声
_COMPAT_EXPORTS = (
    os, re, struct, sys, time, Optional, cstring, legality, O, R,
    _AIR_SNAP, _CONV_PROGRESS, _VERIFIED_NAMES, _arr, _count, _count_by, _wargoal_gate,
)
