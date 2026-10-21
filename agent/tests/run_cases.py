"""用真的 Ollama 跑驗收案例（tests/cases.json）。

用法（在 agent/ 資料夾，Ollama 要開著）：
    python -m tests.run_cases            # 跑 Phase 3 以前的案例
    python -m tests.run_cases --phase 4  # 跑 Phase 4 以前的案例
    python -m tests.run_cases --id 3     # 只跑某一題
"""
import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from app import agent, data, tools
from app.schemas import ChatMessage, ChatRequest

CASES = Path(__file__).parent / "cases.json"


def detect_lang(text: str) -> str:
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk > latin / 4:
        return "zh"
    if re.search(r"\b(und|der|die|das|ist|nicht|ich|Sie|du)\b|[äöüß]", text):
        return "de"
    return "en"


async def run_case(case: dict) -> tuple[bool, list[str], dict, float]:
    used = []
    original = tools.run_tool

    def recording(name, args, ctx=None):
        used.append(name)
        return original(name, args, ctx)

    agent.tools.run_tool = recording
    try:
        req = ChatRequest(messages=[ChatMessage(role="user", content=case["input"])], lang=case["lang"])
        t0 = time.monotonic()
        out = await agent.run_agent(req)
        elapsed = time.monotonic() - t0
    finally:
        agent.tools.run_tool = original

    problems = []
    reply = out["reply"]
    if case.get("expect_tools") and not set(case["expect_tools"]) & set(used):
        problems.append(f"沒有呼叫 {case['expect_tools']}（實際：{used}）")
    # 地名本來就保留中文，判斷語言前先拿掉
    names = [loc["name"] for i in out["locations"] if (loc := data.get_by_id(i))]
    plain = reply
    for n in sorted(names, key=len, reverse=True):
        plain = plain.replace(n, " ")
    if case.get("expect_lang") and detect_lang(plain) != case["expect_lang"]:
        problems.append(f"語言應為 {case['expect_lang']}，偵測到 {detect_lang(plain)}")
    if case.get("expect_any") and not any(w in reply for w in case["expect_any"]):
        problems.append(f"回覆應包含其中之一：{case['expect_any']}")
    for w in case.get("expect_none", []):
        if w in reply:
            problems.append(f"回覆不應包含：{w!r}")
    if case.get("expect_locations") and not out["locations"]:
        problems.append("應回傳可點擊的地點")
    if case.get("expect_route"):
        if not out["route"]:
            problems.append("應回傳 route")
        elif "📊" not in reply:
            problems.append("缺少系統路線摘要")
    bad = [i for i in out["locations"] if not data.get_by_id(i)]
    if bad:
        problems.append(f"回傳了不存在的地點 id：{bad}")
    return not problems, problems, {**out, "tools": used}, elapsed


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, default=3)
    ap.add_argument("--id", type=int)
    args = ap.parse_args()

    cases = [c for c in json.loads(CASES.read_text(encoding="utf-8"))
             if (c["id"] == args.id if args.id else c["phase"] <= args.phase) and "expect_lang" in c]
    passed = 0
    for c in cases:
        try:
            ok, problems, out, sec = await run_case(c)
        except Exception as e:   # 逾時或 Ollama 錯誤：記為失敗，繼續下一題
            print(f"\n❌ 案例 {c['id']} {c['input']}\n   ⚠ {type(e).__name__}")
            continue
        passed += ok
        print(f"\n{'✅' if ok else '❌'} 案例 {c['id']}（{sec:.1f}s）{c['input']}")
        print(f"   工具：{out['tools']}　地點：{[data.get_by_id(i)['name'] for i in out['locations']]}")
        print("   回覆：" + out["reply"].replace("\n", "\n         "))
        if out["route"]:
            r = out["route"]
            print(f"   路線：{[l['km'] for l in r['legs']]} km　共 {r['total_km']} km／自駕 {r['car_kgCO2e']} kg")
        for p in problems:
            print(f"   ⚠ {p}")
    print(f"\n通過 {passed}/{len(cases)}")


if __name__ == "__main__":
    asyncio.run(main())
