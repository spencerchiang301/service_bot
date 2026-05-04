from openai import OpenAI
from qdrant_client import QdrantClient
from config import settings

openai_client = OpenAI(api_key=settings.openai_api_key)
qdrant = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)

SYSTEM_PROMPT = """\
你是「{business_name}」的客服助理，個性親切有禮。
請根據下方【商品與服務資料】回答客戶問題。

規則：
1. 如資料中有相關資訊，直接回答（含價格、服務內容等）
2. 如資料中找不到答案，誠實說「目前沒有這項資訊，建議直接聯繫我們」
3. 不要捏造任何價格或服務內容
4. 回答簡潔、友善，使用繁體中文
5. 若回答中只列出部分商品或服務（非完整列表），務必在末尾補充一句：
   「如需查看完整列表，可以輸入『列出所有商品』或『列出所有服務』」

【商品與服務資料】
{context}
"""


def embed(text: str) -> list[float]:
    resp = openai_client.embeddings.create(input=text, model=settings.embedding_model)
    return resp.data[0].embedding


def retrieve(query: str) -> str:
    vec = embed(query)
    result = qdrant.query_points(
        collection_name=settings.collection_name,
        query=vec,
        limit=settings.top_k,
        with_payload=True,
    )
    hits = result.points
    if not hits:
        return "（知識庫目前為空，請先執行 python ingest.py 匯入資料）"
    return "\n\n---\n\n".join(h.payload["text"] for h in hits)


def chat(query: str, history: list[dict]) -> str:
    """一次性回傳完整回答，供 Telegram / LINE / WhatsApp 使用。"""
    return "".join(chat_stream(query, history))


def chat_stream(query: str, history: list[dict]):
    context = retrieve(query)
    system = SYSTEM_PROMPT.format(
        business_name=settings.business_name,
        context=context,
    )
    messages = (
        [{"role": "system", "content": system}]
        + history[-10:]
        + [{"role": "user", "content": query}]
    )
    stream = openai_client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        stream=True,
        temperature=0.3,
    )
    for chunk in stream:
        token = chunk.choices[0].delta.content
        if token:
            yield token
