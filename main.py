import asyncio
import mimetypes
import os
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
SUPPORTED_DATA = {".xlsx", ".txt", ".md", ".pdf"}

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

import db
import bot_telegram
import bot_line
import order_flow as of
from config import settings
from rag import chat_stream, chat
from ws_manager import dashboard_mgr, customer_mgr
from state import order_states

app = FastAPI(title="Service Bot")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def startup():
    db.init_db()


# ── Webhook routers ───────────────────────────────────────────────────────────
app.include_router(bot_telegram.router)
app.include_router(bot_line.router)


# ── WebSocket: Dashboard (客服人員) ───────────────────────────────────────────
@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket):
    await dashboard_mgr.connect(websocket)
    try:
        await websocket.send_json({
            "type": "init",
            "conversations": db.list_conversations("open"),
        })
        while True:
            await websocket.receive_text()  # keepalive
    except WebSocketDisconnect:
        dashboard_mgr.disconnect(websocket)


# ── WebSocket: Web 客戶聊天 ───────────────────────────────────────────────────
@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    if session_id == "new":
        session_id = str(uuid.uuid4())

    await customer_mgr.connect(session_id, websocket)
    contact = db.get_or_create_contact("web", session_id)
    conv = db.get_or_create_conversation(contact.id, "web")

    try:
        await websocket.send_json({"type": "session_id", "value": session_id})

        while True:
            data = await websocket.receive_json()
            text = data.get("text", "").strip()
            if not text:
                continue

            # 儲存用戶訊息
            msg = db.save_message(conv.id, "user", content=text)
            await dashboard_mgr.broadcast({
                "type": "new_message",
                "conversation_id": conv.id,
                "contact_name": contact.name or session_id[:8],
                "channel": "web",
                "mode": conv.mode,
                "unread": conv.unread,
                "last_message": text[:80],
                "message": {"id": msg.id, "role": "user", "content": text,
                            "message_type": "text", "media_url": None,
                            "created_at": msg.created_at.isoformat()},
            })

            # 若已切人工模式則不回覆
            fresh_conv = db.get_conversation(conv.id)
            if fresh_conv and fresh_conv.mode == "agent":
                await websocket.send_json({"type": "agent_mode"})
                continue

            # ── 訂單流程 or RAG（包 try/except 避免例外卡住） ─────────────
            try:
                loop = asyncio.get_event_loop()
                pending_buttons: list[str] = []

                if session_id in order_states:
                    reply, new_state = of.handle_step(text, order_states[session_id])
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
                        order_states.pop(session_id, None)
                        pickup_label = "外送" if order.pickup_type == "delivery" else "外帶"
                        reply = (
                            f"✅ 訂單已建立！\n\n"
                            f"訂單編號：{order.order_no}\n"
                            f"取餐方式：{pickup_label}\n"
                            f"合計：{order.total} 元\n\n"
                            f"商家確認後會通知您，謝謝！"
                        )
                        await dashboard_mgr.broadcast({"type": "new_order", "order": db._order_dict(order)})
                        new_state = None
                    if new_state is None:
                        order_states.pop(session_id, None)
                    else:
                        order_states[session_id] = new_state
                        pending_buttons = of.get_buttons(new_state)
                    full_reply = reply

                else:
                    result = await loop.run_in_executor(None, of.detect_and_extract, text)
                    if result["intent"] == "order":
                        reply, state = of.start_order(result["items"])
                        order_states[session_id] = state
                        pending_buttons = of.get_buttons(state)
                        full_reply = reply
                    elif result["intent"] == "order_no_items":
                        full_reply = "好的，請告訴我您想訂購的品項和數量，例如：「2杯大杯拿鐵，1個起司蛋糕」"
                    else:
                        history = db.get_rag_history(conv.id)
                        full_reply = await loop.run_in_executor(None, chat, text, history)

            except Exception as exc:
                import traceback; traceback.print_exc()
                order_states.pop(session_id, None)
                full_reply = f"⚠️ 系統發生錯誤，請稍後再試"
                pending_buttons = []

            await websocket.send_json({"type": "stream_token", "value": full_reply})
            await websocket.send_json({"type": "stream_end", "buttons": pending_buttons})

            bot_msg = db.save_message(conv.id, "bot", full_reply)
            await dashboard_mgr.broadcast({
                "type": "new_message",
                "conversation_id": conv.id,
                "contact_name": contact.name or session_id[:8],
                "channel": "web",
                "mode": conv.mode,
                "unread": 0,
                "last_message": full_reply[:80],
                "message": {"id": bot_msg.id, "role": "bot", "content": full_reply,
                            "message_type": "text", "media_url": None,
                            "created_at": bot_msg.created_at.isoformat()},
            })

    except WebSocketDisconnect:
        customer_mgr.disconnect(session_id)


# ── REST API: 對話管理 ────────────────────────────────────────────────────────

@app.get("/api/conversations")
def list_conversations(status: str = "open"):
    return db.list_conversations(status)


@app.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: int):
    db.mark_read(conv_id)
    return db.list_messages(conv_id)


class ReplyBody(BaseModel):
    content: str = ""
    message_type: str = "text"   # text | image
    media_url: str = ""


@app.post("/api/conversations/{conv_id}/reply")
async def agent_reply(conv_id: int, body: ReplyBody):
    conv = db.get_conversation(conv_id)
    if not conv:
        return {"ok": False, "error": "not found"}

    contact = db.get_contact(conv.contact_id)
    msg = db.save_message(
        conv_id, "agent",
        content=body.content,
        message_type=body.message_type,
        media_url=body.media_url or None,
    )
    is_image = body.message_type == "image"

    # 依頻道發送回覆
    if conv.channel == "telegram" and settings.telegram_token:
        async with httpx.AsyncClient() as client:
            if is_image and body.media_url:
                full_url = f"http://localhost:{8000}{body.media_url}"
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_token}/sendPhoto",
                    json={"chat_id": contact.platform_id, "photo": full_url,
                          "caption": body.content},
                    timeout=10,
                )
            else:
                await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
                    json={"chat_id": contact.platform_id, "text": body.content},
                    timeout=10,
                )
    elif conv.channel == "line" and settings.line_channel_access_token:
        async with httpx.AsyncClient() as client:
            line_msg = {"type": "text", "text": body.content} if not is_image else {
                "type": "image",
                "originalContentUrl": f"http://localhost:8000{body.media_url}",
                "previewImageUrl": f"http://localhost:8000{body.media_url}",
            }
            await client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {settings.line_channel_access_token}"},
                json={"to": contact.platform_id, "messages": [line_msg]},
                timeout=10,
            )
    elif conv.channel == "web":
        await customer_mgr.send(contact.platform_id, {
            "type": "agent_message",
            "message_type": body.message_type,
            "content": body.content,
            "media_url": body.media_url,
        })

    last_msg = "📷 圖片" if is_image else body.content[:80]
    await dashboard_mgr.broadcast({
        "type": "new_message",
        "conversation_id": conv_id,
        "contact_name": contact.name,
        "channel": conv.channel,
        "mode": conv.mode,
        "unread": 0,
        "last_message": last_msg,
        "message": {
            "id": msg.id, "role": "agent",
            "content": body.content, "message_type": body.message_type,
            "media_url": body.media_url,
            "created_at": msg.created_at.isoformat(),
        },
    })
    return {"ok": True}


# ── REST API: 訂單管理 ────────────────────────────────────────────────────────

@app.get("/api/orders")
def api_list_orders(status: str = ""):
    return db.list_orders(status or None)


@app.get("/api/orders/{order_id}")
def api_get_order(order_id: int):
    o = db.get_order(order_id)
    if not o:
        raise HTTPException(status_code=404, detail="not found")
    return o


class OrderUpdateBody(BaseModel):
    status: str = ""
    payment_status: str = ""
    notes: str = ""


@app.patch("/api/orders/{order_id}")
async def api_update_order(order_id: int, body: OrderUpdateBody):
    fields = {k: v for k, v in body.model_dump().items() if v}
    o = db.update_order(order_id, **fields)
    if not o:
        raise HTTPException(status_code=404, detail="not found")
    await dashboard_mgr.broadcast({"type": "order_updated", "order": o})
    return o


class TagBody(BaseModel):
    tag: str


@app.post("/api/orders/{order_id}/tags")
async def api_add_tag(order_id: int, body: TagBody):
    o = db.order_add_tag(order_id, body.tag.strip())
    if not o:
        raise HTTPException(status_code=404, detail="not found")
    await dashboard_mgr.broadcast({"type": "order_updated", "order": o})
    return o


@app.delete("/api/orders/{order_id}/tags/{tag}")
async def api_remove_tag(order_id: int, tag: str):
    o = db.order_remove_tag(order_id, tag)
    if not o:
        raise HTTPException(status_code=404, detail="not found")
    await dashboard_mgr.broadcast({"type": "order_updated", "order": o})
    return o


# ── ──────────────────────────────────────────────────────────────────────────

class ModeBody(BaseModel):
    mode: str  # bot | agent


@app.post("/api/conversations/{conv_id}/mode")
async def set_mode(conv_id: int, body: ModeBody):
    db.set_mode(conv_id, body.mode)
    conv = db.get_conversation(conv_id)
    contact = db.get_contact(conv.contact_id) if conv else None
    await dashboard_mgr.broadcast({
        "type": "mode_changed",
        "conversation_id": conv_id,
        "mode": body.mode,
    })
    # 通知網頁客戶已切換到人工模式
    if body.mode == "agent" and conv and conv.channel == "web" and contact:
        await customer_mgr.send(contact.platform_id, {"type": "agent_mode"})
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/resolve")
async def resolve(conv_id: int):
    db.resolve_conversation(conv_id)
    await dashboard_mgr.broadcast({"type": "resolved", "conversation_id": conv_id})
    return {"ok": True}


class ConvTagBody(BaseModel):
    tag: str


@app.post("/api/conversations/{conv_id}/tags")
async def conv_add_tag(conv_id: int, body: ConvTagBody):
    result = db.conv_add_tag(conv_id, body.tag.strip())
    if not result:
        raise HTTPException(status_code=404, detail="not found")
    await dashboard_mgr.broadcast({"type": "conv_tags_updated", "conversation_id": conv_id, "tags": result["tags"]})
    return result


@app.delete("/api/conversations/{conv_id}/tags/{tag}")
async def conv_remove_tag(conv_id: int, tag: str):
    result = db.conv_remove_tag(conv_id, tag)
    if not result:
        raise HTTPException(status_code=404, detail="not found")
    await dashboard_mgr.broadcast({"type": "conv_tags_updated", "conversation_id": conv_id, "tags": result["tags"]})
    return result


@app.get("/api/conversations/{conv_id}/tags")
def conv_get_tags(conv_id: int):
    return db.conv_get_tags(conv_id)


# ── 圖片上傳 ──────────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if mime not in ALLOWED_TYPES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="只支援 jpg/png/gif/webp")
    ext = Path(file.filename or "img").suffix or ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / filename
    dest.write_bytes(await file.read())
    return {"url": f"/files/{filename}"}


@app.get("/files/{filename}")
async def get_file(filename: str):
    path = UPLOAD_DIR / filename
    if not path.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.get("/health")
def health():
    return {"status": "ok", "business": settings.business_name}


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.get("/api/admin/template")
async def download_template(format: str = "xlsx"):
    from template_gen import generate
    from fastapi.responses import Response as RawResponse
    try:
        data, filename, media_type = generate(format)
    except ValueError:
        raise HTTPException(status_code=400, detail="不支援的格式")
    return RawResponse(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/knowledge")
def admin_knowledge_status():
    """回傳 Qdrant 中實際載入的內容，依來源分組。"""
    from qdrant_client import QdrantClient
    from collections import defaultdict

    try:
        qdrant = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        # 確認 collection 存在
        names = [c.name for c in qdrant.get_collections().collections]
        if settings.collection_name not in names:
            return {"total": 0, "sources": []}

        # 用 scroll 撈所有 points（含 payload）
        groups: dict[str, list[str]] = defaultdict(list)
        offset = None
        while True:
            result, next_offset = qdrant.scroll(
                collection_name=settings.collection_name,
                with_payload=True,
                limit=100,
                offset=offset,
            )
            for point in result:
                source = point.payload.get("source", "（未知來源）")
                groups[source].append(point.payload.get("text", ""))
            if next_offset is None:
                break
            offset = next_offset

        sources = [
            {
                "source": src,
                "chunks": len(texts),
                "preview": texts[0][:120] if texts else "",
            }
            for src, texts in sorted(groups.items())
        ]
        return {"total": sum(s["chunks"] for s in sources), "sources": sources}

    except Exception as e:
        return {"total": 0, "sources": [], "error": str(e)}


@app.get("/api/admin/files")
def admin_list_files():
    files = []
    for p in DATA_DIR.iterdir():
        if p.is_file() and p.suffix in SUPPORTED_DATA:
            stat = p.stat()
            files.append({
                "name": p.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    return sorted(files, key=lambda x: x["modified"], reverse=True)


@app.post("/api/admin/files")
async def admin_upload_file(file: UploadFile = File(...)):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_DATA:
        raise HTTPException(status_code=400, detail="只支援 .xlsx .txt .md .pdf")
    dest = DATA_DIR / file.filename
    content = await file.read()
    dest.write_bytes(content)

    # 結構化格式同步寫入 KnowledgeItem（讓資料管理頁看得到）
    imported = 0
    if suffix == ".xlsx":
        imported = db.ki_import_from_excel(str(dest), file.filename)
    elif suffix == ".csv":
        imported = db.ki_import_from_csv(str(dest), file.filename)

    return {"ok": True, "name": file.filename, "imported": imported}


@app.post("/api/admin/files/reimport")
async def reimport_all_files():
    """把 data/ 裡所有 xlsx/csv 重新匯入 KnowledgeItem。"""
    total = 0
    results = []
    for p in sorted(DATA_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.suffix == ".xlsx":
            count = db.ki_import_from_excel(str(p), p.name)
            results.append({"name": p.name, "count": count})
            total += count
        elif p.suffix == ".csv":
            count = db.ki_import_from_csv(str(p), p.name)
            results.append({"name": p.name, "count": count})
            total += count
    return {"ok": True, "total": total, "files": results}


@app.delete("/api/admin/files/{filename}")
def admin_delete_file(filename: str):
    path = DATA_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404)
    path.unlink()
    return {"ok": True}


def _run_ingest(reset: bool):
    import subprocess
    args = ["python3", "ingest.py"]
    if reset:
        args.append("--reset")
    subprocess.run(args, cwd=Path(".").resolve())


@app.post("/api/admin/ingest")
async def admin_ingest(background_tasks: BackgroundTasks, reset: bool = False):
    background_tasks.add_task(_run_ingest, reset)
    return {"ok": True}


# ── Knowledge Item CRUD ───────────────────────────────────────────────────────

@app.get("/api/admin/items")
def items_list(category: str = ""):
    return db.ki_list(category or None)


class ItemBody(BaseModel):
    category: str
    col1: str = ""; col2: str = ""; col3: str = ""; col4: str = ""


@app.post("/api/admin/items")
def item_create(body: ItemBody):
    return db.ki_create(body.category, body.col1, body.col2, body.col3, body.col4)


class ItemPatch(BaseModel):
    col1: str = None; col2: str = None; col3: str = None; col4: str = None


@app.patch("/api/admin/items/{item_id}")
def item_update(item_id: int, body: ItemPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    result = db.ki_update(item_id, **fields)
    if not result:
        raise HTTPException(status_code=404)
    return result


@app.delete("/api/admin/items/{item_id}")
def item_delete(item_id: int):
    db.ki_delete(item_id)
    return {"ok": True}


def _sync_admin_panel_to_qdrant():
    """完整重建 Qdrant collection：KnowledgeItem + data/ 裡的 txt/md/pdf。"""
    import uuid as _uuid
    from openai import OpenAI
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    openai_client = OpenAI(api_key=settings.openai_api_key)
    qdrant = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)

    # 完全重建 collection，清除所有舊資料（含 ingest.py 的殘留）
    names = [c.name for c in qdrant.get_collections().collections]
    if settings.collection_name in names:
        qdrant.delete_collection(settings.collection_name)
    qdrant.create_collection(
        collection_name=settings.collection_name,
        vectors_config=VectorParams(size=1536, distance=Distance.COSINE),
    )

    all_texts: list[str] = []
    all_sources: list[str] = []

    # 1. KnowledgeItem（xlsx/csv 匯入 + 手動新增）→ source = "admin_panel"
    for text, _ in db.ki_to_text_chunks():
        all_texts.append(text)
        all_sources.append("admin_panel")

    # 2. data/ 裡的 txt / md / pdf（非結構化檔案）→ source = 檔名
    for p in sorted(DATA_DIR.iterdir()):
        if not p.is_file() or p.suffix not in (".txt", ".md", ".pdf"):
            continue
        try:
            if p.suffix == ".pdf":
                from pypdf import PdfReader
                raw = "\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
            else:
                raw = p.read_text(encoding="utf-8")
            size, overlap = 400, 60
            start = 0
            while start < len(raw):
                chunk = raw[start: start + size].strip()
                if chunk:
                    all_texts.append(chunk)
                    all_sources.append(p.name)
                start += size - overlap
        except Exception:
            pass

    if not all_texts:
        return 0

    vectors: list = []
    for i in range(0, len(all_texts), 100):
        resp = openai_client.embeddings.create(
            input=all_texts[i: i + 100], model=settings.embedding_model
        )
        vectors.extend(r.embedding for r in resp.data)

    points = [
        PointStruct(
            id=str(_uuid.uuid4()),
            vector=vectors[i],
            payload={"text": all_texts[i], "source": all_sources[i]},
        )
        for i in range(len(all_texts))
    ]
    qdrant.upsert(collection_name=settings.collection_name, points=points)
    return len(points)


@app.post("/api/admin/items/sync")
async def items_sync():
    """同步 KnowledgeItem → Qdrant，同步完成後才回傳結果。"""
    import asyncio
    count = await asyncio.get_event_loop().run_in_executor(None, _sync_admin_panel_to_qdrant)
    return {"ok": True, "count": count}


@app.post("/api/admin/items/import")
async def items_import(file: UploadFile = File(...)):
    """從 Excel 範本匯入資料到資料庫。"""
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="只支援 .xlsx")
    tmp = Path(f"/tmp/{file.filename}")
    tmp.write_bytes(await file.read())
    count = db.ki_import_from_excel(str(tmp), file.filename)
    tmp.unlink(missing_ok=True)
    return {"ok": True, "count": count}


class SettingsBody(BaseModel):
    business_name: str


@app.get("/api/admin/settings")
def admin_get_settings():
    return {"business_name": settings.business_name}


@app.post("/api/admin/settings")
def admin_update_settings(body: SettingsBody):
    env_path = Path(".env")
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("BUSINESS_NAME="):
            lines[i] = f"BUSINESS_NAME={body.business_name}"
            updated = True
            break
    if not updated:
        lines.append(f"BUSINESS_NAME={body.business_name}")
    env_path.write_text("\n".join(lines) + "\n")
    # 更新 runtime 設定（不需重啟）
    os.environ["BUSINESS_NAME"] = body.business_name
    settings.business_name = body.business_name
    return {"ok": True}


# ── Static files ──────────────────────────────────────────────────────────────
@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
async def serve_admin():
    return FileResponse("admin/index.html")

@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/", include_in_schema=False)
async def serve_dashboard():
    return FileResponse("dashboard/index.html")

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
