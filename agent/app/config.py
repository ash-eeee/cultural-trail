"""讀取環境變數設定（來源：agent/.env，或系統環境變數）。"""
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

AGENT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(AGENT_DIR / ".env")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _origins(raw: str) -> list[str]:
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    # 安全：不允許萬用字元，必須是明確的 http(s) 來源
    for o in origins:
        p = urlparse(o)
        if o == "*" or p.scheme not in ("http", "https") or not p.netloc or p.path:
            raise ValueError(f"ALLOWED_ORIGINS 格式錯誤：{o!r}（需為 https://example.com 這種格式，不可用 *）")
    return origins


def _ollama_host(raw: str) -> str:
    host = raw.rstrip("/")
    # 安全：Ollama 沒有驗證機制，只連本機，避免把請求送到外部
    if urlparse(host).hostname not in LOCAL_HOSTS:
        raise ValueError(f"OLLAMA_HOST 只能指向本機：{host!r}")
    return host


def _path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (AGENT_DIR / p).resolve()


OLLAMA_HOST = _ollama_host(os.getenv("OLLAMA_HOST", "http://localhost:11434"))
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
PORT = int(os.getenv("PORT", "8787"))
ALLOWED_ORIGINS = _origins(os.getenv("ALLOWED_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500"))
LOCATIONS_PATH = _path(os.getenv("LOCATIONS_PATH", "../data/locations.json"))
DEBUG = os.getenv("DEBUG", "0") == "1"
# 走 https 通道（cloudflared／ngrok）時設為 1，速率限制才會依真實使用者 IP 計算
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"
