import asyncio
import os
import secrets
from contextlib import asynccontextmanager

import dotenv
import uvicorn
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from itsdangerous import URLSafeTimedSerializer

dotenv.load_dotenv()

DEBUG = False
DEVELOPMENT = os.environ.get("DEVELOPMENT", "0") == "1"

SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
serializer = URLSafeTimedSerializer(SECRET_KEY)

ph = PasswordHasher()


def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


class ConnectionManager:
    def __init__(self):
        self.slaves: dict[str, WebSocket] = {}
        self.master: WebSocket | None = None
        self.master_connected_slave_id: str | None = None

    def generate_slave_id(self) -> str:
        while True:
            slave_id = secrets.token_hex(8)
            if slave_id not in self.slaves:
                return slave_id

    async def connect_slave(self, websocket: WebSocket) -> str:
        await websocket.accept()
        slave_id = self.generate_slave_id()
        self.slaves[slave_id] = websocket
        return slave_id

    async def disconnect_slave(self, slave_id: str):
        self.slaves.pop(slave_id, None)
        if self.master_connected_slave_id == slave_id:
            if self.master:
                await self.master.close()
            self.master = None
            self.master_connected_slave_id = None

    async def connect_master(self, websocket: WebSocket, slave_id: str) -> bool:
        if slave_id not in self.slaves:
            return False
        await websocket.accept()
        self.master = websocket
        self.master_connected_slave_id = slave_id
        slave_ws = self.slaves[slave_id]
        await slave_ws.send_text("CONNECTED")
        return True

    async def disconnect_master(self, websocket: WebSocket):
        if self.master is websocket:
            slave_id = self.master_connected_slave_id
            self.master = None
            self.master_connected_slave_id = None
            if slave_id and slave_id in self.slaves:
                await self.slaves[slave_id].send_text("DISCONNECTED")

    def get_slaves(self):
        return list(self.slaves.keys())


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(heartbeat_task())
    yield


app = FastAPI(lifespan=lifespan)


async def heartbeat_task():
    while True:
        await asyncio.sleep(30)
        disconnected = []
        for slave_id, ws in manager.slaves.items():
            try:
                await ws.send_text("HEARTBEAT")
            except Exception:
                disconnected.append(slave_id)
        for slave_id in disconnected:
            await manager.disconnect_slave(slave_id)


@app.websocket("/ws/slave")
async def slave_endpoint(websocket: WebSocket):
    slave_id = await manager.connect_slave(websocket)
    await websocket.send_text(slave_id)
    try:
        while True:
            data = await websocket.receive_bytes()
            if manager.master and manager.master_connected_slave_id == slave_id:
                debug_print(f"slave -> master: {data}")
                await manager.master.send_bytes(data)
    except WebSocketDisconnect:
        await manager.disconnect_slave(slave_id)


@app.websocket("/ws/master")
async def master_endpoint(
    websocket: WebSocket, slave_id: str | None = None, token: str | None = None
):
    if not slave_id:
        await websocket.close(code=4000)
        return

    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        return

    try:
        serializer.loads(token, max_age=3600)
    except Exception:
        await websocket.close(code=4001)
        return

    success = await manager.connect_master(websocket, slave_id)
    if not success:
        await websocket.close(code=4002)
        return

    try:
        while True:
            data = await websocket.receive_bytes()
            if slave_id in manager.slaves:
                debug_print(f"master -> slave: {data}")
                await manager.slaves[slave_id].send_bytes(data)
    except WebSocketDisconnect:
        await manager.disconnect_master(websocket)


def get_session(request: Request) -> bool:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        serializer.loads(token, max_age=3600)
        return True
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")


protected = APIRouter(dependencies=[Depends(get_session)])


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    token = body.get("token")

    hashed_token = os.environ.get("AUTH_TOKEN")
    if not hashed_token:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    try:
        ph.verify(hashed_token, token)
    except VerifyMismatchError:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    session_token = serializer.dumps("1")
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        "session",
        session_token,
        httponly=True,
        secure=not DEVELOPMENT,
        samesite="strict",
        max_age=3600,
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("session")
    return response


@protected.get("/api/slaves")
async def list_slaves():
    return JSONResponse({"slaves": manager.get_slaves()})


@protected.post("/api/slaves/{slave_id}/kick")
async def kick_slave(slave_id: str):
    if slave_id not in manager.slaves:
        raise HTTPException(status_code=404, detail="Slave not found")
    ws = manager.slaves[slave_id]
    await ws.close()
    await manager.disconnect_slave(slave_id)
    return JSONResponse({"status": "kicked"})


app.include_router(protected)


@app.get("/api/health")
async def health():
    return JSONResponse({"status": "healthy"})


LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>WebSocket Relay - Login</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 400px; margin: 100px auto; padding: 20px; text-align: center; }
        input { padding: 10px; width: 100%; margin-bottom: 10px; box-sizing: border-box; }
        button { padding: 10px 20px; background: #007bff; color: white; border: none; cursor: pointer; }
        button:hover { background: #0056b3; }
        .error { color: red; margin-bottom: 10px; }
    </style>
</head>
<body>
    <h1>WebSocket Relay Admin</h1>
    <p>Enter your authentication token:</p>
    <input type="password" id="token" placeholder="Auth Token">
    <button onclick="login()">Login</button>
    <p id="error" class="error"></p>
    <script>
        async function checkAuth() {
            try {
                const resp = await fetch('/api/slaves');
                if (resp.ok) {
                    window.location.href = '/dashboard';
                }
            } catch (e) {}
        }
        checkAuth();

        async function login() {
            const authToken = document.getElementById('token').value;
            try {
                const resp = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: authToken })
                });
                if (resp.ok) {
                    window.location.href = '/dashboard';
                } else {
                    document.getElementById('error').textContent = 'Invalid token';
                }
            } catch (e) {
                document.getElementById('error').textContent = 'Connection error';
            }
        }
    </script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>WebSocket Relay - Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
        input { padding: 10px; width: 100%; margin-bottom: 10px; box-sizing: border-box; }
        button { padding: 10px 20px; background: #007bff; color: white; border: none; cursor: pointer; margin-right: 5px; }
        button:hover { background: #0056b3; }
        .logout { background: #6c757d; }
        .logout:hover { background: #545b62; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { padding: 10px; border: 1px solid #ddd; text-align: left; }
        th { background: #f5f5f5; }
        .kick { background: #dc3545; }
        .kick:hover { background: #c82333; }
    </style>
</head>
<body>
    <h1>WebSocket Relay Dashboard</h1>
    <div>
        <button onclick="loadSlaves()">Refresh</button>
        <button class="logout" onclick="logout()">Logout</button>
    </div>
    <h2>Connected Slaves</h2>
    <table id="slaves">
        <thead><tr><th>Slave ID</th><th>Action</th></tr></thead>
        <tbody></tbody>
    </table>
    <script>
        async function loadSlaves() {
            try {
                const resp = await fetch('/api/slaves');
                if (resp.status === 401) {
                    window.location.href = '/';
                    return;
                }
                const data = await resp.json();
                const tbody = document.querySelector('#slaves tbody');
                tbody.innerHTML = '';
                for (const slave of data.slaves) {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `<td>${slave}</td><td><button class="kick" onclick="kick('${slave}')">Kick</button></td>`;
                    tbody.appendChild(tr);
                }
            } catch (e) {
                console.error(e);
            }
        }

        async function kick(slaveId) {
            if (!confirm('Kick slave ' + slaveId + '?')) return;
            await fetch('/api/slaves/' + slaveId + '/kick', { method: 'POST' });
            loadSlaves();
        }

        async function logout() {
            await fetch('/api/logout', { method: 'POST' });
            window.location.href = '/';
        }

        loadSlaves();
    </script>
</body>
</html>"""


@app.get("/")
async def index():
    return HTMLResponse(LOGIN_HTML)


@app.get("/dashboard")
async def dashboard(request: Request):
    get_session(request)
    return HTMLResponse(DASHBOARD_HTML)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
