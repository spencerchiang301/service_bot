# 共享的對話歷史，key 格式：
#   網頁:     UUID
#   Telegram: tg_{chat_id}
#   LINE:     line_{user_id}
sessions: dict[str, list[dict]] = {}

# 訂單收集狀態機，key 同上
# 結構: {"step": str, "items": [...], "pickup_type": str,
#         "delivery_address": str, "contact_name": str, "contact_phone": str}
order_states: dict[str, dict] = {}
