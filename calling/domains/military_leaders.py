"""角色、顾问池、将领/元帅查询、提拔招募及省份指针查询领域 Mixin。"""
from __future__ import annotations

import offsets as O
import legality
import struct
import sys

from ._shared import CH_KEY, CH_NAME, _arr, item_name

class MilitaryLeadersMixin:
    """角色、顾问池、将领/元帅查询、提拔招募及省份指针查询领域 Mixin。"""

    VT_ARMY_LEADER = 0x43D5890   # 11CArmyLeader vtable, 1.19.4.1 ELF RTTI

    VT_ADVISOR = 0x4349AD8   # 8CAdvisor vtable, 1.19.4.1 ELF RTTI

    VT_CCHARACTER = 0x4349390   # 10CCharacter vtable, 1.19.4.1 ELF RTTI

    G_PROVINCE_MANAGER = 0x459E9F0   # global -> manager singleton (vtable+0x50 = GetProvince(id))
    VT_PROVINCE_MGR    = 0x50        # slot offset inside the manager vtable

    CH_ARMY_LEADER = 0xA8   # CArmyLeader*, set for generals and admirals (1.19.5; was 0xC0)
    # Skills live at +0xF58/+0xF68/+0xF78/+0xF88 in 1.19.5 — the whole CArmyLeader
    # block moved down 0x100 bytes when the class grew.
    AL_SKILL       = 0x28
    AL_ATTACK      = 0xF58
    AL_DEFENSE     = 0xF68
    AL_PLANNING    = 0xF78
    AL_LOGISTICS   = 0xF88

    def _extract_char_key_and_name(self, ch: int):
        """(key, name) of a CCharacter, tolerant of the 1.19.x layout shifts.

        CH_KEY/CH_NAME are correct for 1.19.4.1 (checked live), but the fallback scan
        keeps this working if a future patch inserts a field before them — the key is
        always a `TAG_something` token and the name a printable string.
        """
        p = self.p
        key = item_name(p, ch + CH_KEY)
        name = item_name(p, ch + CH_NAME)
        if key and 2 < len(key) < 64 and "_" in key:
            return key, (name or key)
        found_key, found_name = None, None
        for off in range(0x10, 0x78, 8):
            s = item_name(p, ch + off)
            if not s or not s.isprintable() or not (3 <= len(s) <= 64):
                continue
            head = s.split("_", 1)[0]
            if "_" in s and 2 <= len(head) <= 4 and head.isalpha() and head.isupper():
                found_key = s
            elif found_name is None:
                found_name = s
        return found_key, (found_name or found_key or key)

    def _find_army_leader_ptr(self, ch: int) -> int:
        """CArmyLeader* inside a CCharacter, found by its vtable, not by an offset.

        `CH_ARMY_LEADER = 0xC0` is where 1.19.4.1 keeps it, but that is exactly the
        kind of offset that moves when a class grows (CScientist was added in this
        version). Matching the CArmyLeader vtable over +0x50..+0x150 is immune to it.
        """
        p = self.p
        raw = p.try_read(ch + 0x50, 0x100)
        if not raw:
            return 0
        for i in range(0, len(raw) - 7, 8):
            ptr = struct.unpack_from("<Q", raw, i)[0]
            if not (0x10000 < ptr < 0x7FFFFFFFFFFF):
                continue
            head = p.try_read(ptr + 8, 4)
            if head and struct.unpack("<i", head)[0] == 4713:
                return ptr
        return 0

    def _advisor_pool(self):
        """{character_key: CAdvisor*} for the live process.

        Probed 2026-11-10: a CAdvisor is NOT stored inside its CCharacter (a
        +0x000..+0x220 dump only shows CCharacterTemplate / CUnitMedalStore).
        Advisors live in their own array; each one carries its character key as a
        std::string at +0x30 (e.g. "GER_werner_von_blomberg"). So collect every
        object whose vtable is 8CAdvisor and index it by that string.
        """
        p = self.p
        cache = getattr(self, "_adv_pool_cache", None)
        if cache and cache[0] == p.pid and cache[1]:
            return cache[1]
        if sys.platform == "win32":
            found = {}
            for addr in self.win_scan_class("CAdvisor"):
                key = p.stdstring(addr + 0x30)
                if key:
                    found[key] = addr
            self._adv_pool_cache = (p.pid, found)
            return found
        target = p.va(self.VT_ADVISOR)
        needle = struct.pack("<Q", target)
        found = {}
        for m in p.regions(writable=True):
            a, b = m["start"], m["end"]
            cur = a
            while cur < b:
                n = min(4 << 20, b - cur)
                try:
                    buf = p.try_read(cur, n)
                except Exception:
                    buf = None
                if buf:
                    k = 0
                    while True:
                        k = buf.find(needle, k)
                        if k < 0:
                            break
                        addr = cur + k
                        try:
                            key = p.stdstring(addr + 0x30)
                        except Exception:
                            key = None
                        if key and 3 < len(key) < 64 and key not in found:
                            found[key] = addr
                        k += 8
                cur += n
        if found:
            self._adv_pool_cache = (p.pid, found)
        return found

    def find_advisor(self, key, tag=None):
        """CAdvisor* for a character key / idea token / bare name."""
        pool = self._advisor_pool()
        if not pool:
            return 0
        k = (key or "").lower()
        for cand in (k, k.upper()):
            if pool.get(cand):
                return pool[cand]
        for name, addr in pool.items():
            nl = name.lower()
            if nl == k or nl.endswith("_" + k):
                return addr
        return 0

    def _find_advisor_ptr(self, ch: int) -> int:
        """Kept for compatibility: advisors are not inside the character."""
        return 0

    def province_ptr(self, province_id):
        """CProvince* for a province id, via the engine's own GetProvince(id).

        Found 2026-11-10 by disassembling 0xda7410:
            rax = [0x459e9f0]            ; manager singleton
            fn  = [[rax]+0x50]           ; virtual GetProvince(int)
        This is exactly the object COrderSetParadropTargetCommand wants as arg1.

        PE 1.19.5.0: G_PROVINCE_MANAGER is a Linux ELF offset, so the injected
        call crashed (Err 299).  The live province table is a plain
        CPdxArray<CProvince*> inside CCurrentGameState (GS_PROVINCES, cap 13414)
        and is indexed by province id, so the pointer is a pure read now.
        """
        p = self.p
        if sys.platform == "win32":
            gs = p.u64(self.va(getattr(O, "G_GAMESTATE", 0x33DD6B0)))
            if not gs:
                return 0
            arr = _arr(p, gs, getattr(O, "GS_PROVINCES", 0x2B0))
            if 0 <= int(province_id) < len(arr):
                return arr[int(province_id)]
            return 0
        mgr = p.u64(self.va(self.G_PROVINCE_MANAGER))
        if not mgr:
            return 0
        vt = p.u64(mgr)
        if not vt:
            return 0
        fn = p.u64(vt + self.VT_PROVINCE_MGR)
        if not fn:
            return 0
        import api
        api.allow_call(fn - self.base)
        # thiscall: rdi = manager singleton, esi = province id
        return self.call(fn - self.base, (mgr, int(province_id)))

    def _scan_vtable(self, vt_off):
        """All live objects whose first qword is `va(vt_off)`, cached per pid.

        2026-11-10: objindex.pkl is bound to ONE game process; after a restart
        every address in it is dead, which is why hoi4_generals returned 0 here.
        Scanning the writable heap for the class vtable is pid-proof.
        """
        p = self.p
        cache = getattr(self, "_vt_scan_cache", None)
        if cache and cache[0] == p.pid and vt_off in cache[1]:
            return cache[1][vt_off]
        import fastmem as _fm
        from objindex import _scan_regions_for_vptrs
        target = p.va(vt_off)
        raw_regs = _fm.enum_regions(p, writable=True, heap_only=True, merge_adjacent=True)
        if raw_regs is not None:
            regs = [{"start": s, "end": e} for s, e, _ in raw_regs]
        else:
            regs = p.regions(writable=True)
        found = _scan_regions_for_vptrs(
            p, regs, [target], max_hits=10_000_000, chunk_size=4 << 20
        )
        if not hasattr(self, "_vt_scan_cache") or self._vt_scan_cache[0] != p.pid:
            self._vt_scan_cache = (p.pid, {})
        self._vt_scan_cache[1][vt_off] = found
        return found

    def characters(self, tag=None, limit=5000):
        """The country's characters: generals, admirals, advisors, leaders."""
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        try:
            chars = self.all_characters(c.tag)
        except Exception:
            chars = []
        if not chars:
            # `_scan_vtable` is a no-op on the PE build, and CCharacterManager+0x10
            # is empty right after a save is loaded (the manager repopulates later),
            # so a freshly restarted session found zero characters.  The object index
            # always has them: 12k CCharacter instances are scanned in ~12 s.
            try:
                chars = list(self.oi.instances("CCharacter"))
            except Exception:
                chars = []
        if not chars:
            chars = self._scan_vtable(self.VT_CCHARACTER)
        for ch in chars:
            k, nm = self._extract_char_key_and_name(ch)
            if k and k.lower().startswith(pref):
                out.append({"key": k, "name": nm, "ptr": hex(ch)})
                if len(out) >= limit:
                    break
        return {"count": len(out), "characters": out}

    def _character_manager(self):
        return self.oi.singleton("CCharacterManager")

    def _country_leader_ptrs(self, tag=None):
        """PE 1.19.5.0: 直接从 CCountryCharacters (CCountry+0xFF0) 读本国全量陆/海军将领指针。

        2026-11-23 (C-8 真机实证):
          * CCountryCharacters+0x70 = CPdxArray<CArmyLeader*>（纯净本国陆军将领，含静态历史、
            归国如 CHI_alexander_von_falkenhausen、以及 CCreateUnitLeaderCommand 动态招募的
            随机名将领如 'Emil Pickelhaubt'！）；
          * CCountryCharacters+0x88 = CPdxArray<CNavyLeader*>（纯净本国海军将领）；
          * 每名将领自身 +0x20 存 script key，+0x40 存本地化显示名。
        """
        p = self.p
        c = self._c(tag)
        if sys.platform != "win32":
            return {"army": [], "navy": []}
        cc = p.u64(c.ptr + 0xFF0)
        if not cc or not (0x10000000000 < cc < 0x7F0000000000):
            return {"army": [], "navy": []}
        army = [x for x in _arr(p, cc, 0x70) if 0x10000000000 < x < 0x7F0000000000]
        navy = [x for x in _arr(p, cc, 0x88) if 0x10000000000 < x < 0x7F0000000000]
        return {"army": army, "navy": navy}

    def all_characters(self, tag=None):
        p = self.p
        c = self._c(tag)
        pref = (c.tag + "_").lower()
        out = []
        # PE 1.19.5.0: CCountry+0xFF0 is CCountryCharacters:
        #   +0x10  记录数组（实机验证每条占 16 字节：{CCharacter*, flags}，旧代码用 _arr 按 8 字节
        #          步长直接读，只拿到前半且混入 56 个非指针整数，丢掉了莫德尔/勃洛姆堡等 14 名将领！）
        #   +0x40  CAdvisor*, +0x70 CArmyLeader*, +0x88 CNavyLeader*, +0xA0 CScientist*
        # 2026-11-23 (C-8): CCountryCharacters 本身已严格按国家分桶，严禁再用 `startswith(pref)`
        # 把动态招募的随机名将领（如 'Emil Pickelhaubt'）和归国将领（如 CHI_falkenhausen）砍掉！
        if sys.platform == "win32":
            cc = p.u64(c.ptr + 0xFF0)
            if cc and (0x10000000000 < cc < 0x7F0000000000):
                data = p.u64(cc + 0x10)
                cap = p.i32(cc + 0x18)
                sz = p.i32(cc + 0x1C)
                if data and 0 < sz <= cap <= 8192:
                    # 实测 16 字节步长 {ptr, u64_flags}；按 16 字节遍历并容错检查
                    seen = set()
                    try:
                        raw = p.read(data, sz * 16)
                        for idx in range(sz):
                            v0 = struct.unpack_from("<Q", raw, idx * 16)[0]
                            if 0x10000000000 < v0 < 0x7F0000000000 and v0 not in seen:
                                seen.add(v0)
                                out.append(v0)
                    except Exception:
                        for v in _arr(p, cc, 0x10):
                            if 0x10000000000 < v < 0x7F0000000000 and v not in seen:
                                seen.add(v)
                                out.append(v)
                if out:
                    return out
        pool = []
        mgr = self._character_manager()
        if mgr:
            pool = _arr(p, mgr, 0x10)
        if not pool:
            try:
                pool = list(self.oi.instances("CCharacter"))
            except Exception:
                pool = []
        for ch in pool:
            if not (0x10000000000 < ch < 0x7F0000000000):
                continue
            k = item_name(p, ch + CH_KEY)
            if not k:
                k, _nm = self._extract_char_key_and_name(ch)
            if k and k.lower().startswith(pref):
                out.append(ch)
        return out

    def generals(self, tag=None):
        """The country's generals and admirals: name, skills, pointer."""
        p = self.p
        c = self._c(tag)
        out = []
        seen_leaders = set()

        # 2026-11-23 (C-8 根因修复): Windows 上优先 O(1) 直读 CCountryCharacters+0x70/+0x88，
        # 100% 包含所有静态历史将领 + 动态招募将领（无 TAG_ 前缀）+ 归国将领，且不依赖冷启动 OI！
        if sys.platform == "win32":
            # 建立 al_ptr -> CCharacter* 反查表（用于补充 character_ptr）
            al_to_ch = {}
            try:
                for ch in self.all_characters(tag):
                    try:
                        al0 = p.u64(ch + self.CH_ARMY_LEADER)
                        if 0x10000000000 < al0 < 0x7F0000000000:
                            al_to_ch[al0] = ch
                    except Exception:
                        continue
            except Exception:
                pass

            lptrs = self._country_leader_ptrs(tag)
            pref = (c.tag + "_").lower()
            static_defs = legality.character_defs(c.tag) if hasattr(legality, "character_defs") else {}

            for al in lptrs.get("army") or []:
                if al in seen_leaders:
                    continue
                seen_leaders.add(al)
                ch = al_to_ch.get(al)
                k = item_name(p, al + 0x20) or ""
                nm = item_name(p, al + 0x40) or ""
                if (not k or not nm) and ch:
                    ck, cnm = self._extract_char_key_and_name(ch)
                    k = k or ck or ""
                    nm = nm or cnm or ""
                if not k:
                    k = nm or ("leader_%x" % al)
                if not nm:
                    nm = k
                rk = legality.RANKS.get(p.i32(al + legality.AL_RANK), "?")
                is_dyn = (k not in static_defs) and (not k.lower().startswith(pref))
                out.append({
                    "key": k,
                    "name": nm,
                    "character_ptr": hex(ch) if ch else None,
                    "leader_ptr": hex(al),
                    "attack": p.i32(al + self.AL_ATTACK),
                    "defense": p.i32(al + self.AL_DEFENSE),
                    "planning": p.i32(al + self.AL_PLANNING),
                    "logistics": p.i32(al + self.AL_LOGISTICS),
                    "rank": rk,
                    "dynamic": is_dyn,
                })

            for nl in lptrs.get("navy") or []:
                if nl in seen_leaders:
                    continue
                seen_leaders.add(nl)
                ch = al_to_ch.get(nl)
                k = item_name(p, nl + 0x20) or ""
                nm = item_name(p, nl + 0x40) or ""
                if (not k or not nm) and ch:
                    ck, cnm = self._extract_char_key_and_name(ch)
                    k = k or ck or ""
                    nm = nm or cnm or ""
                if not k:
                    k = nm or ("admiral_%x" % nl)
                if not nm:
                    nm = k
                is_dyn = (k not in static_defs) and (not k.lower().startswith(pref))
                out.append({
                    "key": k,
                    "name": nm,
                    "character_ptr": hex(ch) if ch else None,
                    "leader_ptr": hex(nl),
                    "attack": None,
                    "defense": None,
                    "planning": None,
                    "logistics": None,
                    "rank": "admiral",
                    "skills_verified": False,
                    "dynamic": is_dyn,
                })

        if not out:
            try:
                chars = self.all_characters(tag)
            except Exception:
                chars = []
            if not chars:
                chars = [int(row["ptr"], 16)
                         for row in self.characters(tag, limit=5000)["characters"]]
            for ch in chars:
                al = p.u64(ch + self.CH_ARMY_LEADER)
                if not al:
                    al = self._find_army_leader_ptr(ch)
                if not al or al in seen_leaders:
                    continue
                seen_leaders.add(al)
                if sys.platform == "win32":
                    cls = self.oi.class_of(al)
                    if cls == "CNavyLeader":
                        key, nm = self._extract_char_key_and_name(ch)
                        out.append({"key": key, "name": nm, "character_ptr": hex(ch),
                                    "leader_ptr": hex(al), "attack": None, "defense": None,
                                    "planning": None, "logistics": None,
                                    "rank": "admiral", "skills_verified": False})
                        continue
                    if cls != "CArmyLeader":
                        continue
                key, nm = self._extract_char_key_and_name(ch)
                out.append({
                    "key": key,
                    "name": nm,
                    "character_ptr": hex(ch),
                    "leader_ptr": hex(al),
                    "attack": p.i32(al + self.AL_ATTACK),
                    "defense": p.i32(al + self.AL_DEFENSE),
                    "planning": p.i32(al + self.AL_PLANNING),
                    "logistics": p.i32(al + self.AL_LOGISTICS),
                    "rank": legality.RANKS.get(p.i32(al + legality.AL_RANK), "?"),
                })

        # mark the commanders the player can actually use right now
        for row in out:
            ok, why = legality.leader_available(self, row["key"], tag)
            row["available"] = ok
            if not ok:
                row["unavailable_reason"] = why
        avail = [r for r in out if r["available"]]
        return {"count": len(out), "available_count": len(avail), "generals": out}

    def available_generals(self, tag=None, rank=None):
        """Only commanders that can be assigned right now, as the player sees them."""
        rows = [r for r in self.generals(tag)["generals"] if r["available"]]
        if rank:
            rows = [r for r in rows if r["rank"] == rank]
        # Admirals carry None skills (see generals()); they must not poison the sort.
        def _sum(r):
            return sum(v or 0 for v in (r.get("attack"), r.get("defense"),
                                        r.get("planning"), r.get("logistics")))
        rows.sort(key=lambda r: -_sum(r))
        return {"count": len(rows), "generals": rows}

    def country_characters(self, tag=None):
        """All of the country's characters: leaders, generals, admirals, advisors."""
        p = self.p
        out = []
        for ch in self.all_characters(tag):
            out.append({"key": item_name(p, ch + CH_KEY),
                        "name": item_name(p, ch + CH_NAME),
                        "is_military_leader": bool(p.u64(ch + self.CH_ARMY_LEADER)),
                        "ptr": hex(ch)})
        return {"count": len(out), "characters": out}

    def promote_general(self, character_key, tag=None):
        """Promote a corps commander to field marshal. Costs command power.

        NOTE: this used to silently do nothing. The command was given a CCharacter
        handle (type 73). The CanExecute of CPromoteUnitLeaderCommand
        (img+0x23F51A0) resolves the handle and does `cmpl $0x1, 0xD7C(obj)`,
        does. +0xD7C is CArmyLeader's RANK field (0=field marshal, 1=corps
        commander, 2=admiral). So the command wants a CArmyLeader (type 4713) and
        with the wrong type the compare never matched. The same mistake crashed the
        crashed the game once before (see FINDINGS).
        """
        p = self.p
        ch = None
        for c in self.all_characters(tag):
            if item_name(p, c + CH_KEY) == character_key:
                ch = c
                break
        if ch is None:
            raise KeyError("no such character: %s" % character_key)
        lp = p.u64(ch + 0xC0)
        if not lp:
            return {"ok": False, "error": "%s is not an army leader (no CArmyLeader)"
                                          % character_key}
        h = self.handle_of(lp)
        if h["type"] != 4713:
            return {"ok": False, "error": "beklenen CArmyLeader handle'i (4713), gelen %d"
                                          % h["type"]}
        rank = p.i32(lp + 0xD7C)
        if rank != 1:
            names = {0: "already a field marshal", 1: "a corps commander", 2: "an admiral"}
            return {"ok": False, "rank": rank,
                    "error": "only a CORPS COMMANDER can be promoted to field marshal; %s %s"
                             % (character_key, names.get(rank, "rank %d" % rank))}
        res = self.send_command("CPromoteUnitLeaderCommand",
                                {"arg0": (h["id"] << 32) | (h["type"] & 0xFFFFFFFF)}, tag)
        try:
            new_rank = p.i32(lp + 0xD7C)
        except Exception:
            new_rank = None
        if isinstance(res, dict):
            res["rank_before"] = rank
            res["rank_after"] = new_rank
            res["promoted"] = (new_rank == 0)
            if new_rank == rank:
                res["note"] = ("Rank did not change, the game refused. The most likely reason "
                               "is insufficient command power, which it rejects silently.")
        return res

    def recruit_unit_leader(self, leader_type=0, tag=None, dry_run=False):
        """Recruit a military leader (costs COMMAND POWER, not political power).

        公开参数约定（保持向后兼容）：
          - 0 / 'general' / 'corps_commander' -> 陆军军长（引擎 CCreateUnitLeaderCommand +0x2C = 1）
          - 1 / 'field_marshal'               -> 陆军元帅（引擎 CCreateUnitLeaderCommand +0x2C = 0）
          - 2 / 'admiral' / 'navy_leader'     -> 海军上将（引擎 CCreateUnitLeaderCommand +0x2C = 2）

        2026-11-23 (复测 6 P1-2): 经 PE 反汇编确认三大根因并根治：
          1. CCreateUnitLeaderCommand::ctor (0x11CDF70) 需两个参数：(CCountryTag* tag, int engine_type)，
             以前缺失 ARGSPEC 且传 {"arg0": 0} 导致 +0x28 tag_id==0 被 CanExecute (0x11EE9D0) 秒拒；
          2. 引擎实际枚举为 0=field_marshal / 1=corps_commander / 2=admiral，此处显式映射，
             确保默认 type=0 真正招出可分配给集团军的军长（corps_commander）；
          3. 消耗资源是 Command Power（CCountry+0x1F0）而非 PP；发令前实调 CanExecute，
             发令后仅在角色列表或 CP 真实变动时才返回 ok=True，杜绝假成功。
        """
        # 解析请求类型到 (role, engine_type_code)
        _str_map = {
            "general": ("corps_commander", 1, 0),
            "corps_commander": ("corps_commander", 1, 0),
            "field_marshal": ("field_marshal", 0, 1),
            "marshal": ("field_marshal", 0, 1),
            "admiral": ("admiral", 2, 2),
            "navy_leader": ("admiral", 2, 2),
        }
        _int_map = {
            0: ("corps_commander", 1),
            1: ("field_marshal", 0),
            2: ("admiral", 2),
        }
        if isinstance(leader_type, str):
            key_s = leader_type.strip().lower()
            if key_s in _str_map:
                role, engine_type, pub_type = _str_map[key_s]
            elif key_s.isdigit() and int(key_s) in _int_map:
                pub_type = int(key_s)
                role, engine_type = _int_map[pub_type]
            else:
                return {"ok": False, "blocked_by": "invalid_leader_type",
                        "error": "unknown leader_type %r (expected 0/general, 1/field_marshal, 2/admiral)" % (leader_type,)}
        else:
            pub_type = int(leader_type)
            if pub_type not in _int_map:
                return {"ok": False, "blocked_by": "invalid_leader_type",
                        "error": "leader_type must be 0 (general), 1 (field_marshal), or 2 (admiral), got %r" % (leader_type,)}
            role, engine_type = _int_map[pub_type]

        c = self._c(tag)
        try:
            cp_before = c.command_power
        except Exception:
            cp_before = None
        try:
            pp_before = round(float(c.political_power), 2)
        except Exception:
            pp_before = None
        try:
            _lp0 = self._country_leader_ptrs(c.tag) if sys.platform == "win32" else {}
            leaders_before = set(_lp0.get("army") or []) | set(_lp0.get("navy") or [])
        except Exception:
            leaders_before = set()
        try:
            chars_before = set(self.all_characters(c.tag))
        except Exception:
            chars_before = set()

        can_exec = None
        if sys.platform == "win32":
            import cmdwin
            spec = cmdwin.spec_of("CCreateUnitLeaderCommand")
            cmd = cmdwin.build_command(
                self, "CCreateUnitLeaderCommand",
                {"tag": int(c.tag_id), "leader_type": int(engine_type)},
            )
            # 实调引擎原生 CanExecute (0x11EE9D0) 进行只读预检
            try:
                can_exec_rva = spec.get("can_execute")
                if can_exec_rva:
                    can_exec = bool(self.call(self.p.base + int(can_exec_rva),
                                              (cmd, 0), absolute=True, force=True) & 0xFF)
            except Exception:
                can_exec = None
            if dry_run:
                return {
                    "ok": bool(can_exec is not False),
                    "dry_run": True,
                    "can_execute": can_exec,
                    "leader_type": pub_type,
                    "role": role,
                    "engine_type_code": engine_type,
                    "command_power_before": cp_before,
                    "pp_before": pp_before,
                    "cmd": hex(cmd),
                }
            if can_exec is False:
                return {
                    "ok": False,
                    "verified": False,
                    "blocked_by": "can_execute_refused",
                    "can_execute": False,
                    "leader_type": pub_type,
                    "role": role,
                    "engine_type_code": engine_type,
                    "command_power_before": cp_before,
                    "pp_before": pp_before,
                    "cmd": hex(cmd),
                    "error": ("engine CanExecute refused CCreateUnitLeaderCommand "
                              "(command_power=%s; recruiting a leader requires sufficient command power)"
                              % cp_before),
                }
            res = cmdwin.post_drained(self, cmd, seconds=1.5)
            res = dict(res) if isinstance(res, dict) else {"raw": res}
            res["cmd"] = hex(cmd)
            self._settle(0.2)
        else:
            if dry_run:
                return {"ok": True, "dry_run": True, "leader_type": pub_type,
                        "role": role, "engine_type_code": engine_type}
            res = self.send_command("CCreateUnitLeaderCommand",
                                    {"tag": int(c.tag_id), "leader_type": int(engine_type)}, tag)
            self._settle(0.35)

        try:
            cp_after = c.command_power
        except Exception:
            cp_after = None
        try:
            pp_after = round(float(c.political_power), 2)
        except Exception:
            pp_after = None
        cp_spent = (
            round(max(0.0, float(cp_before) - float(cp_after)), 2)
            if (cp_before is not None and cp_after is not None)
            else None
        )
        pp_spent = (
            round(max(0.0, float(pp_before) - float(pp_after)), 2)
            if (pp_before is not None and pp_after is not None)
            else None
        )

        new_ptr = None
        new_char_ptr = None
        leader_key = None
        leader_name = None
        new_rank = None
        new_stats = None
        # 2026-11-23 (C-8 根因修复): 优先直接对比 CCountryCharacters+0x70/+0x88 中的真实
        # CArmyLeader*/CNavyLeader* 差集，100% 捕获动态招募将领并返回可直接用于
        # hoi4_assign_commander 的 leader_ptr、key 与本地化名！
        try:
            if sys.platform == "win32":
                _lp1 = self._country_leader_ptrs(c.tag)
                army_after = list(_lp1.get("army") or [])
                navy_after = list(_lp1.get("navy") or [])
                new_army = [x for x in army_after if x not in leaders_before]
                new_navy = [x for x in navy_after if x not in leaders_before]
                if new_army:
                    new_ptr = new_army[-1]
                    leader_key = item_name(self.p, new_ptr + 0x20) or None
                    leader_name = item_name(self.p, new_ptr + 0x40) or leader_key
                    try:
                        new_rank = legality.RANKS.get(self.p.i32(new_ptr + legality.AL_RANK))
                        new_stats = {
                            "attack": self.p.i32(new_ptr + self.AL_ATTACK),
                            "defense": self.p.i32(new_ptr + self.AL_DEFENSE),
                            "planning": self.p.i32(new_ptr + self.AL_PLANNING),
                            "logistics": self.p.i32(new_ptr + self.AL_LOGISTICS),
                        }
                    except Exception:
                        pass
                elif new_navy:
                    new_ptr = new_navy[-1]
                    leader_key = item_name(self.p, new_ptr + 0x20) or None
                    leader_name = item_name(self.p, new_ptr + 0x40) or leader_key
                    new_rank = "admiral"
        except Exception:
            pass
        if new_ptr is None:
            try:
                chars_after = list(self.all_characters(c.tag))
                new_list = [ch for ch in chars_after if ch not in chars_before]
                if new_list:
                    new_char_ptr = new_list[-1]
                    try:
                        leader_key = item_name(self.p, new_char_ptr + CH_KEY) or None
                    except Exception:
                        leader_key = None
                    try:
                        k2, nm2 = self._extract_char_key_and_name(new_char_ptr)
                        leader_key = leader_key or k2
                        leader_name = nm2 or leader_key
                    except Exception:
                        leader_name = leader_key
                    try:
                        lp = self.p.u64(new_char_ptr + self.CH_ARMY_LEADER)
                        if lp:
                            new_ptr = lp
                            new_rank = legality.RANKS.get(self.p.i32(lp + legality.AL_RANK))
                    except Exception:
                        pass
            except Exception:
                pass

        out = dict(res) if isinstance(res, dict) else {"raw": res}
        verified = bool(
            new_ptr is not None
            or new_char_ptr is not None
            or (cp_spent is not None and cp_spent > 0.0)
            or (pp_spent is not None and pp_spent > 0.0)
        )
        out.update({
            "ok": verified,
            "verified": verified,
            "can_execute": can_exec,
            "leader_type": pub_type,
            "role": role,
            "engine_type_code": engine_type,
            "command_power_before": cp_before,
            "command_power_after": cp_after,
            "command_power_spent": cp_spent,
            "pp_before": pp_before,
            "pp_after": pp_after,
            "pp_spent": pp_spent,
            "leader_ptr": hex(new_ptr) if new_ptr else None,
            "character_ptr": hex(new_char_ptr) if new_char_ptr else None,
            "leader_key": leader_key,
            "leader_name": leader_name,
            "leader_rank": new_rank,
            "stats": new_stats,
        })
        if not verified:
            out["blocked_by"] = out.get("blocked_by") or "no_effect_observed"
            out["error"] = (
                "command posted but neither character count nor command_power changed "
                "(command_power=%s)" % cp_after
            )
        return out
