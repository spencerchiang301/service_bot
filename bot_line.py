"""
LINE Messaging API Webhook 處理器

設定步驟：
1. 至 https://developers.line.biz 建立 Messaging API Channel
2. .env 加入：LINE_CHANNEL_SECRET=xxx  LINE_CHANNEL_ACCESS_TOKEN=xxx
3. LINE Developer Console → Webhook URL：https://你的網域/webhook/line
4. 開啟「Use webhook」，關閉「Auto-reply messages」
"""
import asyncio
import base64
import hashlib
import hmac
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response

import db
import order_flow as of
from rag import chat
from state import order_states
from ws_manager import dashboard_mgr

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

router = APIRouter(prefix="/webhook")

LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_API  = "https://api.line.me/v2/bot/message/push"


def _verify(body: bytes, signature: str, secret: str) -> bool:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


async def _download_line_image(access_token: str, message_id: str) -> str:
    """下載 LINE 圖片，存到 uploads/，回傳相對路徑。"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    ext = ".jpg"
    ct = resp.headers.get("content-type", "")
    if "png" in ct:
        ext = ".png"
    elif "gif" in ct:
        ext = ".gif"
    filename = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / filename).write_bytes(resp.content)
    return f"/files/{filename}"


async def push_line(access_token: str, user_id: str, text: str):
    """Push API — 用於客服人員非即時回覆（reply token 已過期）。"""
    async with httpx.AsyncClient() as client:
        await client.post(
            LINE_PUSH_API,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"to": user_id, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )


async def reply_line(access_token: str, reply_token: str, text: str, buttons: list[str] | None = None):
    msg: dict = {"type": "text", "text": text}
    if buttons:
        msg["quickReply"] = {
            "items": [
                {"type": "action", "action": {"type": "message", "label": b[:20], "text": b}}
                for b in buttons
            ]
        }
    async with httpx.AsyncClient() as client:
        await client.post(
            LINE_REPLY_API,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"replyToken": reply_token, "messages": [msg]},
            timeout=10,
        )


@router.post("/line")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(default=""),
):
    try:
        from config import settings
        secret = settings.line_channel_secret
        access_token = settings.line_channel_access_token
        if not secret:
            return Response(status_code=200)
    except Exception:
        raise HTTPException(status_code=500, detail="LINE 設定未完成")

    body = await request.body()
    if not _verify(body, x_line_signature, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()
    loop = asyncio.get_event_loop()

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        msg_type = event["message"].get("type")

        # ── 圖片訊息 ────────────────────────────────────────────────────────
        if msg_type == "image":
            user_id = event["source"].get("userId", "unknown")
            contact = db.get_or_create_contact("line", user_id, user_id[:12])
            conv = db.get_or_create_conversation(contact.id, "line")
            message_id = event["message"]["id"]
            media_url = await _download_line_image(access_token, message_id)
            msg = db.save_message(conv.id, "user", content="",
                                  message_type="image", media_url=media_url)
            await dashboard_mgr.broadcast({
                "type": "new_message",
                "conversation_id": conv.id,
                "contact_name": contact.name,
                "channel": "line",
                "mode": conv.mode,
                "unread": conv.unread,
                "last_message": "📷 圖片",
                "message": {"id": msg.id, "role": "user", "content": "",
                            "message_type": "image", "media_url": media_url,
                            "created_at": msg.created_at.isoformat()},
            })
            continue

        if msg_type != "text":
            continue

        text: str = event["message"]["text"].strip()
        reply_token: str = event["replyToken"]
        user_id: str = event["source"].get("userId", "unknown")

        contact = db.get_or_create_contact("line", user_id, user_id[:12])
        conv = db.get_or_create_conversation(contact.id, "line")
        msg = db.save_message(conv.id, "user", text)

        await dashboard_mgr.broadcast({
            "type": "new_message",
            "conversation_id": conv.id,
            "contact_name": contact.name,
            "channel": "line",
            "mode": conv.mode,
            "unread": conv.unread,
            "last_message": text[:80],
            "message": {"id": msg.id, "role": "user", "content": text,
                        "created_at": msg.created_at.isoformat()},
        })

        if conv.mode == "agent":
            continue

        # Bot 模式：訂單流程 or RAG
        session_key = f"line_{user_id}"
        reply = ""

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

        line_buttons = of.get_buttons(order_states[session_key]) if session_key in order_states else []
        bot_msg = db.save_message(conv.id, "bot", reply)
        await reply_line(access_token, reply_token, reply, buttons=line_buttons or None)

        await dashboard_mgr.broadcast({
            "type": "new_message",
            "conversation_id": conv.id,
            "contact_name": contact.name,
            "channel": "line",
            "mode": conv.mode,
            "unread": 0,
            "last_message": reply[:80],
            "message": {"id": bot_msg.id, "role": "bot", "content": reply,
                        "created_at": bot_msg.created_at.isoformat()},
        })

    return Response(status_code=200)
