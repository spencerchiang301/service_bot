import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from sqlmodel import Field, Session, SQLModel, create_engine, select

# ── Models ────────────────────────────────────────────────────────────────────

class Contact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str          # telegram | line | web
    platform_id: str       # platform-specific user/chat ID
    name: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Conversation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    contact_id: int = Field(foreign_key="contact.id")
    channel: str           # telegram | line | web
    mode: str = "bot"      # bot | agent
    status: str = "open"   # open | resolved
    unread: int = 0
    last_message: str = ""
    tags_json: str = "[]"  # ["VIP", "投訴", ...]
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id")
    role: str                        # user | bot | agent
    content: str = ""
    message_type: str = "text"       # text | image
    media_url: Optional[str] = None  # 圖片相對路徑
    created_at: datetime = Field(default_factory=datetime.utcnow)


class KnowledgeItem(SQLModel, table=True):
    """商家可直接編輯的知識庫資料（source of truth）。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    category: str          # product | service | info
    col1: str = ""         # 商品名稱 / 服務名稱 / 項目
    col2: str = ""         # 規格     / 說明     / 內容
    col3: str = ""         # 價格     / 費用     / -
    col4: str = ""         # 備註     / 備註     / -
    source: str = "manual" # manual | <filename>（記錄從哪裡匯入）
    sort_order: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_no: str = ""                   # e.g. ORD-0404-001
    conversation_id: int = Field(foreign_key="conversation.id")
    contact_name: str = ""
    contact_phone: str = ""
    pickup_type: str = "takeout"         # takeout | delivery
    delivery_address: str = ""
    items_json: str = "[]"               # [{name,spec,qty,unit_price,subtotal}]
    total: int = 0
    status: str = "pending"              # pending | confirmed | completed | cancelled
    payment_status: str = "unpaid"       # unpaid | paid
    notes: str = ""
    tags_json: str = "[]"               # ["客戶電話取消", "重複下單", ...]
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── Engine ────────────────────────────────────────────────────────────────────

engine = create_engine("sqlite:///./service_bot.db", echo=False)


def init_db():
    SQLModel.metadata.create_all(engine)


# ── CRUD helpers ──────────────────────────────────────────────────────────────

def get_or_create_contact(platform: str, platform_id: str, name: str = "") -> Contact:
    with Session(engine) as s:
        contact = s.exec(
            select(Contact).where(
                Contact.platform == platform,
                Contact.platform_id == platform_id,
            )
        ).first()
        if not contact:
            contact = Contact(platform=platform, platform_id=platform_id, name=name or platform_id)
            s.add(contact)
            s.commit()
            s.refresh(contact)
        elif name and contact.name != name:
            contact.name = name
            s.add(contact)
            s.commit()
            s.refresh(contact)
        return contact


def get_or_create_conversation(contact_id: int, channel: str) -> Conversation:
    with Session(engine) as s:
        conv = s.exec(
            select(Conversation).where(
                Conversation.contact_id == contact_id,
                Conversation.status == "open",
            ).order_by(Conversation.updated_at.desc())
        ).first()
        if not conv:
            conv = Conversation(contact_id=contact_id, channel=channel)
            s.add(conv)
            s.commit()
            s.refresh(conv)
        return conv


def save_message(
    conversation_id: int,
    role: str,
    content: str = "",
    message_type: str = "text",
    media_url: Optional[str] = None,
) -> Message:
    with Session(engine) as s:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            message_type=message_type,
            media_url=media_url,
        )
        s.add(msg)
        conv = s.get(Conversation, conversation_id)
        if conv:
            conv.last_message = content[:80] if message_type == "text" else "📷 圖片"
            conv.updated_at = datetime.utcnow()
            if role == "user":
                conv.unread += 1
            s.add(conv)
        s.commit()
        s.refresh(msg)
        return msg


def get_conversation(conv_id: int) -> Optional[Conversation]:
    with Session(engine) as s:
        return s.get(Conversation, conv_id)


def get_contact(contact_id: int) -> Optional[Contact]:
    with Session(engine) as s:
        return s.get(Contact, contact_id)


def set_mode(conv_id: int, mode: str):
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        if conv:
            conv.mode = mode
            s.add(conv)
            s.commit()


def resolve_conversation(conv_id: int):
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        if conv:
            conv.status = "resolved"
            s.add(conv)
            s.commit()


def mark_read(conv_id: int):
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        if conv:
            conv.unread = 0
            s.add(conv)
            s.commit()


def conv_add_tag(conv_id: int, tag: str) -> Optional[dict]:
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        if not conv:
            return None
        tags = json.loads(conv.tags_json or "[]")
        if tag and tag not in tags:
            tags.append(tag)
            conv.tags_json = json.dumps(tags, ensure_ascii=False)
            s.add(conv)
            s.commit()
        return {"id": conv_id, "tags": json.loads(conv.tags_json)}


def conv_remove_tag(conv_id: int, tag: str) -> Optional[dict]:
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        if not conv:
            return None
        tags = [t for t in json.loads(conv.tags_json or "[]") if t != tag]
        conv.tags_json = json.dumps(tags, ensure_ascii=False)
        s.add(conv)
        s.commit()
        return {"id": conv_id, "tags": tags}


def conv_get_tags(conv_id: int) -> list[str]:
    with Session(engine) as s:
        conv = s.get(Conversation, conv_id)
        return json.loads(conv.tags_json or "[]") if conv else []


def list_conversations(status: str = "open") -> list[dict]:
    with Session(engine) as s:
        rows = s.exec(
            select(Conversation, Contact)
            .join(Contact, Conversation.contact_id == Contact.id)
            .where(Conversation.status == status)
            .order_by(Conversation.updated_at.desc())
        ).all()
        return [
            {
                "id": c.id,
                "contact_id": c.contact_id,
                "contact_name": ct.name or ct.platform_id,
                "channel": c.channel,
                "mode": c.mode,
                "status": c.status,
                "unread": c.unread,
                "last_message": c.last_message,
                "tags": json.loads(c.tags_json or "[]"),
                "updated_at": c.updated_at.isoformat(),
            }
            for c, ct in rows
        ]


def list_messages(conv_id: int) -> list[dict]:
    with Session(engine) as s:
        msgs = s.exec(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at)
        ).all()
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "message_type": m.message_type,
                "media_url": m.media_url,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ]


# ── KnowledgeItem CRUD ───────────────────────────────────────────────────────

def ki_list(category: Optional[str] = None) -> list[dict]:
    with Session(engine) as s:
        q = select(KnowledgeItem).order_by(KnowledgeItem.sort_order, KnowledgeItem.id)
        if category:
            q = q.where(KnowledgeItem.category == category)
        rows = s.exec(q).all()
        return [_ki_dict(r) for r in rows]


def ki_delete_by_source(source: str):
    """刪除指定來源的所有資料（重新匯入前清理用）。"""
    with Session(engine) as s:
        items = s.exec(select(KnowledgeItem).where(KnowledgeItem.source == source)).all()
        for item in items:
            s.delete(item)
        s.commit()


def ki_create(category: str, col1="", col2="", col3="", col4="", source="manual") -> dict:
    with Session(engine) as s:
        item = KnowledgeItem(category=category, col1=col1, col2=col2, col3=col3, col4=col4, source=source)
        s.add(item)
        s.commit()
        s.refresh(item)
        return _ki_dict(item)


def ki_update(item_id: int, **fields) -> Optional[dict]:
    with Session(engine) as s:
        item = s.get(KnowledgeItem, item_id)
        if not item:
            return None
        for k, v in fields.items():
            if hasattr(item, k):
                setattr(item, k, v)
        item.updated_at = datetime.utcnow()
        s.add(item)
        s.commit()
        s.refresh(item)
        return _ki_dict(item)


def ki_delete(item_id: int):
    with Session(engine) as s:
        item = s.get(KnowledgeItem, item_id)
        if item:
            s.delete(item)
            s.commit()


def ki_import_from_excel(path: str, filename: str = "") -> int:
    """從 Excel 範本匯入資料到 KnowledgeItem，回傳匯入筆數。"""
    import openpyxl
    source = filename or Path(path).name
    ki_delete_by_source(source)           # 先清除同檔案的舊資料
    wb = openpyxl.load_workbook(path, data_only=True)
    count = 0
    CAT_MAP = {"🛍️ 商品": "product", "🔧 服務": "service", "ℹ️ 基本資訊": "info"}
    for ws in wb.worksheets:
        cat = CAT_MAP.get(ws.title.strip())
        if not cat:
            continue
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            continue
        for row in rows[1:]:
            vals = [str(v).strip() if v is not None else "" for v in row]
            if not any(vals):
                continue
            ki_create(cat, *vals[:4], source=source)
            count += 1
    return count


def ki_import_from_csv(path: str, filename: str = "") -> int:
    """從 CSV 匯入（格式：類型,名稱,規格,價格,說明,備註）。"""
    import csv
    source = filename or Path(path).name
    ki_delete_by_source(source)
    CAT_MAP = {"商品": "product", "服務": "service", "基本資訊": "info"}
    count = 0
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith("#"):
                continue
            cat_label = row[0].strip() if row else ""
            cat = CAT_MAP.get(cat_label)
            if not cat:
                continue
            # CSV 格式：類型, 名稱, 規格/費用, 價格, 說明, 備註
            vals = [v.strip() for v in row[1:5]]
            if not any(vals):
                continue
            ki_create(cat, *vals, source=source)
            count += 1
    return count


def ki_to_text_chunks() -> list[tuple[str, str]]:
    """把所有 KnowledgeItem 轉成 (text, source) 供 embed 用。
    每個類別額外產生一個彙總 chunk，讓「有哪些商品？」之類的廣義問題也能正確檢索。
    """
    LABELS = {"product": "商品", "service": "服務", "info": "基本資訊"}
    chunks = []
    by_cat: dict[str, list[str]] = {"product": [], "service": [], "info": []}

    with Session(engine) as s:
        items = s.exec(select(KnowledgeItem).order_by(KnowledgeItem.category, KnowledgeItem.sort_order)).all()

    for item in items:
        label = LABELS.get(item.category, item.category)
        if item.category == "product":
            name = f"{item.col1}（{item.col2}）" if item.col2 else item.col1
            text = f"【{label}】{name}：售價 {item.col3} 元" if item.col3 else f"【{label}】{name}"
            if item.col4:
                text += f"。{item.col4}"
        elif item.category == "service":
            text = f"【{label}】{item.col1}：{item.col2}"
            if item.col3:
                text += f"（費用：{item.col3}）"
            if item.col4:
                text += f"。{item.col4}"
        else:  # info
            text = f"【{label}】{item.col1}：{item.col2}"
        if text.strip():
            chunks.append((text, "admin_panel"))
            by_cat.setdefault(item.category, []).append(text)

    # 各類別彙總 chunk（方便回答「有哪些商品/服務？」等全覽問題）
    for cat, lines in by_cat.items():
        if not lines:
            continue
        label = LABELS.get(cat, cat)
        summary = f"【{label}完整列表】\n" + "\n".join(f"- {l}" for l in lines)
        chunks.append((summary, "admin_panel"))

    return chunks


def _ki_dict(item: KnowledgeItem) -> dict:
    return {
        "id": item.id, "category": item.category,
        "col1": item.col1, "col2": item.col2,
        "col3": item.col3, "col4": item.col4,
        "source": item.source,
        "sort_order": item.sort_order,
        "updated_at": item.updated_at.isoformat(),
    }


def get_rag_history(conv_id: int) -> list[dict]:
    """最近 10 輪對話，轉成 OpenAI messages 格式供 RAG 使用。"""
    with Session(engine) as s:
        msgs = s.exec(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.desc())
            .limit(20)
        ).all()
    history = []
    for m in reversed(msgs):
        if m.role == "user":
            history.append({"role": "user", "content": m.content})
        elif m.role in ("bot", "agent"):
            history.append({"role": "assistant", "content": m.content})
    return history


# ── Order CRUD ────────────────────────────────────────────────────────────────

def create_order(
    conversation_id: int,
    contact_name: str,
    contact_phone: str,
    pickup_type: str,
    delivery_address: str,
    items: list[dict],
    total: int,
    notes: str = "",
) -> "Order":
    with Session(engine) as s:
        today = datetime.utcnow().strftime("%m%d")
        count = s.exec(select(Order)).all()
        order_no = f"ORD-{today}-{len(count)+1:03d}"
        order = Order(
            order_no=order_no,
            conversation_id=conversation_id,
            contact_name=contact_name,
            contact_phone=contact_phone,
            pickup_type=pickup_type,
            delivery_address=delivery_address,
            items_json=json.dumps(items, ensure_ascii=False),
            total=total,
            notes=notes,
        )
        s.add(order)
        s.commit()
        s.refresh(order)
        return order


def list_orders(status: Optional[str] = None) -> list[dict]:
    with Session(engine) as s:
        q = select(Order).order_by(Order.created_at.desc())
        if status:
            q = q.where(Order.status == status)
        return [_order_dict(o) for o in s.exec(q).all()]


def get_order(order_id: int) -> Optional[dict]:
    with Session(engine) as s:
        o = s.get(Order, order_id)
        return _order_dict(o) if o else None


def update_order(order_id: int, **fields) -> Optional[dict]:
    with Session(engine) as s:
        o = s.get(Order, order_id)
        if not o:
            return None
        for k, v in fields.items():
            if hasattr(o, k):
                setattr(o, k, v)
        o.updated_at = datetime.utcnow()
        s.add(o)
        s.commit()
        s.refresh(o)
        return _order_dict(o)


def order_add_tag(order_id: int, tag: str) -> Optional[dict]:
    """新增 tag（不重複）。"""
    with Session(engine) as s:
        o = s.get(Order, order_id)
        if not o:
            return None
        tags = json.loads(o.tags_json or "[]")
        if tag and tag not in tags:
            tags.append(tag)
            o.tags_json = json.dumps(tags, ensure_ascii=False)
            o.updated_at = datetime.utcnow()
            s.add(o)
            s.commit()
            s.refresh(o)
        return _order_dict(o)


def order_remove_tag(order_id: int, tag: str) -> Optional[dict]:
    """移除 tag。"""
    with Session(engine) as s:
        o = s.get(Order, order_id)
        if not o:
            return None
        tags = [t for t in json.loads(o.tags_json or "[]") if t != tag]
        o.tags_json = json.dumps(tags, ensure_ascii=False)
        o.updated_at = datetime.utcnow()
        s.add(o)
        s.commit()
        s.refresh(o)
        return _order_dict(o)


def _order_dict(o: "Order") -> dict:
    return {
        "id": o.id,
        "order_no": o.order_no,
        "conversation_id": o.conversation_id,
        "contact_name": o.contact_name,
        "contact_phone": o.contact_phone,
        "pickup_type": o.pickup_type,
        "delivery_address": o.delivery_address,
        "items": json.loads(o.items_json or "[]"),
        "total": o.total,
        "status": o.status,
        "payment_status": o.payment_status,
        "notes": o.notes,
        "tags": json.loads(o.tags_json or "[]"),
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }
