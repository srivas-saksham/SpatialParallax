#!/usr/bin/env python3
"""
server.py

WebSocket server that:
  • Receives pose data (position + rotation) from WebXR clients
  • Computes velocities between frames
  • Sends structured JSON updates to any connected viewer clients
  • Pretty-prints JSON logs in the terminal
"""

import asyncio
import json
import math
import time
from collections import defaultdict

import websockets
from websockets.protocol import State

HOST = "0.0.0.0"
PORT = 8765

# Last known state per client
client_last = defaultdict(lambda: None)

# Track all connected sockets
clients = set()


async def _safe_send(ws, data: str):
    """Safely send data to a single websocket client."""
    try:
        await ws.send(data)
    except Exception as e:
        peer = getattr(ws, "remote_address", ("?", "?"))
        peer_str = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        print(f"[{peer_str}] ❗ send error: {e}")


async def broadcast_json(update: dict):
    """Serialize update and send to all currently open clients."""
    update_str = json.dumps(update, indent=2)  # ✅ pretty-print JSON
    print(update_str)  # show JSON in terminal

    active = [c for c in clients if c.state == State.OPEN]
    if not active:
        return

    send_tasks = [asyncio.create_task(_safe_send(c, update_str)) for c in active]
    await asyncio.gather(*send_tasks)


async def handler(websocket):
    """Handle one client connection."""
    peer = websocket.remote_address
    peer_str = f"{peer[0]}:{peer[1]}" if peer else "unknown"
    print(f"➡️  Client connected: {peer_str}")

    clients.add(websocket)

    try:
        async for message in websocket:
            now = time.time()

            # Parse JSON
            try:
                data = json.loads(message)
            except Exception as e:
                print(f"[{peer_str}] 🔴 Invalid JSON: {e}")
                continue
            if not isinstance(data, dict):
                continue

            client_id = data.get("clientId", "unknown")
            ts = data.get("ts")
            if isinstance(ts, (int, float)):
                ts_s = ts / 1000.0 if ts > 1e12 else ts
            else:
                ts_s = now

            pos = data.get("position")
            rot = data.get("rotation")
            if not pos or not rot:
                continue

            try:
                px, py, pz = float(pos["x"]), float(pos["y"]), float(pos["z"])
                rx, ry, rz, rw = (
                    float(rot["x"]),
                    float(rot["y"]),
                    float(rot["z"]),
                    float(rot["w"]),
                )
            except Exception:
                continue

            # Compute velocity
            vel_info = None
            last = client_last[websocket]
            if last and isinstance(last.get("pos"), dict) and isinstance(
                last.get("ts"), (int, float)
            ):
                dt = ts_s - last["ts"] if ts_s >= last["ts"] else (now - last["ts"])
                if dt > 0:
                    dx = px - last["pos"]["x"]
                    dy = py - last["pos"]["y"]
                    dz = pz - last["pos"]["z"]
                    speed = math.sqrt(dx * dx + dy * dy + dz * dz) / dt
                    vel_info = {
                        "vx": dx / dt,
                        "vy": dy / dt,
                        "vz": dz / dt,
                        "speed_m_s": speed,
                        "dt": dt,
                    }

            # Save last pose
            client_last[websocket] = {"pos": {"x": px, "y": py, "z": pz}, "ts": ts_s}

            # Build JSON update
            update = {
                "clientId": client_id,
                "ts": ts_s,
                "position": {"x": px, "y": py, "z": pz},
                "rotation": {"x": rx, "y": ry, "z": rz, "w": rw},
            }
            if vel_info:
                update["velocity"] = vel_info

            # Broadcast JSON
            await broadcast_json(update)

    except websockets.ConnectionClosed:
        print(f"⬅️  Client disconnected: {peer_str}")
    finally:
        client_last.pop(websocket, None)
        clients.discard(websocket)


async def main():
    print(f"🚀 Starting WebSocket server on ws://{HOST}:{PORT}")
    async with websockets.serve(handler, HOST, PORT, max_size=2**20):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down.")
