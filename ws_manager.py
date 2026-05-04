from fastapi import WebSocket


class DashboardManager:
    """所有客服人員的 WebSocket 連線。"""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws) if hasattr(self._connections, 'discard') else None
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, event: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


class CustomerManager:
    """網頁客戶的 WebSocket 連線，以 session_id 為 key。"""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[session_id] = ws

    def disconnect(self, session_id: str):
        self._connections.pop(session_id, None)

    async def send(self, session_id: str, event: dict) -> bool:
        ws = self._connections.get(session_id)
        if ws:
            try:
                await ws.send_json(event)
                return True
            except Exception:
                self.disconnect(session_id)
        return False

    def is_connected(self, session_id: str) -> bool:
        return session_id in self._connections


dashboard_mgr = DashboardManager()
customer_mgr = CustomerManager()
