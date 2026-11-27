"""Calling functions inside the target process. Platform entry point.

Delegates to the active platform injector backend in `calling/process/`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process import Injector, CallError        # noqa: F401
