# videobot-LINE

## Запуск

- Установить зависимости:
  - `pip install -r requirements.txt`
- Создать `.env` (можно скопировать из `.env.example`)

## ENV

- `LINE_CHANNEL_ACCESS_TOKEN` (обязательно)
- `LINE_CHANNEL_SECRET` (обязательно)
- `PUBLIC_BASE_URL` (обязательно для отправки видео как Video message)
  - должен быть **HTTPS**
  - пример: `https://your-domain.com`
- `PORT` (по умолчанию: `8000`)
- `CACHE_DIR` (по умолчанию: `./cache`)
- `MAX_FILESIZE_BYTES` (по умолчанию: `50000000`)

## Команды

- Health-check: `GET /health`
- Webhook: `POST /callback`

## Принцип работы

- LINE шлёт события на `/callback`.
- Бот сразу отвечает `reply` сообщением "Скачиваю…".
- Скачивание выполняется в фоне.
- После скачивания бот отправляет `push`:
  - `Video message` (если `PUBLIC_BASE_URL` задан и он HTTPS)
  - иначе fallback — текст с URL.

## Важно про PUBLIC_BASE_URL

LINE требует публичные **HTTPS** URL для `Video message` (видео + превью). Поэтому локально без туннеля (ngrok/cloudflared) будет работать только fallback.
