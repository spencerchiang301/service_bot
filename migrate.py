"""一次性 migration：替現有 message 表加入 message_type / media_url 欄位"""
import sqlite3

conn = sqlite3.connect("service_bot.db")
cur = conn.cursor()

existing = [row[1] for row in cur.execute("PRAGMA table_info(message)")]
if "message_type" not in existing:
    cur.execute("ALTER TABLE message ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'")
    print("✅ 加入 message_type")
else:
    print("⏭️  message_type 已存在")

if "media_url" not in existing:
    cur.execute("ALTER TABLE message ADD COLUMN media_url TEXT")
    print("✅ 加入 media_url")
else:
    print("⏭️  media_url 已存在")

existing_ki = [row[1] for row in cur.execute("PRAGMA table_info(knowledgeitem)")]
if "source" not in existing_ki:
    cur.execute("ALTER TABLE knowledgeitem ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    print("✅ 加入 knowledgeitem.source")
else:
    print("⏭️  knowledgeitem.source 已存在")

existing_conv = [row[1] for row in cur.execute("PRAGMA table_info(conversation)")]
if "tags_json" not in existing_conv:
    cur.execute("ALTER TABLE conversation ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
    print("✅ 加入 conversation.tags_json")
else:
    print("⏭️  conversation.tags_json 已存在")

existing_order = [row[1] for row in cur.execute("PRAGMA table_info('order')")]
if existing_order:
    if "tags_json" not in existing_order:
        cur.execute("ALTER TABLE 'order' ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
        print("✅ 加入 order.tags_json")
    else:
        print("⏭️  order.tags_json 已存在")

conn.commit()
conn.close()
print("完成")
