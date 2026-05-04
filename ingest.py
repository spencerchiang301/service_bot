"""
使用方法：
    python ingest.py              # 匯入 data/ 資料夾內所有支援的檔案
    python ingest.py path/to/file # 匯入指定檔案
    python ingest.py --reset      # 清空知識庫後重新匯入 data/

支援格式：.xlsx  .txt  .md  .pdf
"""
import sys
import uuid
from pathlib import Path

from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from config import settings

openai_client = OpenAI(api_key=settings.openai_api_key)
qdrant = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)

SUPPORTED = {".xlsx", ".txt", ".md", ".pdf"}
VECTOR_SIZE = 1536  # text-embedding-3-small

# Excel 中哪些 sheet 名稱要匯入（忽略說明頁）
SKIP_SHEETS = {"📋 說明", "說明", "readme", "README"}


# ── Qdrant ────────────────────────────────────────────────────────────────────

def ensure_collection(reset: bool = False):
    names = [c.name for c in qdrant.get_collections().collections]
    if reset and settings.collection_name in names:
        qdrant.delete_collection(settings.collection_name)
        print(f"🗑️  已清空 collection: {settings.collection_name}")
        names = []
    if settings.collection_name not in names:
        qdrant.create_collection(
            collection_name=settings.collection_name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"✅ 建立 collection: {settings.collection_name}")


def embed_and_upsert(chunks: list[str], source: str):
    """將 chunks 批次 embed 並寫入 Qdrant。"""
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), 100):
        resp = openai_client.embeddings.create(
            input=chunks[i : i + 100], model=settings.embedding_model
        )
        vectors.extend(r.embedding for r in resp.data)

    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=vec,
            payload={"text": chunk, "source": source},
        )
        for chunk, vec in zip(chunks, vectors)
    ]
    qdrant.upsert(collection_name=settings.collection_name, points=points)
    return len(points)


# ── Excel 轉換 ────────────────────────────────────────────────────────────────

def _rows_to_dicts(ws) -> list[dict]:
    """將工作表轉為 list[dict]，第一列為欄位名稱。"""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    # 去除 emoji 前綴取得乾淨欄名，並過濾空值
    headers = [str(h).replace(" *", "").strip() if h else "" for h in rows[0]]
    result = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        d = {headers[i]: row[i] for i in range(len(headers)) if headers[i]}
        result.append(d)
    return result


def _sheet_to_chunks(ws) -> list[str]:
    """
    依照 sheet 類型將資料轉成自然語言句子，每列一個 chunk。
    這樣 Qdrant 檢索時可以精準對應到單筆商品或服務。
    """
    sheet_name = ws.title.strip()
    rows = _rows_to_dicts(ws)
    if not rows:
        return []

    chunks = []
    section = sheet_name.lstrip("🛍️🔧ℹ️📋 ").strip()  # 取得乾淨的分類名

    for row in rows:
        parts = [f"【{section}】"]

        # 商品 sheet：名稱 + 規格 + 價格 + 備註
        if "商品名稱" in row:
            name = row.get("商品名稱") or ""
            spec = row.get("規格／尺寸") or row.get("規格") or ""
            price = row.get("價格（元）") or row.get("價格") or ""
            note = row.get("備註") or ""
            if not name:
                continue
            label = f"{name}（{spec}）" if spec else name
            price_str = f"售價 {price} 元" if price != "" else "價格請洽店家"
            sentence = f"{label}：{price_str}"
            if note:
                sentence += f"。備註：{note}"
            parts.append(sentence)

        # 服務 sheet：服務名稱 + 說明 + 費用 + 備註
        elif "服務名稱" in row:
            name = row.get("服務名稱") or ""
            desc = row.get("服務說明") or ""
            fee = row.get("費用") or ""
            note = row.get("備註") or ""
            if not name:
                continue
            sentence = f"{name}：{desc}"
            if fee:
                sentence += f"（費用：{fee}）"
            if note:
                sentence += f"。{note}"
            parts.append(sentence)

        # 基本資訊 sheet：項目 + 內容
        elif "項目" in row:
            item = row.get("項目") or ""
            content = row.get("內容") or ""
            if not item or not content:
                continue
            parts.append(f"{item}：{content}")

        # 其他 sheet：直接把每欄拼接成一句話
        else:
            sentence = "　".join(
                f"{k}：{v}" for k, v in row.items() if v is not None and v != ""
            )
            if not sentence:
                continue
            parts.append(sentence)

        chunk = "\n".join(parts)
        chunks.append(chunk)

    return chunks


def read_excel(path: Path) -> list[tuple[str, list[str]]]:
    """回傳 [(sheet_label, [chunk, ...]), ...]，略過說明頁。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    results = []
    for ws in wb.worksheets:
        raw_title = ws.title.strip()
        if raw_title in SKIP_SHEETS:
            continue
        chunks = _sheet_to_chunks(ws)
        if chunks:
            results.append((raw_title, chunks))
    return results


# ── 其他格式 ──────────────────────────────────────────────────────────────────

def read_text_file(path: Path) -> str:
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8")


def chunk_text(text: str, size: int = 400, overlap: int = 60) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return [c.strip() for c in chunks if c.strip()]


# ── 主要匯入函式 ──────────────────────────────────────────────────────────────

def ingest(path: Path):
    if path.suffix == ".xlsx":
        sheets = read_excel(path)
        if not sheets:
            print(f"⚠️  {path.name}: 無可匯入的內容，跳過")
            return
        total = 0
        for sheet_title, chunks in sheets:
            n = embed_and_upsert(chunks, source=f"{path.name} > {sheet_title}")
            print(f"   📊 {sheet_title}: {n} 筆")
            total += n
        print(f"✅ {path.name} 共寫入 {total} 筆")
    else:
        text = read_text_file(path)
        chunks = chunk_text(text)
        if not chunks:
            print(f"⚠️  {path.name}: 無內容，跳過")
            return
        n = embed_and_upsert(chunks, source=path.name)
        print(f"✅ {path.name}: 寫入 {n} 筆")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    reset = "--reset" in args
    args = [a for a in args if a != "--reset"]

    ensure_collection(reset=reset)

    if args:
        targets = [Path(a) for a in args]
    else:
        targets = list(Path("data").rglob("*"))

    ingested = 0
    for p in sorted(targets):
        if p.is_file() and p.suffix in SUPPORTED:
            print(f"\n📄 處理 {p.name} ...")
            ingest(p)
            ingested += 1

    if ingested == 0:
        print("⚠️  找不到可匯入的檔案（支援 .xlsx .txt .md .pdf）")
    else:
        print(f"\n🎉 完成，共處理 {ingested} 個檔案")
