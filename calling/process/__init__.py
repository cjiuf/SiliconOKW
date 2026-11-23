"""Process backend selection.

Everything above this layer (api, game, the MCP tools) is platform-agnostic and
only sees the `Backend` / `Injector` interface chosen here.

  read/write   Linux: process_vm_readv / process_vm_writev
  calls        Linux: ptrace, stop every thread, hijack the main thread

Windows: ReadProcessMemory / WriteProcessMemory. The backend exists and the
read/write path is complete; `Injector.call` is still a stub (see
process/windows.py), so on Windows the read-only half of the stack runs and the
action tools raise CallError until the thread hijack is implemented.
"""
import platform

if platform.system() == "Windows":
    from . import windows as backend            # noqa: E402,F401
else:
    from . import linux as backend              # noqa: E402,F401

Backend = backend.Backend
Injector = backend.Injector
CallError = backend.CallError
find_pid = backend.find_pid
