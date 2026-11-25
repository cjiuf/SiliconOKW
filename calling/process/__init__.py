"""Process backend selection (Windows x64 PE 专用)."""
import platform

if platform.system() != "Windows":
    raise NotImplementedError(
        "Linux process backend has been removed; SiliconOKW targets Windows x64 PE."
    )

from . import windows as backend            # noqa: E402,F401

Backend = backend.Backend
Injector = backend.Injector
CallError = backend.CallError
find_pid = backend.find_pid
