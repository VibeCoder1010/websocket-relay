import asyncio
import os
import threading

import dotenv
import websockets

dotenv.load_dotenv()

WS_URL = os.getenv("WS_HOST", "ws://localhost:8000")


async def master(slave_id: str):
    uri = f"{WS_URL}/ws/master?slave_id={slave_id}&token={os.environ['AUTH_TOKEN']}"
    async with websockets.connect(uri) as websocket:
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
    slave_id = input("Enter slave ID: ").strip()
    asyncio.run(master(slave_id))
