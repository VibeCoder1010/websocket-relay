import asyncio
import threading

import websockets


async def slave():
    uri = "ws://localhost:8000/slave"
    async with websockets.connect(uri) as websocket:
        slave_id = await websocket.recv()
        print(f"Connected with ID: {slave_id}")

        async def receive_messages():
            try:
                while True:
                    msg = await websocket.recv()
                    if isinstance(msg, str):
                        if msg == "CONNECTED":
                            print("*** Master connected ***")
                        elif msg == "DISCONNECTED":
                            print("*** Master disconnected ***")
                        elif msg == "HEARTBEAT":
                            print("*** Heartbeat ***")
                        else:
                            print(f"Received: {msg}")
                    else:
                        print(f"Received bytes: {msg}")
            except Exception:
                pass

        def input_loop():
            while True:
                msg = input("Message: ")
                asyncio.run(websocket.send(msg.encode()))

        threading.Thread(target=input_loop, daemon=True).start()
        await receive_messages()


if __name__ == "__main__":
    asyncio.run(slave())
