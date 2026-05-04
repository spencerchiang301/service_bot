"""
訂單收集流程：意圖偵測、品項提取、多輪狀態機
"""
import json
import re

from openai import OpenAI

import db
from config import settings

openai_client = OpenAI(api_key=settings.openai_api_key)

# ── 意圖偵測 + 品項提取（合一 LLM 呼叫）────────────────────────────────────

def detect_and_extract(message: str) -> dict:
    """
    用 LLM 一次判斷意圖並提取品項。
    回傳：
      {"intent": "inquiry"}                    — 一般問答，交 RAG 處理
      {"intent": "order", "items": [...]}      — 下單意圖，含已提取品項
      {"intent": "order_no_items"}             — 想下單但沒說要什麼
    """
    products = db.ki_list("product")
    product_list = (
        "\n".join(
            f'- {p["col1"]}（{p["col2"]}）：{p["col3"]}元' if p["col2"]
            else f'- {p["col1"]}：{p["col3"]}元'
            for p in products if p["col1"]
        )
        if products else "（尚無商品資料）"
    )

    system = f"""你是訂單意圖分析器，判斷用戶訊息是「詢問」還是「下單」。

商品列表：
{product_list}

判斷規則：
- 如果訊息包含商品名稱且帶有數量或明顯購買意圖（例：「2杯拿鐵」「來一個起司蛋糕」「給我大拿鐵」）→ order
- 如果只表達想購買但沒說具體品項（例：「我要下單」「幫我點餐」）→ order_no_items
- 其他問題、打招呼、詢問 → inquiry

回傳 JSON（只回 JSON，不加其他文字）：
- 詢問：{{"intent": "inquiry"}}
- 下單有品項：{{"intent": "order", "items": [{{"name": "商品名稱", "spec": "規格或空字串", "qty": 數量整數, "unit_price": 單價整數}}]}}
- 下單無品項：{{"intent": "order_no_items"}}

注意：商品名稱和規格請對照商品列表模糊比對，unit_price 從列表取得。"""

    resp = openai_client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    try:
        data = json.loads(resp.choices[0].message.content)
        intent = data.get("intent", "inquiry")
        if intent == "order":
            items = _normalize_items(data.get("items", []), products)
            if items:
                return {"intent": "order", "items": items}
            return {"intent": "order_no_items"}
        return {"intent": intent}
    except Exception:
        return {"intent": "inquiry"}


def _normalize_items(items: list[dict], products: list[dict]) -> list[dict]:
    """補齊價格、計算小計，過濾無效品項。"""
    result = []
    for item in items:
        item["qty"] = int(item.get("qty") or 1)
        item["unit_price"] = int(item.get("unit_price") or 0)
        if item["unit_price"] == 0:
            for p in products:
                name_match = item["name"] in p["col1"] or p["col1"] in item["name"]
                spec_match = not item.get("spec") or item["spec"] == p["col2"]
                if name_match and spec_match and p["col3"]:
                    item["unit_price"] = int(p["col3"])
                    break
        item["subtotal"] = item["unit_price"] * item["qty"]
        if item.get("name"):
            result.append(item)
    return result


# ── 步驟對應按鈕 ─────────────────────────────────────────────────────────────

STEP_BUTTONS: dict[str, list[str]] = {
    "confirm_items": ["✅ 確認", "❌ 取消"],
    "pickup_type":   ["🏪 外帶", "🛵 外送"],
    # address / contact 步驟讓使用者自由輸入，不給按鈕
}


def get_buttons(state: dict) -> list[str]:
    return STEP_BUTTONS.get(state.get("step", ""), [])


# ── 格式化 ────────────────────────────────────────────────────────────────────

def format_items(items: list[dict]) -> str:
    lines = []
    total = 0
    for it in items:
        spec = f"（{it['spec']}）" if it.get("spec") else ""
        sub = it.get("subtotal", it["unit_price"] * it["qty"])
        lines.append(f"  • {it['name']}{spec} x{it['qty']} = {sub} 元")
        total += sub
    lines.append(f"\n合計：{total} 元")
    return "\n".join(lines)


def calc_total(items: list[dict]) -> int:
    return sum(it.get("subtotal", it["unit_price"] * it["qty"]) for it in items)


# ── 狀態機 ────────────────────────────────────────────────────────────────────

def start_order(items: list[dict]) -> tuple[str, dict]:
    """建立初始狀態，回傳確認提示與狀態。"""
    state = {"step": "confirm_items", "items": items}
    summary = format_items(items)
    reply = (
        f"📋 確認您的訂單：\n\n{summary}\n\n"
        "回覆「確認」送出訂單，或「取消」重新來過"
    )
    return reply, state


def handle_step(message: str, state: dict) -> tuple[str, dict | None]:
    """
    處理當前步驟，回傳 (reply, new_state)。
    new_state 為 None 表示流程結束（完成或取消）。
    new_state 有 "__order__" key 表示需要建立訂單。
    """
    step = state["step"]
    msg = message.strip()

    # ── 隨時可取消 ────────────────────────────────────────
    if any(w in msg for w in ["取消", "算了", "不要了", "cancel"]):
        return "好的，訂單已取消。需要時隨時再告訴我！", None

    # ── confirm_items ─────────────────────────────────────
    if step == "confirm_items":
        if any(w in msg for w in ["確認", "是", "對", "好", "OK", "ok", "沒問題", "正確"]):
            new = {**state, "step": "pickup_type"}
            return (
                "請問取餐方式？\n\n"
                "🏪 **外帶** — 到店自取\n"
                "🛵 **外送** — 送到府上",
                new,
            )
        return (
            f"請確認訂單內容：\n\n{format_items(state['items'])}\n\n"
            "回覆「確認」或「取消」",
            state,
        )

    # ── pickup_type ───────────────────────────────────────
    if step == "pickup_type":
        if any(w in msg for w in ["外送", "送", "delivery"]):
            new = {**state, "step": "address", "pickup_type": "delivery"}
            return "請提供外送地址（含縣市區街道門號）：", new
        if any(w in msg for w in ["外帶", "自取", "帶走", "takeout", "帶"]):
            new = {**state, "step": "contact", "pickup_type": "takeout", "delivery_address": ""}
            return "好的，外帶！\n請提供您的姓名與聯絡電話：\n（例：王小明 0912345678）", new
        return "請回覆「外帶」或「外送」：", state

    # ── address ───────────────────────────────────────────
    if step == "address":
        if len(msg) < 6:
            return "地址太短，請提供完整地址（含縣市區街道）：", state
        new = {**state, "step": "contact", "delivery_address": msg}
        return "收到地址！\n請提供您的姓名與聯絡電話：\n（例：王小明 0912345678）", new

    # ── contact ───────────────────────────────────────────
    if step == "contact":
        # 正規化：全形數字→半形，移除分隔符和空格
        normalized = msg
        for fw, hw in zip("０１２３４５６７８９", "0123456789"):
            normalized = normalized.replace(fw, hw)
        normalized = normalized.replace("-", "").replace("－", "").replace(" ", "").replace("\u3000", "")
        # 台灣手機 09xxxxxxxx (10碼) 或 +8869xxxxxxxx
        m = re.search(r"09\d{8}", normalized) or re.search(r"(?:886)9\d{8}", normalized)
        if m:
            raw = m.group(0)
            phone = "09" + raw[-8:] if raw.startswith("886") else raw
        else:
            phone = ""
        # 姓名：去掉電話號碼部分
        name = re.sub(r"[0-9０-９\-－+\s]+", " ", msg).strip() or "客戶"

        if not phone:
            return "未偵測到手機號碼，請重新輸入：\n（例：王小明 0912-345-678）", state

        new = {
            **state,
            "step": "__done__",
            "contact_name": name,
            "contact_phone": phone,
        }
        return "__CREATE_ORDER__", new

    return "發生錯誤，訂單已取消。", None
