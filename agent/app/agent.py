"""Agent 迴圈：呼叫 Ollama → 執行工具 → 再呼叫，直到模型給出最終回覆。（docs/agent-spec.md §6.4）"""
import asyncio
import json
import logging
import re
import time

from . import data, guard, ollama_client, tools
from .lang import wrong_language
from .prompt import LANG_ORDERS, build_system_prompt
from .schemas import ChatRequest
from .tools.plan_route import find_place_names
from .tools.search_locations import parse_query

log = logging.getLogger("agent.loop")

MAX_TOOL_ROUNDS = 5
TOTAL_TIMEOUT = 45.0
MAX_TOOL_CALLS_PER_ROUND = 4   # 避免模型一次塞一堆工具呼叫
# 日文、德文：小模型常先用中文回答再翻譯，等於寫兩次；第一次回答短一點，兩次加起來才不會超過 45 秒
SHORT_ANSWER_LANGS = {"ja", "de"}
SHORT_ANSWER_TOKENS = 140
REWRITE_MIN_SECONDS = 6         # 剩餘時間少於這個就不翻譯（寧可語言不完美，也不要逾時變成離線版）

FORCE_ANSWER = {
    "zh-TW": "工具呼叫次數已達上限，請直接用目前查到的資料回答使用者。",
    "en": "Tool call limit reached. Answer the user now using only the data already retrieved.",
    "ja": "ツールの呼び出し回数が上限に達しました。取得済みのデータだけで回答してください。",
    "de": "Das Limit für Werkzeugaufrufe ist erreicht. Antworte jetzt nur mit den bereits abgerufenen Daten.",
}

DEFAULT_CHIPS = {
    "zh-TW": ["這個網站怎麼使用？", "如何解鎖集章？", "台中有什麼夜市？", "我附近有什麼？"],
    "en": ["How do I use this site?", "How do I unlock stamps?", "Night markets in Taichung", "What's near me?"],
    "ja": ["このサイトの使い方は？", "スタンプの集め方は？", "台中の夜市は？", "近くに何がある？"],
    "de": ["Wie benutze ich die Seite?", "Wie sammle ich Stempel?", "Nachtmärkte in Taichung", "Was ist in meiner Nähe?"],
}


# 看起來是在問地點、行程或網站用法（這類問題一定要先查資料）
_NEEDS_DATA = re.compile(
    r"行程|一日遊|推薦|好玩|景點|美食|餐廳|小吃|夜市|老街|溫泉|去哪|哪裡|附近|集章|徽章|網站|怎麼用|路線|怎麼去|碳排|環保|"
    r"trip|visit|itinerary|recommend|where|near|stamp|badge|"
    r"観光|おすすめ|何が|どこ|近く|スタンプ|"
    r"besuchen|empfehl|Sehensw|wo |Nähe|Stempel", re.IGNORECASE)

_ROUTE_WORDS = re.compile(r"路線|怎麼去|怎麼走|交通|碳排|環保|到.+的路|route|get to|travel from|carbon|"
                          r"行き方|ルート|Route|Weg|anreisen|Kohlendioxid|CO2", re.IGNORECASE)
_NEARBY_WORDS = re.compile(r"附近|周邊|周遭|near me|nearby|around here|近く|周辺|in der Nähe|Nähe", re.IGNORECASE)

_SITE_TOPICS = [
    ("stamp", re.compile(r"集章|解鎖|蓋章|上傳|stamp|スタンプ|Stempel", re.IGNORECASE)),
    ("badge", re.compile(r"徽章|通行證|badge|バッジ|Abzeichen", re.IGNORECASE)),
    ("gps", re.compile(r"gps|定位|位置資訊|位置情報", re.IGNORECASE)),
    ("usage", re.compile(r"網站|怎麼用|如何使用|使用方式|how (do i|to) use|使い方|benutze", re.IGNORECASE)),
]

# 翻譯指令放在 user 訊息、用中文寫並註明目標語言的中文名稱：實測小模型這樣才會真的翻（放 system 或用目標語言寫，常常原文照抄）
REWRITE_TARGET = {"en": "英文（English）", "ja": "日文（日本語）", "de": "德文（Deutsch）", "zh-TW": "繁體中文"}
REWRITE = "請把下面這段文字翻譯成{target}。[[P1]] 這類代號是地名，照抄，不要翻譯或刪除。不要增加或刪減內容。只輸出{target}翻譯。\n\n{text}"

EMPTY_REPLY = {
    "zh-TW": "抱歉，我這次沒有整理出答案，可以換個方式再問一次嗎？",
    "en": "Sorry, I couldn't put together an answer this time. Could you rephrase your question?",
    "ja": "申し訳ありません、今回はうまく回答できませんでした。言い方を変えてもう一度聞いてもらえますか？",
    "de": "Entschuldigung, diesmal konnte ich keine Antwort formulieren. Kannst du die Frage anders stellen?",
}


def needs_data(text: str) -> bool:
    _, city, category = parse_query(text)
    return bool(city or category or _NEEDS_DATA.search(text))


def _call(name: str, **args) -> dict:
    return {"function": {"name": name, "arguments": args}}


def auto_tool_call(text: str, lat: float | None = None, lng: float | None = None) -> dict:
    """伺服器依問題先查資料（小模型常不查就回答，而且多一輪呼叫在 M1 上要多 15–20 秒）。"""
    if _ROUTE_WORDS.search(text):
        names = find_place_names(text)
        if len(names) >= 2:
            return _call("plan_route", locations=names)
    if _NEARBY_WORDS.search(text) and lat is not None and lng is not None:
        return _call("nearby_locations", lat=lat, lng=lng)
    for topic, pattern in _SITE_TOPICS:
        if pattern.search(text):
            return _call("get_site_help", topic=topic)
    return _call("search_locations", query=text, limit=5)   # 結果少一點，小模型讀寫都比較快


def _collect_ids(result: dict) -> list[str]:
    """從工具結果收集地點 id（之後由 guard 再過濾一次）。"""
    ids = []
    if isinstance(result.get("id"), str):
        ids.append(result["id"])
    for r in result.get("results", []) or []:
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            ids.append(r["id"])
    return ids


def text_tool_calls(content: str) -> list[dict]:
    """llama3.2 偶爾把工具呼叫寫成文字 JSON（{"name": ..., "parameters": {...}}），
    名稱是我們的工具才當成真的工具呼叫；其他（例如 print）交給 guard 擋掉。"""
    try:
        obj = json.loads((content or "").strip())
    except ValueError:
        return []
    if not isinstance(obj, dict) or obj.get("name") not in tools.TOOLS:
        return []
    args = obj.get("parameters", obj.get("arguments"))
    return [{"function": {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}}]


async def _model(messages: list[dict], tool_defs, max_tokens, on_text) -> dict:
    """呼叫模型：有 on_text（串流模式）就邊收邊回報，否則一次取得完整回覆。"""
    if on_text is None:
        msg = await ollama_client.chat(messages, tool_defs, max_tokens=max_tokens)
    else:
        msg = await ollama_client.chat_stream(messages, tool_defs, max_tokens=max_tokens, on_content=on_text)
    if tool_defs and not msg.get("tool_calls") and (calls := text_tool_calls(msg.get("content", ""))):
        msg = {**msg, "tool_calls": calls}
    if on_text is not None and msg.get("tool_calls") and msg.get("content"):
        await on_text("")     # 這一輪其實是工具呼叫：把剛剛顯示的文字清掉
    return msg


async def _loop(req: ChatRequest, used_tools: list[str], found: dict, on_text=None) -> str:
    ctx = req.context
    messages = [{"role": "system",
                 "content": build_system_prompt(req.lang, ctx.lat if ctx else None, ctx.lng if ctx else None)}]
    messages += [{"role": m.role, "content": m.content} for m in req.recent()]
    tool_ctx = {"lang": req.lang}
    tool_defs = tools.ollama_tools()

    def run_calls(calls: list[dict], content: str = "") -> None:
        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls[:MAX_TOOL_CALLS_PER_ROUND]:
            fn = call.get("function") or {}
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            if isinstance(args, str):            # 有些模型會回傳 JSON 字串
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            result = tools.run_tool(name, args, tool_ctx)
            used_tools.append(name if name in tools.TOOLS else "invalid")
            found["ids"] += _collect_ids(result)
            if name == "plan_route" and "legs" in result:
                found["route"] = result
                found["ids"] += result["order"]
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(result, ensure_ascii=False)})

    # 需要資料的問題：先由伺服器查好，模型通常一次就能回答
    last_user = req.recent()[-1].content
    if needs_data(last_user):
        used_tools.append("auto")
        run_calls([auto_tool_call(last_user, ctx.lat if ctx else None, ctx.lng if ctx else None)])

    # 非中文時，在模型開始回答前再提醒一次語言（工具結果都是中文，小模型容易跟著用中文回答）
    lang_reminder = [] if req.lang == "zh-TW" else [{"role": "system", "content": LANG_ORDERS[req.lang]}]
    max_tokens = SHORT_ANSWER_TOKENS if req.lang in SHORT_ANSWER_LANGS else None

    for _ in range(MAX_TOOL_ROUNDS):
        msg = await _model(messages + lang_reminder, tool_defs, max_tokens, on_text)
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content", "")
        run_calls(calls, msg.get("content", ""))

    # 超過工具呼叫上限：不給工具，強制用現有資料回答
    messages.append({"role": "system", "content": FORCE_ANSWER.get(req.lang, FORCE_ANSWER["zh-TW"])})
    msg = await _model(messages, None, max_tokens, on_text)
    return msg.get("content", "")


def protect_names(text: str, names: list[str]) -> tuple[str, dict[str, str]]:
    """把地名換成 [[P1]] 這類代號，翻譯時地名才不會被模型翻掉（規格：地名保留正式中文名稱）。"""
    tokens = {}
    for name in sorted(set(names), key=len, reverse=True):   # 長的先換，避免短地名吃掉長地名的一部分
        if name and name in text:
            token = f"[[P{len(tokens) + 1}]]"
            tokens[token] = name
            text = text.replace(name, token)
    return text, tokens


def restore_names(text: str, tokens: dict[str, str]) -> str:
    for token, name in tokens.items():
        text = text.replace(token, name)
    return re.sub(r"\[\[P\d+\]\]", "", text)   # 模型自己編出來的多餘代號直接移除


async def _fix_language(text: str, lang: str, names: list[str]) -> str:
    """回覆語言不對時，請模型翻譯一次（不給工具）；地名先換成代號保護起來。"""
    protected, tokens = protect_names(text, names)
    target = REWRITE_TARGET.get(lang, REWRITE_TARGET["zh-TW"])
    msg = await ollama_client.chat([{"role": "user", "content": REWRITE.format(target=target, text=protected)}])
    return restore_names(msg.get("content", "") or protected, tokens)


async def _answer(req: ChatRequest, used_tools: list[str], found: dict, on_text=None) -> str:
    started = time.monotonic()
    raw = await _loop(req, used_tools, found, on_text)
    time_left = TOTAL_TIMEOUT - (time.monotonic() - started)
    names = [loc["name"] for i in dict.fromkeys(found["ids"]) if (loc := data.get_by_id(i))]
    if wrong_language(raw, req.lang, names) and time_left >= REWRITE_MIN_SECONDS:
        try:
            # 只用剩下的時間翻譯；來不及就回傳原本的回答，不讓整個請求逾時
            raw = await asyncio.wait_for(_fix_language(raw, req.lang, names), time_left - 1)
            used_tools.append("rewrite_lang")
        except (asyncio.TimeoutError, ollama_client.OllamaError):
            used_tools.append("rewrite_skipped")
    return raw


def _public_route(route: dict | None) -> dict | None:
    """回給前端的路線：各段距離與各交通方式的大約碳排（不含建議；建議由地圖頁「我的行程」產生）。"""
    if not route:
        return None
    return {
        "order": route["order"],
        "legs": [{"from": l["from"], "to": l["to"], "km": l["km"], "est": True, "modes": l["modes"]}
                 for l in route["legs"]],
        "total_km": route["total_km"],
        "car_kgCO2e": route["car_kgCO2e"],
    }


async def run_agent(req: ChatRequest, on_preview=None) -> dict:
    """執行 Agent；Ollama 連不上或逾時時丟出 OllamaError / asyncio.TimeoutError。
    on_preview（串流用）：模型邊寫，就把「已經過 guard 檢查」的目前文字傳出去。"""
    started = time.monotonic()
    used_tools: list[str] = []
    found: dict = {"ids": [], "route": None}   # 工具查到的地點 id 與路線
    on_text = None
    if on_preview is not None:
        last_sent = [None]

        async def on_text(raw: str) -> None:
            text = guard.preview(raw, req.lang, found["ids"], found["route"] is not None)
            if text != last_sent[0]:
                last_sent[0] = text
                await on_preview(text)
    try:
        raw = await asyncio.wait_for(_answer(req, used_tools, found, on_text), TOTAL_TIMEOUT)
    finally:
        # 隱私：日誌只記錄耗時與工具名稱，不記錄對話內容與位置
        log.info("chat done in %.1fs, tools=%s", time.monotonic() - started, used_tools)

    reply = guard.fix_list_names(guard.clean_reply(raw, req.lang), found["ids"]) \
        or EMPTY_REPLY.get(req.lang, EMPTY_REPLY["zh-TW"])
    route = found["route"]
    reply = guard.finalize_route(reply, route, req.lang)
    return {
        "reply": reply,
        "chips": DEFAULT_CHIPS.get(req.lang, DEFAULT_CHIPS["zh-TW"]),
        "locations": guard.mentioned_locations(reply, found["ids"]),
        "route": _public_route(route),
        "lang": req.lang,
        "fallback": False,
    }
