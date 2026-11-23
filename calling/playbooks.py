# -*- coding: utf-8 -*-
"""Veteran playbooks + unconventional-breakout arsenal for HOI4 1.19.4.1.

Merged 2026-11-06 from two sources:
  * the GER timeline/production tables already validated in runs 3-5
    (`focus_sequence` with manpower/year gates, `research_by_year`, army caps);
  * the 1.15-1.19 "Götterdämmerung" PVE guide (`BREAKOUT_AND_CHEESE_ARSENAL`,
    seven majors, MINOR/DEFAULT, playbook modes).

Focus ids were verified against common/national_focus/germany.txt. Six ids that
appear in the source documents do NOT exist in 1.19.4.1 and were replaced:
  GER_expand_the_reichsbahn        -> (dropped, no such focus)
  GER_invest_in_the_steel_industry -> (dropped)
  GER_autarky                      -> GER_autarky_efforts
  GER_hermann_goring_werke         -> (dropped; GER_ascension_of_goring exists)
  GER_kdf_wagen                    -> GER_kdf_wagen_factories
  GER_synthetic_rubber             -> (dropped; no such focus)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MAJOR_TAGS = {"GER", "SOV", "USA", "ENG", "JAP", "ITA", "FRA"}

_ACTIVE_PLAYBOOK_MODES: Dict[str, str] = {}

# =============================================================================
# 五大非常规逃课与小国破局武器库
# =============================================================================
BREAKOUT_AND_CHEESE_ARSENAL: Dict[str, Dict[str, Any]] = {
    "paradrop_france_benelux": {
        "name": "伞击法国与低地跳板秒杀流",
        "target_tags": ["FRA", "BEL", "HOL", "LUX"],
        "applicable_when": ("法国尚未点出 FRA_defensive_strategems、仍挂《各自为政的政府》"
                            "(-50% 投降界限) 时；或西线陆地推进受阻、小国跨海破局时。"),
        "mechanics_principle": (
            "1936-1939 的法国只需丢失约 25%~30% 胜利点即投降。宣战前不挂战斗机升空"
            "(No-Air-Up)，AI 不派战斗机迎战，空域默认 >70% 制空权；宣战第 1 小时空降"
            "巴黎(50 VP)+加来+奥尔良+兰斯+里尔，24-48 小时内法国投降。"),
        "execution_steps": [
            "1. 研发 paratroopers，2~3 厂造 transport_plane_equipment（每 50 架投 1 个师）。",
            "2. 5 点陆军经验造 1 伞兵营(2w) 模板，训练 8~12 个。",
            "3. 航程不够先单宣荷兰/比利时拿低地机场做跳板。",
            "4. 宣战前停飞该空域己方飞机，画好空降令，宣战第 1 小时落地。",
        ],
    },
    "dutch_east_indies_1936_gambit": {
        "name": "1936 闪击荷兰 + 傀儡荷属东印度吸人力/橡胶",
        "target_tags": ["HOL", "INS"],
        "applicable_when": "1936-1937 全球紧张度 <25%（英法无法保障独立）时，缺人力/橡胶/石油的国家。",
        "mechanics_principle": ("荷兰本土极弱，荷属东印度(INS)人口数千万、橡胶石油全图最富。"
                                "对 INS 造宣称（涨紧张度更低），HOL 必参战；和谈吞并荷兰本土、"
                                "把 INS 设为傀儡，即可抽印尼人力并以 1 民工换 80 资源。"),
        "execution_steps": [
            "1. 攒够 50 PP 立即对 INS 或 HOL 造宣称。",
            "2. 等荷兰把 INS 拉进战争后再踩死荷兰本土。",
            "3. 和谈：吞并荷兰欧洲本土，INS 设傀儡/卫星。",
            "4. 德国若走《四年计划》右线，早战会激活《掠夺经济》消费品递增，需配左线。",
        ],
    },
    "early_1936_anschluss_manpower_spam": {
        "name": "1936 一营骑/步刷人力门槛秒吞奥地利",
        "target_tags": ["GER", "AUS", "CZE", "POL"],
        "applicable_when": "被 has_army_manpower 门槛卡住关键国策时（德奥 50 万 / 苏台德 72.5 万 / 但泽 95 万）。",
        "mechanics_principle": ("has_army_manpower 只统计已部署在地图上的部队人力，不要求训练满级、"
                                "也不要求换模板后装备满编。海量 2w 一营师满 20% 即下地，再一键换 9 营大模板，"
                                "在野人力瞬间跨过门槛。"),
        "execution_steps": [
            "1. 军工优先步枪，5 点陆军经验建 1 步兵营/骑兵营(2w) 模板。",
            "2. 排队 60~96 个 2w 小师，训练 20% 即 deploy_ready_conveyors(min_progress=0.20)。",
            "3. 下地后 set_army_template 换标准 Infanterie-Division。",
            "4. 立刻点 GER_anschluss / GER_demand_sudetenland / GER_danzig_or_war。",
        ],
    },
    "bathtub_sealion_uk_invasion": {
        "name": "澡盆偷渡英吉利海峡 / 北海赫尔斩首",
        "target_tags": ["ENG"],
        "applicable_when": "1939-1940 与英国交战、要在 1941 美国参战前速灭同盟国时。",
        "mechanics_principle": ("英国两个盲区：① 宣战第 1 小时皇家海军尚未出港；② 主力死守英吉利海峡，"
                                "东部北海 -> 赫尔(Hull)/纽卡斯尔(Newcastle) 常年空虚。"),
        "execution_steps": [
            "1. 北海绕后线：威廉港/鹿特丹/丹麦 -> 德国湾 -> 东部北海 -> 赫尔/纽卡斯尔。",
            "2. 海峡线：提前 70 天画登陆令，全舰队+I 型潜艇挂 Strike Force，宣战第 1 小时下海。",
            "3. 夺港后 24~48 个师海运进港，南下取伦敦。",
        ],
    },
    "order_66_military_access_kill": {
        "name": "军通杀（1.13.6+ 三大变体）",
        "target_tags": ["ALL"],
        "applicable_when": "弱小国挑战超级大国，或利用军通规则以最小代价瞬秒大国。",
        "mechanics_principle": ("拿 A 国军通，把小师铺满其首都与全部胜利点，再向与 A 绑定的第三国 B 宣战，"
                                "A 参战瞬间原地占城 24 小时投降。"),
        "version_1_19_important_notes": [
            "1.13.6+ 直接宣 A 的傀儡/被保障国会把部队强制传回首都。",
            "变体1：对与 A 同阵营的独立盟友 B 宣战，由 B 召唤 A 入战。",
            "变体2：远征军索回，开战后暂停点 Request Forces 原地刷新控制权。",
            "变体3：军通开视野 + 借道中立国贴脸包抄。",
        ],
    },
}


def _p(**kw):
    """Small helper so each major is compact."""
    return kw


# =============================================================================
# GER — operative tables (validated) + 1.15 reference data
# =============================================================================
_GER = {
    "name": "德国 1936 史实闪击流（老手标准时间轴）",
    "forbid_manual_justify_before_year": 1941,
    "justify_fallback_year": 1939,
    "manual_justify_note": (
        "德国史实线严禁手动造宣称：奥地利 GER_anschluss、捷克 GER_demand_sudetenland -> "
        "GER_fate_of_czechoslovakia、梅梅尔 GER_reassert_eastern_claims、波兰 GER_danzig_or_war、"
        "北欧 GER_operation_weserubung、苏联 GER_war_with_the_ussr 全部由国策直接触发或赠送借口。"
    ),
    "grand_doctrines": ["mobile_warfare", "battlefield_support"],
    # --- operative timeline (gates are the game's own manpower checks) ---
    "focus_sequence": [
        ("GER_remilitarize_the_rhineland",       0, 1936, True),
        ("GER_reorganize_the_wehrmacht",         0, 1936, False),
        ("GER_the_four_year_plan",               0, 1936, False),
        ("GER_autarky_efforts",                  0, 1936, False),
        ("GER_construct_the_reichsautobahn",     0, 1936, False),
        ("GER_kdf_wagen_factories",              0, 1936, False),
        ("GER_fuhrerprinzip",                    0, 1936, False),
        ("GER_party_chancellor_bormann",         0, 1936, False),
        ("GER_coal_liquefaction",                0, 1937, False),
        ("GER_establish_the_reichswerke",        0, 1937, False),
        ("GER_develop_heraeus_facilities",       0, 1937, False),
        ("GER_accelerate_the_rearmament_program", 0, 1937, False),
        ("GER_develop_modern_maneuver_warfare",  0, 1937, False),
        ("GER_adopt_new_panzer_doctrine",        0, 1937, False),
        ("GER_anschluss",                       54, 1937, True),
        ("GER_demand_sudetenland",              74, 1938, True),
        ("GER_first_vienna_award",               0, 1938, True),
        ("GER_fate_of_czechoslovakia",           0, 1938, True),
        ("GER_molotov_ribbentrop_pact",          0, 1939, True),
        ("GER_reassert_eastern_claims",          0, 1939, True),
        ("GER_danzig_or_war",                  100, 1939, True),
        ("GER_around_maginot",                  70, 1939, True),
        ("GER_operation_weserubung",            70, 1940, False),
        ("GER_war_with_the_ussr",               90, 1941, False),
    ],
    "focus_blacklist": [
        "GER_oppose_hitler", "GER_oppose_hitler_ww",
        "GER_danzig_for_slovakia",      # mutex with GER_danzig_or_war (germany.txt:11777)
        "GER_befriend_poland",
        # Inner-circle children auto-complete on a timer; never spend a 70-day slot
        "form_organization_todt", "autonomous_organization_todt",
        "plenipotentiary_of_armaments", "glorify_party_rallies",
        "complete_control_of_the_chancellery", "ministry_of_public_enlightenment",
        "fund_the_film_department", "totaler_krieg", "reichsleiter_goring",
        "plenipotentiary_of_the_four_year_plan", "reichsleiter_himmler",
        "expand_gestapo", "strengthen_the_waffen_ss", "hegemony_of_the_ss",
        "a_strong_successor",
    ],
    "research_by_year": {
        1936: ["electronic_mechanical_engineering", "basic_machine_tools", "construction1",
               "radio", "interwar_artillery", "tech_support", "tech_engineers", "tech_recon",
               "motorised_infantry"],
        1937: ["improved_machine_tools", "dispersed_industry", "dispersed_industry2",
               "construction2", "mechanical_computing", "support_weapons", "artillery1",
               "interwar_antiair", "synth_oil_experiments"],
        1938: ["computing_machine", "infantry_weapons2", "support_weapons2",
               "basic_medium_tank_chassis", "basic_medium_tank", "improved_small_airframe",
               "fighter2", "CAS2", "engines_2", "aa_lmg", "oil_processing",
               "rubber_processing", "tech_logistics_company"],
        1939: ["advanced_machine_tools", "dispersed_industry3", "construction3",
               "improved_computing_machine", "artillery4", "antiair2", "tech_engineers2",
               "tech_logistics_company2", "improved_medium_tank_chassis",
               "improved_medium_tank"],
    },
    "construction_phases": [
        {"until": (1937, 10), "building": "industrial_complex"},
        {"until": (1938, 6),  "building": "arms_factory", "min_refineries": 3},
        {"until": (1945, 12), "building": "arms_factory", "min_refineries": 4},
    ],
    "production_rules": {
        "deprioritize_equipment": ("tac_bomber", "strategic_bomber", "heavy_fighter"),
        "rifle_stockpile_soft_cap": 25000,
        "rifle_max_factories": 15,
        "ratios_normal": {"inf": 0.25, "art": 0.20, "armor": 0.25, "air": 0.30},
        "ratios_rifle_surplus": {"inf": 0.10, "art": 0.20, "armor": 0.35, "air": 0.35},
    },
    "front_guards": {"FRA": {"min_garrison_per_tile": 1, "stance": "defend_only"}},
    "army_cap": 110,
    "rifle_per_division": 600,
    # --- 1.15 reference data (not used by the operative picker) ---
    "allow_manual_justification": False,
    "manual_justify_after_year": 1941,
    "target_divisions_by_year": {1936: 65, 1937: 88, 1938: 110, 1939: 135, 1940: 165, 1941: 200},
    "manpower_gate_focuses": ["GER_anschluss", "GER_demand_sudetenland", "GER_danzig_or_war"],
    "available_cheese_tactics": ["early_1936_anschluss_manpower_spam",
                                 "dutch_east_indies_1936_gambit",
                                 "paradrop_france_benelux",
                                 "bathtub_sealion_uk_invasion"],
    "focus_priority": [
        "GER_remilitarize_the_rhineland", "GER_reorganize_the_wehrmacht", "GER_fuhrerprinzip",
        "GER_ascension_of_todt", "GER_party_chancellor_bormann",
        "GER_prioritize_economic_growth", "GER_construct_the_reichsautobahn",
        "GER_build_defense_industry", "GER_kdf_wagen_factories", "GER_housing_developments",
        "GER_industrial_expansion", "GER_workers_rights", "GER_mass_production",
        "GER_wirtschaftswunder", "GER_the_four_year_plan", "GER_autarky_efforts",
        "GER_coal_liquefaction", "GER_establish_the_reichswerke",
        "GER_develop_modern_maneuver_warfare", "GER_adopt_new_panzer_doctrine",
        "GER_advanced_panzer_research", "GER_expand_kummersdorfs_capacity",
        "GER_develop_heraeus_facilities", "GER_ascension_of_goebbels",
        "GER_anschluss", "GER_demand_sudetenland", "GER_first_vienna_award",
        "GER_fate_of_czechoslovakia", "GER_integrate_czech_manufacturers",
        "GER_reassert_eastern_claims", "GER_molotov_ribbentrop_pact",
        "GER_danzig_or_war", "GER_war_production", "GER_around_maginot",
        "GER_operation_weserubung", "GER_war_with_the_ussr",
    ],
    "forbidden_focus_keywords": [
        "oppose_hitler", "rally_the_wehrmacht", "monarchist", "proletarian",
        "reestablish_free_elections", "revive_the_kaiserreich", "expatriate_the_communists",
        "danzig_for_slovakia", "befriend_poland", "danzig_for_guarantee",
        "form_organization_todt", "autonomous_organization_todt", "plenipotentiary_of_armaments",
        "glorify_party_rallies", "complete_control_of_the_chancellery",
        "ministry_of_public_enlightenment", "fund_the_film_department", "totaler_krieg",
        "reichsleiter_goring", "plenipotentiary_of_the_four_year_plan",
        "reichsleiter_himmler", "expand_gestapo", "strengthen_the_waffen_ss",
        "hegemony_of_the_ss", "a_strong_successor",
        "plan_z", "u_boat_effort", "rebuild_the_high_seas_fleet", "trade_interdiction",
        "crossing_the_atlantic", "befriend_china", "befriend_japan",
    ],
    "hold_focus_until_capitulated": {
        "GER_around_maginot": ["POL"],
        "GER_operation_weserubung": ["POL"],
        "GER_demand_slovenia": ["POL", "FRA"],
        "GER_first_ljubljana_award": ["POL", "FRA"],
        "GER_war_with_the_ussr": ["POL", "FRA"],
    },
    "political_decisions_priority": ["seize_gold", "gold_reserves", "mefo",
                                     "war_bonds", "anti_comintern_pact"],
    "tech_priority": [
        "electronic_mechanical_engineering", "mechanical_computing", "computing_machine",
        "basic_machine_tools", "improved_machine_tools", "advanced_machine_tools",
        "concentrated_industry", "dispersed_industry", "construction1", "construction2",
        "construction3", "infantry_weapons1", "infantry_weapons2", "interwar_antiair",
        "antiair1", "antiair2", "interwar_artillery", "artillery1", "artillery2",
        "tech_support", "tech_engineers", "tech_recon", "tech_logistics_company",
        "paratroopers", "basic_medium_tank_chassis", "improved_medium_tank_chassis",
        "basic_medium_tank", "improved_medium_tank", "aa_lmg", "engines_2",
        "improved_small_airframe", "fighter2", "CAS2", "radio", "synth_oil_experiments",
    ],
    "forbidden_tech_keywords": ["battleship", "heavy_cruiser", "light_cruiser", "carrier",
                                "strategic_bomber", "large_airframe", "tactical_bomber",
                                "heavy_tank", "super_heavy", "amphibious"],
    "civ_build_until_year": 1937, "civ_build_until_month": 11, "min_civ_before_mil": 72,
    "prod_ratios": {"infantry": 0.36, "artillery": 0.14, "anti_air": 0.06, "support": 0.10,
                    "armor": 0.14, "motorized": 0.06, "fighter": 0.08, "cas": 0.06},
    "pp_spending_order": ["workhorse", "economic_reformer", "captain_of_industry",
                          "economy_law", "conscription_law", "trade_law", "industrial_concern",
                          "theorist", "army_chief", "high_command", "air_chief"],
    "preferred_ideas": ["bormann", "hess", "erhard", "schacht", "funk", "todt", "speer",
                        "goebbels", "krupp", "ig_farben", "siemens", "mauser", "rheinmetall",
                        "man", "henschel", "messerschmitt", "guderian", "manstein", "halder",
                        "beck", "brauchitsch", "rommel", "rundstedt", "fritsch", "kesselring"],
    "war_Front_priorities": [],
    "min_attack_org_ratio": 0.60, "min_attack_str_ratio": 0.65,
    "stop_attack_org_ratio": 0.30, "min_garrison_ratio": 0.12,
}


def _major(name, focus, tech, ratios, years, cheese=()):
    return {
        "name": name,
        "allow_manual_justification": False,
        "manual_justify_after_year": 1941,
        "focus_priority": focus,
        "tech_priority": tech,
        "prod_ratios": ratios,
        "target_divisions_by_year": years,
        "available_cheese_tactics": list(cheese),
        "forbidden_focus_keywords": ["civil_war", "coup", "communist", "democratic"],
        "forbidden_tech_keywords": ["super_heavy", "strategic_bomber"],
        "hold_focus_until_capitulated": {},
        "political_decisions_priority": ["war_bonds", "promises_of_peace"],
        "pp_spending_order": ["workhorse", "captain_of_industry", "economy_law",
                              "conscription_law", "industrial_concern", "army_chief"],
        "preferred_ideas": [],
        "war_Front_priorities": [],
        "min_attack_org_ratio": 0.60, "min_attack_str_ratio": 0.65,
        "stop_attack_org_ratio": 0.30, "min_garrison_ratio": 0.15,
    }


_COMMON_TECH = [
    "electronic_mechanical_engineering", "mechanical_computing", "computing_machine",
    "basic_machine_tools", "improved_machine_tools", "advanced_machine_tools",
    "concentrated_industry", "construction1", "construction2", "construction3",
    "infantry_weapons1", "infantry_weapons2", "interwar_antiair", "antiair1",
    "interwar_artillery", "artillery1", "tech_support", "tech_engineers",
    "basic_medium_tank_chassis", "improved_small_airframe", "fighter1", "fighter2", "CAS1",
]

PLAYBOOKS: Dict[str, Dict[str, Any]] = {
    "GER": _GER,
    "SOV": _major("苏联史实 PVE 大清洗优先与装甲反推剧本",
                  ["SOV_the_centre", "SOV_the_path_of_marxism_leninism",
                   "SOV_the_zinovievite_terrorist_center", "SOV_anti_soviet_trotskyist_center",
                   "SOV_the_military_conspiracy", "SOV_behead_the_snake",
                   "SOV_stalins_cult_of_personality", "SOV_heavy_industry",
                   "SOV_finish_the_five_year_plan", "SOV_third_five_year_plan",
                   "SOV_shift_to_armaments_production", "SOV_military_reorganization",
                   "SOV_lessons_of_war"],
                  _COMMON_TECH + ["paratroopers"],
                  {"infantry": 0.40, "artillery": 0.14, "anti_air": 0.08, "support": 0.10,
                   "armor": 0.14, "motorized": 0.05, "fighter": 0.05, "cas": 0.04},
                  {1936: 105, 1937: 130, 1938: 160, 1939: 195, 1940: 240, 1941: 300},
                  ("dutch_east_indies_1936_gambit", "paradrop_france_benelux")),
    "USA": _major("美国 1938 早脱大萧条与民主兵工厂剧本",
                  ["USA_continue_the_new_deal", "USA_wpa", "USA_agricultural_adjustment_act",
                   "USA_neutrality_act", "USA_war_department", "USA_selective_training_act",
                   "USA_fair_labor_standards_act", "USA_arsenal_of_democracy",
                   "USA_the_giant_wakes", "USA_two_ocean_navy_act", "USA_wartime_industry"],
                  _COMMON_TECH,
                  {"infantry": 0.30, "artillery": 0.14, "anti_air": 0.06, "support": 0.10,
                   "armor": 0.14, "motorized": 0.08, "fighter": 0.10, "cas": 0.08},
                  {1936: 36, 1937: 45, 1938: 60, 1939: 85, 1940: 130, 1941: 180}),
    "JAP": _major("日本统制派总动员与扩大在华战争剧本",
                  ["JAP_purge_the_kodoha_faction", "JAP_guide_the_zaibatsus",
                   "JAP_national_mobilization_law", "JAP_imperial_arsenals",
                   "JAP_marco_polo_bridge_incident", "JAP_secure_china",
                   "JAP_strike_south_doctrine"],
                  _COMMON_TECH,
                  {"infantry": 0.44, "artillery": 0.18, "anti_air": 0.04, "support": 0.12,
                   "armor": 0.04, "motorized": 0.04, "fighter": 0.07, "cas": 0.07},
                  {1936: 65, 1937: 85, 1938: 105, 1939: 125, 1940: 145, 1941: 165},
                  ("dutch_east_indies_1936_gambit", "bathtub_sealion_uk_invasion")),
    "ITA": _major("意大利 PVE 防偷家与地中海剧本",
                  ["ITA_ethiopian_war_logistics", "ITA_italian_highways", "ITA_industrial_growth",
                   "ITA_strengthen_the_regime", "ITA_triumph_in_africa", "ITA_pact_of_steel",
                   "ITA_army_primacy"],
                  _COMMON_TECH + ["paratroopers"],
                  {"infantry": 0.42, "artillery": 0.16, "anti_air": 0.06, "support": 0.10,
                   "armor": 0.08, "motorized": 0.06, "fighter": 0.06, "cas": 0.06},
                  {1936: 50, 1937: 62, 1938: 75, 1939: 92, 1940: 115, 1941: 135},
                  ("paradrop_france_benelux", "bathtub_sealion_uk_invasion")),
    "ENG": _major("英国影子工厂与防空制海剧本",
                  ["ENG_limited_rearmament", "ENG_shadow_scheme", "ENG_air_rearmament",
                   "ENG_royal_ordnance_factories", "ENG_steady_as_she_goes", "ENG_home_defence",
                   "ENG_military_training_act", "ENG_general_rearmament", "ENG_fighter_command"],
                  _COMMON_TECH,
                  {"infantry": 0.34, "artillery": 0.14, "anti_air": 0.08, "support": 0.10,
                   "armor": 0.10, "motorized": 0.06, "fighter": 0.12, "cas": 0.06},
                  {1936: 36, 1937: 48, 1938: 65, 1939: 85, 1940: 110, 1941: 135}),
    "FRA": _major("法国消除各自为政与阿登铁壁剧本",
                  ["FRA_form_the_popular_front", "FRA_reform_the_labour_laws",
                   "FRA_nationalize_key_industries", "FRA_form_the_state_arsenals",
                   "FRA_begin_rearmament", "FRA_defensive_focus", "FRA_defensive_strategems",
                   "FRA_extend_the_maginot_line", "FRA_army_reform"],
                  _COMMON_TECH,
                  {"infantry": 0.44, "artillery": 0.18, "anti_air": 0.10, "support": 0.12,
                   "armor": 0.06, "motorized": 0.04, "fighter": 0.06, "cas": 0.00},
                  {1936: 76, 1937: 88, 1938: 102, 1939: 120, 1940: 140}),
    "DEFAULT": {
        "name": "中小国家通用破局剧本",
        "allow_manual_justification": True,
        "manual_justify_after_year": 1936,
        "focus_sequence": [],
        "focus_blacklist": [],
        "target_divisions_by_year": {1936: 24, 1937: 36, 1938: 48, 1939: 65, 1940: 85},
        "available_cheese_tactics": list(BREAKOUT_AND_CHEESE_ARSENAL.keys()),
        "focus_priority": ["political_effort", "industrial_effort", "construction_effort",
                           "production_effort", "extra_tech_slot", "militarism",
                           "army_effort", "aviation_effort", "paratrooper_focus"],
        "forbidden_focus_keywords": ["civil_war", "coup"],
        "forbidden_tech_keywords": ["super_heavy", "strategic_bomber"],
        "hold_focus_until_capitulated": {},
        "political_decisions_priority": ["war_bonds", "promises_of_peace"],
        "tech_priority": _COMMON_TECH + ["paratroopers"],
        "prod_ratios": {"infantry": 0.48, "artillery": 0.18, "anti_air": 0.06,
                        "support": 0.12, "armor": 0.04, "motorized": 0.04,
                        "fighter": 0.08, "cas": 0.00},
        "pp_spending_order": ["workhorse", "economy_law", "conscription_law",
                              "industrial_concern", "army_chief"],
        "preferred_ideas": [], "war_Front_priorities": [],
        "min_attack_org_ratio": 0.60, "min_attack_str_ratio": 0.65,
        "stop_attack_org_ratio": 0.30, "min_garrison_ratio": 0.15,
    },
}


# =============================================================================
# API used by game.py / housekeep.py / movemicro.py
# =============================================================================
def get_playbook(tag: str) -> dict:
    return PLAYBOOKS.get(str(tag or "").upper(), PLAYBOOKS["DEFAULT"])


def get_playbook_mode(tag: str) -> str:
    t = str(tag or "").upper()
    if t in _ACTIVE_PLAYBOOK_MODES:
        return _ACTIVE_PLAYBOOK_MODES[t]
    return "historical" if t in MAJOR_TAGS else "minor_breakout"


def set_playbook_mode(tag: str, mode: str = "historical") -> dict:
    t = str(tag or "").upper()
    m = str(mode or "historical").lower()
    if m not in ("historical", "cheese_breakout", "minor_breakout"):
        return {"ok": False, "error": "invalid mode: %s" % mode}
    _ACTIVE_PLAYBOOK_MODES[t] = m
    return {"ok": True, "tag": t, "mode": m}


def focus_scores(tag: str, year: int, divisions: int, at_war: bool) -> dict:
    pb = get_playbook(tag)
    out = {}
    for idx, (fid, min_divs, min_year, need_peace) in enumerate(pb.get("focus_sequence") or []):
        if divisions < int(min_divs or 0) or year < int(min_year or 0):
            continue
        if need_peace and at_war:
            continue
        out[fid] = 2000 - idx * 5
    return out


def blacklist(tag: str) -> set:
    return set(get_playbook(tag).get("focus_blacklist") or ())


def is_focus_forbidden(tag: str, focus_id: str) -> bool:
    fid = str(focus_id or "").lower()
    for kw in get_playbook(tag).get("forbidden_focus_keywords") or ():
        if kw.lower() in fid:
            return True
    return False


def is_tech_forbidden(tag: str, tech_id: str) -> bool:
    tid = str(tech_id or "").lower()
    for kw in get_playbook(tag).get("forbidden_tech_keywords") or ():
        if kw.lower() in tid:
            return True
    return False


def get_target_divisions(tag: str, year: int) -> int:
    by_year = get_playbook(tag).get("target_divisions_by_year") or {}
    if not by_year:
        return 60
    target = by_year[sorted(by_year)[0]]
    for y in sorted(by_year):
        if year >= y:
            target = by_year[y]
    return target


def is_focus_held_by_war_state(tag, focus_id, active_enemy_tags, capitulated_tags) -> bool:
    holds = get_playbook(tag).get("hold_focus_until_capitulated") or {}
    required = holds.get(focus_id)
    if not required:
        return False
    cap = {str(t).upper() for t in (capitulated_tags or [])}
    for req in required:
        if str(req).upper() not in cap:
            return True
    return False


def get_front_allocation_rule(tag: str, active_enemy_tags) -> Optional[Dict[str, Any]]:
    enemies = {str(t).upper() for t in (active_enemy_tags or [])}
    for rule in get_playbook(tag).get("war_Front_priorities") or []:
        cond = str(rule.get("condition_enemy") or "").upper()
        if cond and cond in enemies:
            return rule
    return None


def get_breakout_tactics(tag: str, year: int = 1936, active_enemy_tags=None,
                         stalled: bool = False) -> dict:
    t = str(tag or "").upper()
    pb = get_playbook(t)
    mode = get_playbook_mode(t)
    is_minor = t not in MAJOR_TAGS
    enemies = {str(e).upper() for e in (active_enemy_tags or [])}
    allowed = pb.get("available_cheese_tactics") or list(BREAKOUT_AND_CHEESE_ARSENAL)
    rec: List[str] = []
    if is_minor and year <= 1937 and not enemies:
        rec += ["dutch_east_indies_1936_gambit", "order_66_military_access_kill"]
    if t == "GER" and year <= 1938:
        rec.append("early_1936_anschluss_manpower_spam")
    if "FRA" in enemies or (is_minor and year <= 1939):
        rec.append("paradrop_france_benelux")
    if "ENG" in enemies:
        rec.append("bathtub_sealion_uk_invasion")
    if stalled:
        for k in ("paradrop_france_benelux", "bathtub_sealion_uk_invasion"):
            if k not in rec:
                rec.append(k)
    arsenal = {}
    for k in allowed:
        item = BREAKOUT_AND_CHEESE_ARSENAL.get(k)
        if not item:
            continue
        arsenal[k] = {"name": item["name"], "applicable_when": item["applicable_when"],
                      "mechanics_principle": item["mechanics_principle"],
                      "execution_steps": item.get("execution_steps", []),
                      "version_1_19_notes": item.get("version_1_19_important_notes", [])}
    return {"tag": t, "is_minor": is_minor, "current_mode": mode,
            "policy_note": ("小国/非常规破局模式：可主动实施早战荷兰、伞击法国、澡盆偷渡或军通杀。"
                            if (is_minor or mode != "historical" or stalled)
                            else "大国史实稳健模式：按国策树推进；死锁时可切换到 cheese_breakout。"),
            "recommended_for_current_situation": rec,
            "arsenal": arsenal}
