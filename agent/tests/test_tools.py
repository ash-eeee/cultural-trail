"""工具單元測試（不需要 Ollama）。"""
import pytest

from app import data
from app.geo import haversine_m
from app.tools import ToolArgError, ollama_tools, run_tool, validate_args


def ids(result):
    return [r["id"] for r in result["results"]]


# ── data ──

def test_data_skips_broken_rows():
    locs = data.get_locations()
    assert len(locs) == 6
    assert data.get_by_id("BROKEN_1") is None


def test_parse_city_handles_postcode_and_tai():
    assert data.parse_city("100臺北市中正區") == "臺北市"
    assert data.parse_city("台北市士林區") == "臺北市"
    assert data.match_city("Tainan") == "臺南市"
    assert data.match_city("台南") == "臺南市"
    assert data.match_city("火星") is None


# ── search_locations ──

def test_search_by_city_in_query():
    r = run_tool("search_locations", {"query": "台南有哪些景點"})
    assert r["filters"]["city"] == "臺南市"
    assert set(ids(r)) == {"A_TN_1", "A_TN_2"}


def test_search_keyword_ranks_name_first():
    r = run_tool("search_locations", {"query": "安平"})
    assert ids(r)[0] in {"A_TN_1", "R_TN_1"}
    assert set(ids(r)) == {"A_TN_1", "R_TN_1"}


def test_search_english_keyword_and_city():
    r = run_tool("search_locations", {"query": "night market", "city": "Taipei"})
    assert ids(r) == ["A_TP_2"]


def test_search_category_filter():
    r = run_tool("search_locations", {"query": "", "city": "臺南", "category": "restaurant"})
    assert ids(r) == ["R_TN_1"]


def test_search_not_found_gives_alternatives():
    r = run_tool("search_locations", {"query": "火星上的餐廳"})
    assert r["count"] == 0
    assert r["status"] == "not_found" and r["cities_with_data"]


def test_search_relaxes_wrong_category():
    # 模型用 restaurant 找溫泉 → 自動放寬類別
    r = run_tool("search_locations", {"query": "溫泉", "city": "宜蘭縣", "category": "restaurant"})
    assert ids(r) == ["A_YL_1"] and r["filters"]["category"] is None and "note" in r


def test_search_unknown_city():
    r = run_tool("search_locations", {"query": "", "city": "火星市"})
    assert r["count"] == 0 and r["cities_with_data"]


def test_search_limit_is_clamped():
    r = run_tool("search_locations", {"query": "", "limit": 999})
    assert r["count"] <= 15
    r = run_tool("search_locations", {"query": "", "limit": 2})
    assert r["count"] == 2


def test_search_desc_is_truncated():
    long_desc = "長" * 500
    data.get_locations()[0]["desc"] = long_desc
    r = run_tool("search_locations", {"query": "", "limit": 15})
    assert all(len(x["desc"]) <= 81 for x in r["results"])


# ── get_location ──

def test_get_location_found():
    r = run_tool("get_location", {"id": "A_TN_1"})
    assert r["name"] == "安平古堡" and r["city"] == "臺南市"


def test_get_location_not_found():
    assert "error" in run_tool("get_location", {"id": "nope"})


# ── nearby_locations ──

def test_nearby_sorted_by_distance():
    # 安平古堡旁邊
    r = run_tool("nearby_locations", {"lat": 23.0016, "lng": 120.1606, "radius_m": 1000})
    assert ids(r) == ["A_TN_1", "R_TN_1"]
    assert r["results"][0]["distance_m"] <= r["results"][1]["distance_m"]


def test_nearby_category_and_empty():
    r = run_tool("nearby_locations", {"lat": 23.0016, "lng": 120.1606, "radius_m": 1000, "category": "restaurant"})
    assert ids(r) == ["R_TN_1"]
    r = run_tool("nearby_locations", {"lat": 0, "lng": 0})
    assert r["count"] == 0 and "note" in r


def test_haversine_known_distance():
    # 臺北車站 → 臺南車站 直線約 264 km
    d = haversine_m(25.0478, 121.5170, 22.9971, 120.2126)
    assert 258_000 < d < 270_000


# ── get_site_help ──

def test_site_help_lang_from_server():
    zh = run_tool("get_site_help", {"topic": "stamp"}, {"lang": "zh-TW"})
    en = run_tool("get_site_help", {"topic": "stamp"}, {"lang": "en"})
    assert "500 公尺" in zh["text"] and "500 m" in en["text"]


def test_site_help_all_languages():
    for lang in ("zh-TW", "en", "ja", "de"):
        for topic in ("usage", "stamp", "badge", "gps"):
            r = run_tool("get_site_help", {"topic": topic}, {"lang": lang})
            assert r["lang"] == lang and "<" not in r["text"]
    assert "500 m" in run_tool("get_site_help", {"topic": "stamp"}, {"lang": "ja"})["text"]


def test_site_help_has_no_html():
    for lang in ("zh-TW", "en", "ja", "de"):
        for topic in ("usage", "stamp", "badge", "gps"):
            assert "<" not in run_tool("get_site_help", {"topic": topic}, {"lang": lang})["text"]


# ── 參數驗證與安全 ──

def test_unknown_args_are_dropped():
    # 小模型常多送不存在的參數
    assert validate_args("search_locations", {"query": "老街", "object": "景點"}) == {"query": "老街"}


def test_empty_optional_enum_is_ignored():
    assert validate_args("search_locations", {"query": "a", "category": ""}) == {"query": "a"}


def test_bad_enum_rejected():
    assert "error" in run_tool("search_locations", {"query": "a", "category": "hotel"})


def test_missing_required():
    with pytest.raises(ToolArgError):
        validate_args("get_location", {})
    with pytest.raises(ToolArgError):
        validate_args("nearby_locations", {"lat": 23})


def test_numbers_coerced_and_clamped():
    args = validate_args("nearby_locations", {"lat": "23.0", "lng": 120, "radius_m": 10**9})
    assert args["lat"] == 23.0 and args["radius_m"] == 20000


def test_nan_rejected():
    assert "error" in run_tool("nearby_locations", {"lat": "nan", "lng": 120})


def test_unknown_tool_rejected():
    assert "error" in run_tool("unlock_stamp", {"id": "A_TN_1"})


def test_lang_cannot_be_set_by_model():
    # 模型送的 lang 不在 schema 中，會被丟棄，改用伺服器的值
    r = run_tool("get_site_help", {"topic": "usage", "lang": "en"}, {"lang": "zh-TW"})
    assert r["lang"] == "zh-TW"


def test_long_string_truncated():
    args = validate_args("search_locations", {"query": "x" * 10_000})
    assert len(args["query"]) <= 100


def test_tools_are_read_only():
    names = {t["function"]["name"] for t in ollama_tools()}
    assert names == {"search_locations", "get_location", "nearby_locations", "plan_route",
                     "estimate_carbon", "get_site_help"}
    for bad in ("stamp", "unlock", "redeem", "update", "delete", "write"):
        assert not any(bad in n for n in names)


def test_lang_detection():
    from app.lang import wrong_language
    assert not wrong_language("推薦安平古堡", "zh-TW")
    assert wrong_language("I recommend the castle", "zh-TW")
    assert not wrong_language("I recommend 安平古堡 and 林百貨 in Tainan.", "en")   # 英文中夾中文地名可以
    assert wrong_language("資料中找不到臺北市的夜市。", "en")
    assert not wrong_language("安平古堡がおすすめです。", "ja")
    assert wrong_language("推薦安平古堡", "ja")


def test_search_full_sentence_falls_back_to_city():
    r = run_tool("search_locations", {"query": "台南一日遊怎麼玩？"})
    assert r["filters"]["city"] == "臺南市" and {"A_TN_1", "A_TN_2", "R_TN_1"} == set(ids(r))


def test_search_no_city_no_match_stays_empty():
    # 沒有縣市時不放寬，避免亂推薦
    assert run_tool("search_locations", {"query": "火星上的餐廳"})["count"] == 0


def test_search_japanese_question():
    r = run_tool("search_locations", {"query": "台北の夜市を教えてください"})
    assert r["filters"]["city"] == "臺北市" and "夜市" in r["filters"]["keywords"]
    assert ids(r) == ["A_TP_2"]


def test_lang_detection_ignores_place_names():
    from app.lang import wrong_language
    text = "Here are some places:\n1. 大稻埕碼頭_大稻埕碼頭貨櫃市集\n2. 關渡_金色水岸_八里左岸自行車道\nEnjoy."
    assert wrong_language(text, "en")   # 沒拿掉地名會誤判
    assert not wrong_language(text, "en", ["大稻埕碼頭_大稻埕碼頭貨櫃市集", "關渡_金色水岸_八里左岸自行車道"])
