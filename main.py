import asyncio
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
    VideoMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from yt_dlp import YoutubeDL


load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("videobot-line")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8000"))

CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILESIZE_BYTES = int(os.getenv("MAX_FILESIZE_BYTES", "50000000"))

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.warning("LINE env vars are not set: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")

if not PUBLIC_BASE_URL:
    logger.warning("PUBLIC_BASE_URL is not set. Bot will not be able to send video via URL.")


app = FastAPI(title="videobot-LINE")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
parser = WebhookParser(LINE_CHANNEL_SECRET)


URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _extract_first_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def _safe_name_from_url(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{h}.mp4"


def _preview_name_from_video_name(video_name: str) -> str:
    return Path(video_name).with_suffix(".jpg").name


def _download_video(url: str, out_path: Path) -> None:
    out_no_ext = str(out_path.with_suffix(""))
    ydl_opts = {
        "outtmpl": out_no_ext + ".%(ext)s",
        "format": "mp4/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


async def download_video(url: str) -> Path:
    filename = _safe_name_from_url(url)
    target_path = CACHE_DIR / filename

    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / filename
        await asyncio.to_thread(_download_video, url, tmp_path)

        downloaded = None
        if tmp_path.exists():
            downloaded = tmp_path
        else:
            candidates = list(Path(tmpdir).glob(tmp_path.stem + ".*"))
            if candidates:
                downloaded = max(candidates, key=lambda p: p.stat().st_size)

        if not downloaded or not downloaded.exists():
            raise RuntimeError("download produced no file")
        if downloaded.stat().st_size > MAX_FILESIZE_BYTES:
            raise RuntimeError("file_too_large")
        downloaded.replace(target_path)

    return target_path


async def ensure_preview_for_video(video_path: Path) -> Path:
    preview_path = CACHE_DIR / _preview_name_from_video_name(video_path.name)
    if preview_path.exists() and preview_path.stat().st_size > 0:
        return preview_path

    def _create_preview() -> None:
        img = Image.new("RGB", (640, 360), color=(24, 24, 24))
        img.save(preview_path, format="JPEG", quality=85)

    await asyncio.to_thread(_create_preview)
    return preview_path


def _reply_text(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
        )


def _push_text(to: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message_with_http_info(PushMessageRequest(to=to, messages=[TextMessage(text=text)]))


def _push_video(to: str, video_url: str, preview_url: str) -> None:
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message_with_http_info(
            PushMessageRequest(
                to=to,
                messages=[
                    VideoMessage(
                        original_content_url=video_url,
                        preview_image_url=preview_url,
                    )
                ],
            )
        )


async def _process_url_and_push(to: str, url: str) -> None:
    try:
        path = await download_video(url)
    except RuntimeError as e:
        err = str(e)
        if err == "file_too_large":
            msg = "Видео слишком большое для текущего лимита."
        else:
            msg = f"Ошибка скачивания: {err}"
        await asyncio.to_thread(_push_text, to, msg)
        return

    if not PUBLIC_BASE_URL:
        await asyncio.to_thread(
            _push_text,
            to,
            f"Скачал: {path.name}. Но PUBLIC_BASE_URL не настроен.",
        )
        return

    if not PUBLIC_BASE_URL.lower().startswith("https://"):
        file_url = f"{PUBLIC_BASE_URL}/files/{path.name}"
        await asyncio.to_thread(
            _push_text,
            to,
            f"Готово: {file_url}\nВажно: LINE требует HTTPS URL для video message.",
        )
        return

    preview_path = await ensure_preview_for_video(path)
    video_url = f"{PUBLIC_BASE_URL}/files/{path.name}"
    preview_url = f"{PUBLIC_BASE_URL}/files/{preview_path.name}"
    await asyncio.to_thread(_push_video, to, video_url, preview_url)


def _get_push_destination(event: MessageEvent) -> str | None:
    source = getattr(event, "source", None)
    if not source:
        return None
    return (
        getattr(source, "user_id", None)
        or getattr(source, "group_id", None)
        or getattr(source, "room_id", None)
    )


@app.post("/callback")
async def callback(
    request: Request,
    x_line_signature: str = Header(None, alias="X-Line-Signature"),
):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

    body = (await request.body()).decode("utf-8")

    try:
        events = parser.parse(body, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
                await asyncio.to_thread(
                    _reply_text,
                    event.reply_token,
                    "Бот не настроен: нет LINE_CHANNEL_ACCESS_TOKEN/LINE_CHANNEL_SECRET.",
                )
                continue

            url = _extract_first_url(event.message.text)
            if not url:
                await asyncio.to_thread(_reply_text, event.reply_token, "Пришли ссылку на видео.")
                continue

            await asyncio.to_thread(_reply_text, event.reply_token, "Скачиваю…")

            to = _get_push_destination(event)
            if to:
                asyncio.create_task(_process_url_and_push(to, url))

    return JSONResponse({"ok": True})


app.mount("/files", StaticFiles(directory=str(CACHE_DIR)), name="files")


@app.get("/health")
async def health():
    return {"ok": True}
