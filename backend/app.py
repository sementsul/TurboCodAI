"""
AI-прокси бэкенд для фронта на GitHub Pages.

Что делает:
- /v1/chat/completions — OpenAI-совместимый прокси (стриминг). Фронт ставит сюда Base URL,
  ключ LLM прячется на сервере (.env). Достаточно, чтобы клиент с github.io работал.
- /tools/chat        — демонстрация Function Calling: модель сама вызывает реальные инструменты
  (погода, fetch_data, поиск), бэкенд их выполняет и возвращает финальный ответ.
- /health            — проверка живости.

Безопасность:
- Токен-гейт (CLIENT_TOKENS): публичный прокси с платным ключом нельзя оставлять открытым,
  иначе любой сольёт твой баланс. Фронт присылает клиентский токен в заголовке Authorization.
- CORS — только разрешённые origin (ALLOWED_ORIGINS), напр. https://USERNAME.github.io.
- Rate-limit на IP. SSRF-защита в fetch_data (нельзя ходить во внутреннюю сеть).
- Реальный ключ LLM — только в .env, никогда в коде/гите/логах.
"""
import os
import json
import time
import socket
import ipaddress
from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

# --- конфигурация из окружения (.env / панель хостинга) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
DEFAULT_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
CLIENT_TOKENS = {t.strip() for t in os.getenv("CLIENT_TOKENS", "").split(",") if t.strip()}
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()] or ["*"]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "120"))

app = FastAPI(title="AI proxy backend", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # cookie не используем — звёздочка origin безопасна
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


# ---------- чистые, тестируемые помощники ----------
def token_ok(authorization: str | None, tokens: set[str]) -> bool:
    """Проверка клиентского токена. Если токены не настроены — режим открыт (только для локалки)."""
    if not tokens:
        return True
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return token in tokens


def is_blocked_url(url: str) -> bool:
    """SSRF-защита: разрешаем только http/https к публичным адресам."""
    try:
        p = urlparse(url)
    except Exception:
        return True
    if p.scheme not in ("http", "https"):
        return True
    host = p.hostname
    if not host or host.lower() in ("localhost", "metadata.google.internal"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


class RateLimiter:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: float) -> bool:
        dq = self.hits[key]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= self.per_min:
            return False
        dq.append(now)
        return True


limiter = RateLimiter(RATE_LIMIT_PER_MIN)


def _guard(request: Request, authorization: str | None):
    if not token_ok(authorization, CLIENT_TOKENS):
        raise HTTPException(status_code=401, detail="invalid client token")
    ip = request.client.host if request.client else "unknown"
    if not limiter.allow(ip, time.time()):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="server LLM_API_KEY is not configured")


# ---------- проксирование OpenAI-совместимого чата ----------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(None)):
    _guard(request, authorization)
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise HTTPException(status_code=400, detail="messages[] required")
    body.setdefault("model", DEFAULT_MODEL)
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}

    if body.get("stream"):
        async def gen():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code >= 400:
                        txt = (await r.aread()).decode("utf-8", "ignore")[:300]
                        yield f"data: {json.dumps({'error': txt})}\n\n".encode()
                        return
                    async for chunk in r.aiter_raw():
                        yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        r = await client.post(url, headers=headers, json=body)
    return JSONResponse(status_code=r.status_code, content=r.json())


# ---------- Function Calling: реальные инструменты ----------
TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Текущая погода в городе/локации.",
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string", "description": "Город, напр. 'Moscow'"}},
            "required": ["location"]}}},
    {"type": "function", "function": {
        "name": "fetch_data",
        "description": "Скачать содержимое публичного URL (http/https).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "search_web",
        "description": "Краткий ответ из поиска (DuckDuckGo Instant Answer).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
]


async def run_tool(name: str, args: dict) -> str:
    """Выполнить инструмент. Возвращает строку-результат для модели."""
    try:
        if name == "get_weather":
            loc = str(args.get("location", "")).strip()
            if not loc:
                return "error: location required"
            async with httpx.AsyncClient(timeout=15) as c:
                g = (await c.get("https://geocoding-api.open-meteo.com/v1/search",
                                 params={"name": loc, "count": 1})).json()
                if not g.get("results"):
                    return f"город '{loc}' не найден"
                p = g["results"][0]
                w = (await c.get("https://api.open-meteo.com/v1/forecast",
                                 params={"latitude": p["latitude"], "longitude": p["longitude"],
                                         "current": "temperature_2m,wind_speed_10m"})).json()
                cur = w.get("current", {})
                return json.dumps({"location": p.get("name"), "country": p.get("country"),
                                   "temp_c": cur.get("temperature_2m"),
                                   "wind_ms": cur.get("wind_speed_10m")}, ensure_ascii=False)

        if name == "fetch_data":
            url = str(args.get("url", "")).strip()
            if is_blocked_url(url):
                return "error: URL заблокирован (только публичные http/https)"
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": "ai-proxy/1.0"})
                return r.text[:4000]

        if name == "search_web":
            q = str(args.get("query", "")).strip()
            async with httpx.AsyncClient(timeout=15) as c:
                d = (await c.get("https://api.duckduckgo.com/",
                                 params={"q": q, "format": "json", "no_html": 1})).json()
            return d.get("AbstractText") or d.get("Answer") or "ничего конкретного не найдено"

        return f"error: unknown tool {name}"
    except Exception as e:  # инструмент не должен ронять весь запрос
        return f"error: {type(e).__name__}: {e}"


@app.post("/tools/chat")
async def tools_chat(request: Request, authorization: str = Header(None)):
    _guard(request, authorization)
    body = await request.json()
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages[] required")
    model = body.get("model") or DEFAULT_MODEL
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    used = []

    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        for _ in range(5):  # ограничение глубины цикла tool-calls
            r = await client.post(url, headers=headers, json={
                "model": model, "messages": messages, "tools": TOOLS, "stream": False})
            if r.status_code >= 400:
                raise HTTPException(status_code=r.status_code, detail=r.text[:300])
            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls")
            if not calls:
                return {"response": msg.get("content", ""), "tool_calls": used}
            messages.append(msg)
            for tc in calls:
                fn = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await run_tool(fn, args)
                used.append({"name": fn, "arguments": args})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    return {"response": "(превышен лимит вызовов инструментов)", "tool_calls": used}


@app.get("/health")
async def health():
    return {"ok": True, "model": DEFAULT_MODEL, "token_gate": bool(CLIENT_TOKENS),
            "origins": ALLOWED_ORIGINS}
