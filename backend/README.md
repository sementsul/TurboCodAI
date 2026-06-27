# AI-прокси бэкенд (для фронта на GitHub Pages)

Прячет ключ LLM, добавляет CORS, rate-limit и Function Calling. Фронт на `github.io`
обращается сюда по сети.

```
[ Фронт: GitHub Pages ]  →  [ ЭТОТ бэкенд: Render/Railway/Fly ]  →  [ DeepSeek / LLM ]
   username.github.io          ключ в .env (секрет)                  api.deepseek.com
```

> **Важно:** GitHub Pages — только статика, бэкенд там не запускается. Бэкенд хостится
> отдельно (Render/Railway/Fly — есть бесплатные тарифы) или локально.

## Эндпоинты
- `POST /v1/chat/completions` — OpenAI-совместимый прокси (стриминг). Фронт ставит сюда Base URL.
- `POST /tools/chat` — Function Calling: модель сама зовёт инструменты (`get_weather`, `fetch_data`, `search_web`), бэкенд их выполняет. Ответ: `{response, tool_calls}`.
- `GET  /health` — статус.

## Безопасность (красная зона — секреты под ревью ПМ)
- **Токен-гейт** `CLIENT_TOKENS`: публичный прокси с платным ключом нельзя оставлять открытым — иначе любой сольёт баланс. Фронт шлёт клиентский токен в поле «API-ключ».
- **CORS** только для `ALLOWED_ORIGINS` (твой `https://USERNAME.github.io`).
- **Rate-limit** на IP. **SSRF-защита** в `fetch_data` (нельзя ходить во внутреннюю сеть).
- Реальный ключ LLM — только в `.env` / переменных хостинга. **`.env` не коммитить.**

## Локальный запуск
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # впиши LLM_API_KEY и CLIENT_TOKENS
uvicorn app:app --host 0.0.0.0 --port 8000
# проверка: curl http://localhost:8000/health
```

## Тесты (чистая логика, без сети)
```bash
pip install pytest
pytest -q
```

## Деплой на Render (бесплатно)
1. Залей этот репозиторий на GitHub.
2. Render → **New → Web Service** (или Blueprint по `render.yaml`), укажи репозиторий, `Root Directory = backend`.
3. В **Environment** впиши: `LLM_API_KEY`, `CLIENT_TOKENS`, `ALLOWED_ORIGINS=https://USERNAME.github.io`.
4. Получишь URL вида `https://ai-proxy.onrender.com`. Проверь `…/health`.

(Аналогично: Railway, Fly.io — есть готовый `Dockerfile`.)

## Подключение фронта (GitHub Pages)
В клиенте → ⚙ Настройки:
- **Провайдер:** «Свой бэкенд (ключ на сервере)».
- **Base URL:** `https://ai-proxy.onrender.com/v1`
- **API-ключ:** один из твоих `CLIENT_TOKENS` (это НЕ ключ LLM, а пропуск к прокси).

Готово — фронт на `github.io` работает, реальный ключ остаётся на сервере.
