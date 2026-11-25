"""陆军寻路、前线态势、五法则微操与战区指令子包（拆分自 calling/movemicro.py）。"""
from .directives import (
    FRONT_STANCES,
    FRONT_STATE_PATH,
    TICK_HOURS,
    clear_front_directive,
    front_directives,
    set_front_directive,
    tactical_front_tick,
)
from .front_state import (
    attack_country,
    plug_border_gaps,
    wartime_status,
    wartime_step,
)
from .pathing import (
    DEFAULT_GARRISON,
    FLAG_SETS,
    deploy_to_border,
    mapdata_owner,
    move_divisions,
)

__all__ = [
    "DEFAULT_GARRISON",
    "FLAG_SETS",
    "FRONT_STANCES",
    "FRONT_STATE_PATH",
    "TICK_HOURS",
    "attack_country",
    "clear_front_directive",
    "deploy_to_border",
    "front_directives",
    "mapdata_owner",
    "move_divisions",
    "plug_border_gaps",
    "set_front_directive",
    "tactical_front_tick",
    "wartime_status",
    "wartime_step",
]
