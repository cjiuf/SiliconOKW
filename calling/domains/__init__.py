"""Game 业务领域 Mixin 子包（拆分自 calling/game.py）。"""
from .focus import FocusMixin
from .politics import PoliticsMixin
from .research import ResearchMixin
from .economy import EconomyMixin
from .economy_build import EconomyBuildMixin
from .overview import OverviewMixin
from .stats_vars import StatsVarsMixin
from .military_land import MilitaryLandMixin
from .military_leaders import MilitaryLeadersMixin
from .battleplan import BattleplanMixin
from .battleplan_orders import BattleplanOrdersMixin
from .diplomacy_war import DiplomacyWarMixin
from .air_navy import AirNavyMixin
from .resolver import ResolverMixin

__all__ = [
    "FocusMixin",
    "PoliticsMixin",
    "ResearchMixin",
    "EconomyMixin",
    "EconomyBuildMixin",
    "OverviewMixin",
    "StatsVarsMixin",
    "MilitaryLandMixin",
    "MilitaryLeadersMixin",
    "BattleplanMixin",
    "BattleplanOrdersMixin",
    "DiplomacyWarMixin",
    "AirNavyMixin",
    "ResolverMixin",
]
