#!/usr/bin/env python3
"""方案 2.5 独立无工具强模型编排器（State-in, Plan-out）。

每回合闭环：
  1. 代码聚合器（0 LLM）：turnengine.build_turn_brief() 生成 ~1.5-2k tokens 紧凑简报；
  2. 强模型无工具单次决策（0 tool schema）：输出受限 JSON Plan；
     - 若模型请求 `inspect`（每回合硬上限 1 次，单次最多 3 条 topic），查完拼入简报再问 1 次；
  3. 事务式代码执行器：turnengine.execute_plan() 串行落地（前台守卫 + 写后验活 + 紧凑回执）；
  4. 按 `sleep` 参数推进游戏并在唤醒时自动暂停，进入下一回合。

零第三方依赖，兼容任意 OpenAI Chat Completions 接口（含 Gemini OpenAI 兼容端点）：
  HOI4_LLM_BASE_URL  默认 https://api.openai.com/v1
  HOI4_LLM_API_KEY   API Key（必填，--brief-only 模式无需）
  HOI4_LLM_MODEL     模型名（如 gemini-2.5-pro / gpt-5 / claude-sonnet-4-5）

用法示例：
  python calling/orchestrator.py --brief-only
  python calling/orchestrator.py --dry-run --turns 1
  python calling/orchestrator.py --turns 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import turnengine as TE  # noqa: E402

PROMPT_PATH = os.path.join(ROOT, "docs", "STRATEGY_PROMPT.md")


def load_system_prompt(path=PROMPT_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def extract_json_object(text):
    """从模型输出中稳健提取唯一 JSON 对象（容忍 ```json 代码块包裹）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
        if m:
            s = m.group(1).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 兜底：截取首尾大括号
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        obj = json.loads(s[i:j + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("模型输出不是合法 JSON 对象: %r" % (s[:200],))


def call_llm_json(system_prompt, user_brief, base_url, api_key, model, timeout_s=90):
    """无工具（0 tool schema）单次调用强模型，要求输出严格 JSON Plan。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_brief},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError("LLM HTTP %d: %s" % (e.code, err_body)) from e
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("LLM 返回空 choices: %r" % (body,))
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    return extract_json_object(content), usage


def run_one_turn(g, llm_fn, system_prompt, tag=None, dry_run=False,
                 max_inspect_rounds=1, do_wait=False):
    """执行方案 2.5 完整单回合（1 次主决策 + 最多 1 次 inspect 逃生舱）。"""
    extra_inspect = None
    total_llm_calls = 0
    usages = []
    plan = None

    for round_idx in range(max_inspect_rounds + 1):
        brief_res = TE.build_turn_brief(g, tag=tag, extra_inspect=extra_inspect)
        plan, usage = llm_fn(system_prompt, brief_res["markdown"])
        total_llm_calls += 1
        if usage:
            usages.append(usage)

        ok, errs = TE.validate_plan(plan)
        if not ok:
            return {
                "ok": False,
                "stage": "validate_plan",
                "errors": errs,
                "plan": plan,
                "llm_calls": total_llm_calls,
                "usages": usages,
            }

        want_inspect = plan.get("inspect") or []
        if want_inspect and round_idx < max_inspect_rounds and not (plan.get("actions") or []):
            insp_res = TE.inspect_topics(g, want_inspect, tag=tag)
            extra_inspect = insp_res.get("results")
            continue
        break

    exec_res = TE.execute_plan(g, plan, dry_run=dry_run, tag=tag)
    wait_res = None
    if do_wait and not dry_run and exec_res.get("ok"):
        slp = plan.get("sleep") or {}
        max_days = int(slp.get("max_days") or 14)
        sigs = slp.get("signals")
        wait_res = TE.wait_and_brief(
            g, max_days=max_days, signals=sigs, pause_on_wake=True, tag=tag
        )["wait"]

    return {
        "ok": bool(exec_res.get("ok")),
        "llm_calls": total_llm_calls,
        "usages": usages,
        "plan": plan,
        "execution": exec_res,
        "wait": wait_res,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="HOI4 方案 2.5 无工具强模型编排器")
    ap.add_argument("--tag", default=None, help="目标国家 tag（默认当前玩家国家）")
    ap.add_argument("--turns", type=int, default=1, help="连续运行回合数")
    ap.add_argument("--brief-only", action="store_true", help="仅生成并打印本回合紧凑简报（不调 LLM、不写游戏）")
    ap.add_argument("--dry-run", action="store_true", help="调用 LLM 生成计划并校验，但不向游戏写入任何命令")
    ap.add_argument("--no-wait", action="store_true", help="执行完动作后不推进时钟")
    ap.add_argument("--base-url", default=os.environ.get("HOI4_LLM_BASE_URL", "https://api.openai.com/v1"))
    ap.add_argument("--api-key", default=os.environ.get("HOI4_LLM_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("HOI4_LLM_MODEL", "gpt-4o"))
    args = ap.parse_args(argv)

    from game import Game
    g = Game()

    if args.brief_only:
        res = TE.build_turn_brief(g, tag=args.tag)
        print(res["markdown"])
        print("\n--- stats: approx_tokens=%d, elapsed=%.3fs ---" % (res["approx_tokens"], res["elapsed_s"]))
        return 0

    if not args.api_key:
        print("错误：未设置 HOI4_LLM_API_KEY（或传 --api-key）；如仅查看简报请使用 --brief-only", file=sys.stderr)
        return 2

    sys_prompt = load_system_prompt()

    def _llm(sp, ub):
        return call_llm_json(sp, ub, args.base_url, args.api_key, args.model)

    for i in range(args.turns):
        print("=== Turn %d/%d ===" % (i + 1, args.turns))
        out = run_one_turn(
            g, _llm, sys_prompt,
            tag=args.tag,
            dry_run=args.dry_run,
            do_wait=(not args.no_wait and i + 1 < args.turns),
        )
        print("llm_calls=%d ok=%s" % (out["llm_calls"], out["ok"]))
        if out.get("execution"):
            print(out["execution"].get("receipt_text", ""))
        if not out["ok"]:
            print("stopped:", out.get("errors") or (out.get("execution") or {}).get("error"), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
