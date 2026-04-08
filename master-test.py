import asyncio
import getpass
import os
import threading

import aiohttp
import dotenv
import websockets

dotenv.load_dotenv()

WS_URL = os.getenv("WS_HOST", "ws://localhost:8000")
HTTP_URL = os.getenv("HTTP_HOST", "http://localhost:8000")


async def login(token: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HTTP_URL}/api/login",
            json={"token": token},
        ) as resp:
            resp.raise_for_status()
            cookie = resp.cookies.get("session")
            if not cookie:
                raise Exception("No session cookie")
            return cookie.value


async def master(slave_id: str, session_token: str):
    # TODO: Remove token param when go server is complete.
    uri = f"{WS_URL}/ws/master?slave_id={slave_id}&token={session_token}"
    headers = {"Cookie": f"session={session_token}"}
    async with websockets.connect(uri, additional_headers=headers) as websocket:
        print(f"Connected to slave: {slave_id}")

        async def receive_messages():
            try:
                while True:
                    msg = await websocket.recv()
                    if isinstance(msg, str):
                        if msg == "CONNECTED":
                            print("*** Connected to slave ***")
                        elif msg == "DISCONNECTED":
                            print("*** Disconnected from slave ***")
                        elif msg == "HEARTBEAT":
                            print("*** Heartbeat ***")
                        else:
                            print(f"Control: {msg}")
                    else:
                        print(f"Received data: {msg}")
            except Exception:
                pass

        def input_loop():
            while True:
                msg = input("Message: ")
                asyncio.run(websocket.send(msg.encode()))

        threading.Thread(target=input_loop, daemon=True).start()
        await receive_messages()


if __name__ == "__main__":
    auth_token = getpass.getpass("Enter auth token: ")
    session_token = asyncio.run(login(auth_token))
    slave_id = input("Enter slave ID: ").strip()
    asyncio.run(master(slave_id, session_token))
