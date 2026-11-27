"""Windows 集团军/战斗计划薄兼容代理层（阶段 1 瘦身）。

本模块原有的 Linux ELF (1.19.4.1) 命令构造死代码已移除；
Windows 活跃实现位于 `calling/armyops_win.py`。
保留本模块作为向后兼容导出与测试 monkey-patch 入口（如 `armyops.orders_groups`）。
"""
import armyops_win as _aw
from armyops_win import (
    AL_ATTACK,
    AL_DEFENSE,
    AL_LOGISTICS,
    AL_NAME,
    AL_PLANNING,
    AL_TAG,
    CARMY_OG,
    FULL,
    OG_DIVISIONS,
    OG_HANDLE,
    OG_IS_GROUP,
    OG_LEADER,
    OG_MEMBERS,
    OG_NAME,
    OG_ORDERS,
    OG_PARENT,
    OG_THEATRE,
)

__all__ = [
    "AL_ATTACK",
    "AL_DEFENSE",
    "AL_LOGISTICS",
    "AL_NAME",
    "AL_PLANNING",
    "AL_TAG",
    "CARMY_OG",
    "FULL",
    "OG_DIVISIONS",
    "OG_HANDLE",
    "OG_IS_GROUP",
    "OG_LEADER",
    "OG_MEMBERS",
    "OG_NAME",
    "OG_ORDERS",
    "OG_PARENT",
    "OG_THEATRE",
    "orders_groups",
    "og_info",
    "leader_skills",
    "orders_of",
    "group_divisions",
    "target_line",
    "create_army",
    "create_army_group",
    "fronts_towards",
    "draw_front_line",
    "delete_order",
    "assign_to_order",
    "execute_plan",
    "offensive_line",
    "set_army_leader",
    "fallback_line",
    "naval_invasion",
    "paradrop",
    "paradrop_targets",
]


def orders_groups(g, country=None):
    return _aw.orders_groups(g, country=country)


def og_info(g, og):
    return _aw.og_info(g, og)


def leader_skills(g, army_leader):
    return _aw.leader_skills(g, army_leader)


def orders_of(g, orders_group):
    return _aw.orders_of(g, orders_group)


def group_divisions(g, orders_group, tag=None):
    return _aw.group_divisions(g, orders_group, tag=tag)


def target_line(g, enemy_tag, depth="full", own_tag=None):
    return _aw.target_line(g, enemy_tag, depth=depth, own_tag=own_tag)


def create_army(g, divisions, theatre=None):
    return _aw.create_army(g, divisions)


def create_army_group(g, armies, theatre=None):
    return _aw.create_army_group(g, armies)


def fronts_towards(g, enemy_tag, own_tag=None, refresh_if_empty=False):
    return _aw.fronts_towards(g, enemy_tag, own_tag=own_tag)


def draw_front_line(g, orders_group, front, section=None, frm=0, to=FULL,
                    divisions=None, flag=0):
    return _aw.draw_front_line(
        g, orders_group, front, section=section, frm=frm, to=to,
        divisions=divisions, flag=flag,
    )


def delete_order(g, orders_group, order_id, allow_child=False):
    return _aw.delete_order(g, orders_group, order_id, allow_child=allow_child)


def assign_to_order(g, orders_group, order_id, divisions):
    return _aw.assign_to_order(g, orders_group, order_id, divisions)


def execute_plan(g, orders_group, order_id=0, flag=0):
    return _aw.execute_plan(g, orders_group, order_id=order_id, flag=flag)


def offensive_line(g, orders_group, parent_order_id, provinces, divisions=None):
    return _aw.offensive_line(g, orders_group, parent_order_id, provinces, divisions=divisions)


def set_army_leader(g, orders_group, leader, flag=0):
    return False, "set_army_leader 仅原 Linux ELF 路径支持，Windows PE 尚未实现原生通道"


def fallback_line(g, orders_group, provinces, divisions=None):
    return False, "fallback_line 仅原 Linux ELF 路径支持，Windows PE 尚未实现原生通道"


def naval_invasion(g, orders_group, order_id, source_province, target_province,
                   divisions=None):
    return False, "naval_invasion 仅原 Linux ELF 路径支持，Windows PE 尚未实现原生通道"


def paradrop(g, orders_group, source_province, target_province,
             divisions=None, order_id=0, confirm=False, wait=8.0, dry_run=False,
             replace_existing=False, allow_duplicate=False):
    return _aw.paradrop(
        g, orders_group, source_province, target_province,
        divisions=divisions, order_id=order_id, confirm=confirm,
        wait=wait, dry_run=dry_run,
        replace_existing=replace_existing, allow_duplicate=allow_duplicate,
    )


def paradrop_targets(g, orders_group=None, source_province=None, limit=2000,
                     target_tag=None, tag=None, max_range_km=None):
    return _aw.paradrop_targets(
        g, orders_group=orders_group, source_province=source_province,
        limit=limit, target_tag=target_tag, tag=tag, max_range_km=max_range_km,
    )
