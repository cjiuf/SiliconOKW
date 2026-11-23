#!/usr/bin/env python3
"""Clausewitz-MCP. Single entry point.

    python3 clausewitz.py serve        start the MCP server (stdio)
    python3 clausewitz.py doctor       check the installation
    python3 clausewitz.py status       connect to a running game and summarise it
    python3 clausewitz.py tools        list the MCP tools
    python3 clausewitz.py tables       regenerate calling/tables/*.txt
    python3 clausewitz.py shell        a Python shell with the calling layer loaded

Use `serve` when adding this to an MCP client; example configurations live in
mcp/config/.
"""
import argparse
import os
import platform
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CALLING = os.path.join(ROOT, "calling")
MCP = os.path.join(ROOT, "mcp")
sys.path.insert(0, CALLING)


def cmd_serve(args):
    cmd = [sys.executable, os.path.join(MCP, "server.py")]
    if getattr(args, "compact", False):
        cmd.append("--profile=compact")
    os.execv(sys.executable, cmd)


def cmd_brief(args):
    from game import Game
    import turnengine as TE
    g = Game()
    res = TE.build_turn_brief(g)
    sys.stdout.write(res["markdown"] + "\n")
    sys.stderr.write("[brief] approx_tokens=%d elapsed=%.3fs\n"
                     % (res["approx_tokens"], res["elapsed_s"]))
    return 0


def cmd_doctor(args):
    ok = True
    print("Clausewitz-MCP installation check")
    print("-" * 52)
    print("  python      : %s" % sys.version.split()[0])
    print("  platform    : %s (%s)" % (platform.system(), platform.machine()))
    if sys.version_info < (3, 8):
        print("  ! Python 3.8+ required"); ok = False

    import config
    game = config.find_game_dir()
    print("  game directory : %s" % (game or "NOT FOUND"))
    if not game:
        print("    -> set the HOI4_PATH environment variable"); ok = False
    else:
        exe = config.binary_path(required=False)
        print("  executable     : %s" % (exe or "none"))
        for sub in ("map/definition.csv", "common/terrain/00_terrain.txt",
                    "common/defines/00_defines.lua", "history/states"):
            p = os.path.join(game, sub)
            mark = "ok " if os.path.exists(p) else "--"
            print("    [%s] %s" % (mark, sub))
            if not os.path.exists(p):
                ok = False

    print("  caches         :")
    for name in ("cmdctors.pkl", "cmdfields.pkl", "tokens.pkl", "mapadj.pkl"):
        p = os.path.join(CALLING, name)
        print("    [%s] %-16s %s" % ("ok " if os.path.exists(p) else "--", name,
                                     ("%.1f MB" % (os.path.getsize(p) / 1e6))
                                     if os.path.exists(p) else ""))

    try:
        from mem import find_pid
        pid = find_pid(config.EXE_NAME)
        print("  running game   : %s" % (("pid %d" % pid) if pid else "none (start the game)"))
    except Exception as e:
        print("  running game   : could not check (%s)" % e)

    if platform.system() == "Windows":
        import offsets_win
        print("  windows table  : %s  (%s)" % (offsets_win.VERSION, offsets_win.BUILD_ID))
    print("-" * 52)
    print("RESULT: %s" % ("ready" if ok else "problems found"))
    return 0 if ok else 1


def cmd_status(args):
    from game import Game
    import session
    g = Game()
    g.read_only = True
    info = session.info(g)
    c = g.player()
    print("%s  --  %s (%s)" % (info["date"], info["my_country"], info["my_tag"]))
    print("  factories : %d civilian  %d military  %d dockyards"
          % (c.civilian_factories, c.military_factories, c.naval_dockyards))
    print("  political power: %d   stability: %.0f%%   war support: %.0f%%"
          % (c.political_power, c.stability_base * 100, c.war_support_base * 100))
    print("  multiplayer: %s (%d players)" % (info["is_multiplayer"], info["player_count"]))
    print("  at war: %s   active land combats: %s" % (info["at_war"], info["active_land_combats"]))
    return 0


def cmd_tools(args):
    p = os.path.join(CALLING, "tables", "tools.txt")
    if not os.path.exists(p):
        return cmd_tables(args)
    sys.stdout.write(open(p, encoding="utf-8").read())
    return 0


def cmd_tables(args):
    return subprocess.call([sys.executable,
                            os.path.join(CALLING, "tables", "_generate.py")])


def cmd_shell(args):
    import code
    banner = ("Clausewitz shell. `g` is a Game bound to the running process.\n"
              "Try: g.player().summary()   |   import combat; combat.combats(g)")
    env = {}
    try:
        from game import Game
        env["g"] = Game()
        env["g"].read_only = True
    except Exception as e:
        banner = "Could not connect to the game (%s). No `g`; modules are loaded." % e
    for m in ("combat", "doctrine", "ragemicro", "turnloop", "session", "mapdata"):
        try:
            env[m] = __import__(m)
        except Exception:
            pass
    code.interact(banner=banner, local=env)
    return 0


COMMANDS = {"serve": cmd_serve, "brief": cmd_brief, "doctor": cmd_doctor, "status": cmd_status,
            "tools": cmd_tools, "tables": cmd_tables, "shell": cmd_shell}


def main():
    ap = argparse.ArgumentParser(prog="clausewitz", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="doctor", choices=sorted(COMMANDS))
    ap.add_argument("--compact", action="store_true",
                    help="仅用于 serve：启用 compact 档位（暴露 5 个高层聚合工具，Schema ~1.5k tokens）")
    args = ap.parse_args()
    try:
        sys.exit(COMMANDS[args.command](args) or 0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
