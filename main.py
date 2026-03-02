import asyncio
import os
import secrets
from contextlib import asynccontextmanager

import dotenv
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer

dotenv.load_dotenv()

DEBUG = False


def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


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


def verify_auth_token(token: str = Depends(oauth2_scheme)) -> str:
    expected = os.environ.get("AUTH_TOKEN")
    if not expected:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return token


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


@app.websocket("/slave")
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


@app.websocket("/master")
async def master_endpoint(
    websocket: WebSocket, slave_id: str | None = None, token: str | None = None
):
    if not slave_id:
        await websocket.close(code=4000)
        return

    token = websocket.query_params.get("token")
    if not token or not verify_auth_token(token):
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


@app.get("/slaves")
async def list_slaves(token: str = Depends(verify_auth_token)):
    return JSONResponse({"slaves": manager.get_slaves()})


@app.get("/health")
async def health():
    return JSONResponse({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
