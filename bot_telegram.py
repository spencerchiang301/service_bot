"""
Telegram Webhook 處理器

設定步驟：
1. 向 @BotFather 申請 Bot，取得 TELEGRAM_TOKEN
2. 在 .env 加入 TELEGRAM_TOKEN=xxx
3. 啟動伺服器後執行一次：
       python bot_telegram.py --setup https://你的網域
"""
import asyncio
import sys
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Request, Response

import db
import order_flow as of
from rag import chat
from state import order_states
from ws_manager import dashboard_mgr

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

router = APIRouter(prefix="/webhook")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _url(token: str, method: str) -> str:
    return TELEGRAM_API.format(token=token, method=method)


async def _download_telegram_file(token: str, file_id: str) -> str:
    """下載 Telegram 圖片，存到 uploads/，回傳相對路徑。"""
    async with httpx.AsyncClient() as client:
        info = await client.get(_url(token, f"getFile?file_id={file_id}"), timeout=10)
        file_path = info.json()["result"]["file_path"]
        img = await client.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30
        )
    ext = Path(file_path).suffix or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / filename).write_bytes(img.content)
    return f"/files/{filename}"


async def send_telegram(token: str, chat_id: str, text: str, buttons: list[str] | None = None):
    payload: dict = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = {
            "keyboard": [[{"text": b} for b in buttons]],
            "one_time_keyboard": True,
            "resize_keyboard": True,
        }
    else:
        payload["reply_markup"] = {"remove_keyboard": True}
    async with httpx.AsyncClient() as client:
        await client.post(_url(token, "sendMessage"), json=payload, timeout=10)


async def typing_action(token: str, chat_id: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            _url(token, "sendChatAction"),
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )


@router.post("/telegram")
async def telegram_webhook(request: Request):
    try:
        from config import settings
        token = settings.telegram_token
        if not token:
            return Response(status_code=200)
    except Exception:
        return Response(status_code=200)

    body = await request.json()
    message = body.get("message") or body.get("edited_message")
    if not message:
        return Response(status_code=200)

    chat_id = str(message["chat"]["id"])
    text: str = message.get("text", "").strip()
    sender = message.get("from", {})
    name = sender.get("first_name", "") + " " + sender.get("last_name", "")
    name = name.strip() or chat_id

    # ── 圖片訊息 ──────────────────────────────────────────────────────────────
    photos = message.get("photo")
    if photos:
        contact = db.get_or_create_contact("telegram", chat_id, name)
        conv = db.get_or_create_conversation(contact.id, "telegram")
        # 取最高解析度（最後一個）
        file_id = photos[-1]["file_id"]
        media_url = await _download_telegram_file(token, file_id)
        caption = message.get("caption", "")
        msg = db.save_message(conv.id, "user", content=caption,
                              message_type="image", media_url=media_url)
        await dashboard_mgr.broadcast({
            "type": "new_message",
            "conversation_id": conv.id,
            "contact_name": contact.name,
            "channel": "telegram",
            "mode": conv.mode,
            "unread": conv.unread,
            "last_message": "📷 圖片",
            "message": {"id": msg.id, "role": "user", "content": caption,
                        "message_type": "image", "media_url": media_url,
                        "created_at": msg.created_at.isoformat()},
        })
        return Response(status_code=200)

    if not text:
        return Response(status_code=200)

    if text == "/start":
        await send_telegram(token, chat_id, "您好！我是客服助理，請問有什麼可以幫您？")
        return Response(status_code=200)

    # DB: 取得或建立 contact + conversation
    contact = db.get_or_create_contact("telegram", chat_id, name)
    conv = db.get_or_create_conversation(contact.id, "telegram")

    # 儲存用戶訊息
    msg = db.save_message(conv.id, "user", text)

    # 廣播到 Dashboard
    await dashboard_mgr.broadcast({
        "type": "new_message",
        "conversation_id": conv.id,
        "contact_name": contact.name,
        "channel": "telegram",
        "mode": conv.mode,
        "unread": conv.unread,
        "last_message": text[:80],
        "message": {"id": msg.id, "role": "user", "content": text,
                    "created_at": msg.created_at.isoformat()},
    })

    if conv.mode == "agent":
        return Response(status_code=200)

    # Bot 模式：訂單流程 or RAG
    await typing_action(token, chat_id)
    loop = asyncio.get_event_loop()
    session_key = f"tg_{chat_id}"

    if session_key in order_states:
        reply, new_state = of.handle_step(text, order_states[session_key])
        if reply == "__CREATE_ORDER__" and new_state:
            order = db.create_order(
                conversation_id=conv.id,
                contact_name=new_state["contact_name"],
                contact_phone=new_state["contact_phone"],
                pickup_type=new_state.get("pickup_type", "takeout"),
                delivery_address=new_state.get("delivery_address", ""),
                items=new_state["items"],
                total=of.calc_total(new_state["items"]),
            )
            order_states.pop(session_key, None)
            pickup_label = "外送" if order.pickup_type == "delivery" else "外帶"
            reply = (
                f"✅ 訂單已建立！\n\n"
                f"訂單編號：{order.order_no}\n"
                f"取餐方式：{pickup_label}\n"
                f"合計：{order.total} 元\n\n"
                f"商家確認後會通知您，謝謝！"
            )
            await dashboard_mgr.broadcast({"type": "new_order", "order": db._order_dict(order)})
        elif new_state is None:
            order_states.pop(session_key, None)
        else:
            order_states[session_key] = new_state

    else:
        result = await loop.run_in_executor(None, of.detect_and_extract, text)
        if result["intent"] == "order":
            reply, state = of.start_order(result["items"])
            order_states[session_key] = state
        elif result["intent"] == "order_no_items":
            reply = "好的，請告訴我您想訂購的品項和數量，例如：「2杯大杯拿鐵，1個起司蛋糕」"
        else:
            history = db.get_rag_history(conv.id)
            reply = await loop.run_in_executor(None, chat, text, history)

    tg_buttons = of.get_buttons(order_states[session_key]) if session_key in order_states else []
    bot_msg = db.save_message(conv.id, "bot", reply)
    await send_telegram(token, chat_id, reply, buttons=tg_buttons or None)

    await dashboard_mgr.broadcast({
        "type": "new_message",
        "conversation_id": conv.id,
        "contact_name": contact.name,
        "channel": "telegram",
        "mode": conv.mode,
        "unread": 0,
        "last_message": reply[:80],
        "message": {"id": bot_msg.id, "role": "bot", "content": reply,
                    "created_at": bot_msg.created_at.isoformat()},
    })
    return Response(status_code=200)


# ── CLI ───────────────────────────────────────────────────────────────────────
async def setup_webhook(public_url: str):
    from config import settings
    webhook_url = f"{public_url.rstrip('/')}/webhook/telegram"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _url(settings.telegram_token, "setWebhook"),
            json={"url": webhook_url, "allowed_updates": ["message"]},
        )
    data = resp.json()
    if data.get("ok"):
        print(f"✅ Telegram webhook 已設定：{webhook_url}")
    else:
        print(f"❌ 設定失敗：{data}")


if __name__ == "__main__":
    if "--setup" in sys.argv:
        idx = sys.argv.index("--setup")
        url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else input("請輸入公開 URL：")
        asyncio.run(setup_webhook(url))
