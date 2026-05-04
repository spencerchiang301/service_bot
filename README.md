# Service Bot — AI Customer Service Platform  
AI 客服系統（RAG + 多通道 + 即時互動）

---

## 🧠 Overview | 簡介

Service Bot is a production-style AI customer service platform that integrates LLM capabilities into real-world business workflows.

It supports multi-channel communication (Telegram, LINE, Web), combines RAG (Retrieval-Augmented Generation) with a deterministic order state machine, and enables seamless human-in-the-loop operations.

Service Bot 是一套可實際應用的 AI 客服系統，整合 LLM 能力到真實業務流程中。  
系統支援 Telegram、LINE、Web 三大通訊平台，結合 RAG（檢索增強生成）與訂單狀態機（state machine），並提供真人客服接管機制。

---

## 🏗️ System Architecture | 系統架構
```
User (Web / Telegram / LINE)
        ↓
API Layer (FastAPI)
        ↓
Application Layer
    ├── Order State Machine
    └── Conversation Orchestrator
        ↓
AI Layer
    ├── RAG Pipeline (Qdrant)
    └── LLM (GPT-4o)
        ↓
Data Layer
    ├── SQLite
    └── Qdrant Vector DB
        ↓
Realtime Layer (WebSocket)
```
---

## 🧩 Architecture Design | 架構設計

The system is designed with clear separation of concerns:

系統採用分層架構設計，確保可維護性與可擴展性：

1. Interaction Layer（互動層）
- Telegram / LINE / Web Chat
- Multi-channel message handling

2. Application Layer（應用層）
- Order workflow (state machine)
- Conversation routing
- Human agent takeover

3. AI Layer（AI 層）
- RAG 知識檢索
- LLM 推理與生成

4. Data Layer（資料層）
- SQLite（交易資料）
- Qdrant（向量資料）

---

flowchart TD
```
%% User Layer
U[Users<br/>Web / Telegram / LINE]

%% Entry Layer
U --> API[FastAPI API Layer]

%% Application Layer
API --> ORCH[Conversation Orchestrator]
ORCH --> ORDER[Order State Machine]
ORCH --> ROUTER[Intent Router]

%% AI Layer
ROUTER -->|Knowledge Query| RAG[RAG Pipeline]
ROUTER -->|General Chat| LLM[LLM Service]

RAG --> VDB[(Qdrant Vector DB)]
RAG --> EMB[Embedding Model]

LLM --> OPENAI[OpenAI GPT-4o]

%% Data Layer
ORDER --> DB[(SQLite / PostgreSQL)]
API --> DB

%% Realtime Layer
API --> WS[WebSocket Manager]
WS --> DASH[Agent Dashboard]

%% Human-in-the-loop
DASH --> HUMAN[Human Agent]
HUMAN --> API

%% Admin / Ingestion
ADMIN[Admin Panel] --> INGEST[Ingestion Pipeline]
INGEST --> VDB

%% Styling
classDef layer fill:#0f172a,color:#fff,stroke:#38bdf8;
class API,ORCH,ORDER,ROUTER,RAG,LLM,WS layer;
```
---

## ⚙️ Key Features | 核心功能

Multi-channel Communication（多平台整合）
- Telegram / LINE / Web unified system
- 跨平台對話一致性

RAG Knowledge System（知識庫問答）
- 支援 Excel / TXT / Markdown / PDF
- 自動 embedding + 向量搜尋
- GPT-4o context-aware 回答

Order State Machine（訂單流程）
- LLM 判斷意圖 → 啟動流程
- 收集訂單資訊（商品 / 配送 / 聯絡）

Human-in-the-loop（真人客服）
- Dashboard 即時接管
- Bot ↔ Human 無縫切換

Real-time Dashboard（即時儀表板）
- WebSocket 推播
- 即時訊息與訂單更新

Admin System（管理後台）
- 知識庫 CRUD
- 批次匯入
- 向量同步

---

## 🏗️ Architecture Decisions | 設計決策

Why RAG instead of fine-tuning?
- Keeps knowledge up-to-date
- Reduces hallucination
- Lower operational cost

👉 使用 RAG 避免模型幻覺並保持資料即時更新

Why Qdrant?
- High-performance vector search
- Simple deployment
- Real-time friendly

👉 適合即時應用場景的向量資料庫

Why SQLite?
- Lightweight and easy to deploy
- Suitable for prototyping

👉 可替換為 PostgreSQL 以支援 production

Why State Machine + LLM?
- State machine ensures deterministic workflow
- LLM provides flexible intent detection

👉 結合穩定性與智能

Why WebSocket?
- Enables real-time communication
- Required for dashboard and live chat

---

## 🚀 Scaling Strategy | 擴展策略

To scale into production:

- Replace SQLite with PostgreSQL
- Deploy on Kubernetes (GKE / EKS)
- Separate RAG into independent service
- Introduce message queue (Kafka / Redis)
- Add caching layer (Redis)

系統可透過以下方式升級為 production：

- 資料庫升級（PostgreSQL）
- 容器化與 Kubernetes 部署
- 拆分 AI service
- 加入訊息佇列與快取機制

---

## 🔮 Future Improvements | 未來優化

- Multi-agent architecture
- Tool calling（外部 API / DB）
- Multi-tenant SaaS
- Observability（監控 / tracing）

---

## 🧠 What This Project Demonstrates | 技術展示

- AI system architecture（RAG + LLM integration）
- Real-time system design（WebSocket）
- Hybrid workflow（LLM + state machine）
- Multi-channel system design

👉 展示從 AI → Backend → System Design 的完整能力

---

## 🧰 Tech Stack | 技術棧

Backend: FastAPI  
Database: SQLite  
Vector DB: Qdrant  
AI: OpenAI GPT-4o  
Realtime: WebSocket  
Messaging: Telegram / LINE  

---

## ⚙️ Setup | 安裝

git clone https://github.com/spencerchiang301/service_bot
cd service_bot  
pip install -r requirements.txt  
cp .env.example .env  

.env 設定：

OPENAI_API_KEY=sk-...  
QDRANT_HOST=localhost  
QDRANT_PORT=6333  

啟動服務：

docker-compose up -d  
uvicorn main:app --host 0.0.0.0 --port 8000  

---

## 🌐 Interfaces | 介面

Web Chat: http://localhost:8000  
Dashboard: http://localhost:8000/dashboard  
Admin: http://localhost:8000/admin  

---

## 📜 License

MIT License
