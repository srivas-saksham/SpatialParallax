#!/usr/bin/env python3
"""
all_in_one.py

One-process WebXR pipeline:
  • Serves /pose (WebXR sender) and /viewer (3D viewer) over HTTP/HTTPS
  • WebSocket endpoint at /ws for bidirectional pose updates
  • Pretty-prints structured JSON, computes velocities between frames
  • Designed for a SINGLE Cloudflare tunnel or local HTTPS

USAGE
=====
1) Install deps:
   pip install aiohttp

2) Start server (HTTP):
   python all_in_one.py --host 0.0.0.0 --port 8765

   (Optional) Local HTTPS with your own cert:
   python all_in_one.py --host 0.0.0.0 --port 8765 \
     --certfile /path/to/cert.pem --keyfile /path/to/key.pem

3) (Optional) Expose ONE public URL with Cloudflare:
   cloudflared tunnel --url http://localhost:8765

4) Open:
   - Phone (AR):   https://<your-tunnel>/pose
   - Desktop:      https://<your-tunnel>/viewer

NOTES
=====
• The pages auto-derive the correct WS URL:
    wss://<host>/ws  when served over https:
    ws://<host>/ws   when served over http:
• No hard-coded URLs required.
• Works on localhost (WebXR requires HTTPS or localhost).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import ssl
import sys
import time
from collections import defaultdict
from typing import Dict, Optional

from aiohttp import web, WSMsgType

# -----------------------------
# Config (defaults; can override via CLI)
# -----------------------------
DEFAULT_HOST = os.environ.get("HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PORT", 8765))

# -----------------------------
# In-memory state
# -----------------------------
# Track all connected WS clients
WSClients = set()

# Last known (per-connection) pose for velocity estimation
# Keyed by the aiohttp WebSocketResponse object
client_last: Dict[web.WebSocketResponse, Optional[dict]] = defaultdict(lambda: None)


# -----------------------------
# Utilities
# -----------------------------
def now_s() -> float:
    """Monotonic-ish wall time (seconds)."""
    return time.time()


def pretty(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


async def _safe_send(ws: web.WebSocketResponse, data: dict) -> None:
    """Send a JSON object safely to a single client."""
    if ws.closed:
        return
    try:
        await ws.send_str(pretty(data))
    except Exception as e:
        peer = ws.headers.get("X-Forwarded-For") or str(ws._req.remote) if hasattr(ws, "_req") else "unknown"
        print(f"[{peer}] ❗ send error: {e}")


async def broadcast(update: dict) -> None:
    """Broadcast pretty JSON to all open clients."""
    # Log once to terminal (pretty)
    print(pretty(update))
    if not WSClients:
        return
    await asyncio.gather(*(_safe_send(ws, update) for ws in list(WSClients) if not ws.closed))


def compute_velocity(last: dict, px: float, py: float, pz: float, ts_s: float, fallback_now: float) -> Optional[dict]:
    """
    Compute velocity from last pose. Use client timestamp if monotonic, else fallback to server delta.
    last = {"pos": {"x":..., "y":..., "z":...}, "ts": <seconds>}
    """
    if not last or not isinstance(last.get("pos"), dict):
        return None
    last_ts = last.get("ts")
    if not isinstance(last_ts, (int, float)):
        return None

    # Prefer client dt if non-negative; otherwise use server delta to avoid negative time differences
    dt = ts_s - last_ts if ts_s >= last_ts else (fallback_now - last_ts)
    if dt <= 0:
        return None

    dx = px - last["pos"]["x"]
    dy = py - last["pos"]["y"]
    dz = pz - last["pos"]["z"]
    speed = math.sqrt(dx * dx + dy * dy + dz * dz) / dt
    return {"vx": dx / dt, "vy": dy / dt, "vz": dz / dt, "speed_m_s": speed, "dt": dt}


# -----------------------------
# HTTP Handlers (HTML pages & misc)
# -----------------------------
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>WebXR Pose Server</title>
  <style>
    body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:0;padding:40px;background:#0b1220;color:#e9eef7}
    a {color:#7fb3ff;text-decoration:none}
    .card{max-width:840px;margin:auto;background:#121a2a;border:1px solid #1e2a44;border-radius:16px;padding:24px}
    h1{margin:0 0 12px;font-size:28px}
    code{background:#0b1220;border:1px solid #1e2a44;border-radius:8px;padding:2px 6px}
    .row{display:flex;gap:16px;flex-wrap:wrap;margin-top:16px}
    .pill{background:#0b1220;border:1px solid #1e2a44;border-radius:9999px;padding:8px 12px}
    .muted{opacity:.75}
  </style>
</head>
<body>
  <div class="card">
    <h1>WebXR Pose Server</h1>
    <p>Open <a href="/pose">/pose</a> on your phone (AR) and <a href="/viewer">/viewer</a> on your laptop/desktop.</p>
    <div class="row">
      <div class="pill"><strong>WS:</strong> <code>/ws</code></div>
      <div class="pill"><strong>Health:</strong> <code>/healthz</code></div>
      <div class="pill"><strong>Status:</strong> <code>/status</code></div>
    </div>
    <p class="muted">Pages auto-detect <code>wss://</code> vs <code>ws://</code>, so no URL pasting is needed.</p>
  </div>
</body>
</html>
"""

POSE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>WebXR Head Tracking</title>
  <style>
    body { 
      margin: 0; 
      padding: 0;
      overflow: hidden; 
      font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif; 
      background: #1a1a1a; 
      color: white;
      display: flex;
      flex-direction: column;
      height: 100vh;
    }
    
    .container {
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 20px;
      text-align: center;
    }
    
    .info {
      margin-bottom: 40px;
      max-width: 300px;
    }
    
    .info h1 {
      font-size: 24px;
      font-weight: 600;
      margin: 0 0 16px 0;
    }
    
    .info p {
      font-size: 16px;
      line-height: 1.5;
      opacity: 0.8;
      margin: 0;
    }
    
    #startButton {
      padding: 16px 32px;
      font-size: 18px;
      font-weight: 600;
      border-radius: 8px;
      border: none;
      background: #333;
      color: white;
      cursor: pointer;
      transition: background 0.2s ease;
      min-width: 200px;
    }
    
    #startButton:hover {
      background: #444;
    }
    
    #startButton:active {
      background: #555;
    }
    
    .status {
      position: absolute;
      top: 20px;
      right: 20px;
      font-size: 14px;
      padding: 8px 12px;
      border-radius: 6px;
      background: rgba(0, 0, 0, 0.5);
    }
    
    .status-connected { color: #4ade80; }
    .status-disconnected { color: #f87171; }
    
    canvas { 
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      z-index: -1;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="info">
      <h1>WebXR Head Tracking</h1>
      <p>Move your phone to control the camera view on /viewer</p>
    </div>
    <button id="startButton">🚀 Start Tracking</button>
  </div>
  
  <div class="status">
    <span id="statusText">Disconnected</span>
  </div>

  <script type="module">
  import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.162.0/build/three.module.js';
  import { ARButton } from 'https://cdn.jsdelivr.net/npm/three@0.162.0/examples/jsm/webxr/ARButton.js';

  const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  const CLIENT_ID = Math.random().toString(36).substring(2, 9);
  const MAX_HZ = 30;

  const startButton = document.getElementById("startButton");
  const statusText = document.getElementById("statusText");
  
  // Three.js setup
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(70, window.innerWidth/window.innerHeight, 0.01, 20);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.xr.enabled = true;
  document.body.appendChild(renderer.domElement);

  // Simple cube
  const cube = new THREE.Mesh(
    new THREE.BoxGeometry(0.2, 0.2, 0.2),
    new THREE.MeshBasicMaterial({ color: 0x666666 })
  );
  cube.position.set(0, 0, -1);
  scene.add(cube);

  // WebSocket state
  let socket = null;
  let socketOpen = false;
  let refSpace = null;
  let lastSend = 0;
  const minInterval = 1.0 / MAX_HZ;

  function openSocket() {
    try {
      socket = new WebSocket(WS_URL);
      socket.onopen = () => {
        socketOpen = true;
        statusText.textContent = "Connected";
        statusText.className = "status-connected";
      };
      socket.onclose = () => {
        socketOpen = false;
        statusText.textContent = "Disconnected";
        statusText.className = "status-disconnected";
        setTimeout(openSocket, 1500);
      };
      socket.onerror = () => {
        statusText.textContent = "Disconnected";
        statusText.className = "status-disconnected";
      };
    } catch (e) {
      setTimeout(openSocket, 2000);
    }
  }

  function sendPose(xrFrame) {
    if (!socketOpen || !refSpace) return;
    const now = performance.now() / 1000;
    if (now - lastSend < minInterval) return;

    const viewerPose = xrFrame.getViewerPose(refSpace);
    if (!viewerPose) return;
    const t = viewerPose.transform;

    const msg = {
      clientId: CLIENT_ID,
      ts: Date.now(),
      position: { x: t.position.x, y: t.position.y, z: t.position.z + 1.0},
      rotation: { x: t.orientation.x, y: t.orientation.y, z: t.orientation.z, w: t.orientation.w },
    };
    
    try { 
      socket.send(JSON.stringify(msg)); 
      lastSend = now; 
    } catch (e) {
      console.warn("Failed to send pose:", e);
    }
  }

  function onXRFrame(t, xrFrame) {
    cube.rotation.y += 0.01;
    renderer.render(scene, camera);
    sendPose(xrFrame);
  }

  renderer.xr.addEventListener("sessionstart", async () => {
    const session = renderer.xr.getSession();
    try {
      refSpace = await session.requestReferenceSpace("local-floor").catch(() => session.requestReferenceSpace("local"));
      openSocket();
      renderer.setAnimationLoop(onXRFrame);
    } catch (e) {
      console.error("Reference space error:", e);
    }
  });

  renderer.xr.addEventListener("sessionend", () => {
    renderer.setAnimationLoop(null);
    refSpace = null;
    if (socket) socket.close();
    statusText.textContent = "Disconnected";
    statusText.className = "status-disconnected";
  });

  startButton.addEventListener("click", async () => {
    startButton.style.display = "none";
    if (!navigator.xr) {
      alert("WebXR not supported in this browser.");
      return;
    }
    try {
      const supported = await navigator.xr.isSessionSupported("immersive-ar");
      if (!supported) {
        alert("AR not supported on this device.");
        return;
      }
      const arButton = ARButton.createButton(renderer, { 
        requiredFeatures: ["local-floor"],
        optionalFeatures: ["local"]
      });
      arButton.style.cssText = `
        position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
        padding: 12px 24px; font-size: 16px; font-weight: 600;
        border-radius: 8px; border: none; 
        background: #333; color: white; cursor: pointer; z-index: 10;
      `;
      document.body.appendChild(arButton);
    } catch (e) {
      alert("Error checking AR support: " + e.message);
    }
  });

  window.addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });
  </script>
</body>
</html>
"""

VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>AR Camera Viewer</title>
  <style>
    body { 
      margin: 0; 
      overflow: hidden; 
      background: #101418; 
      font-family: ui-monospace, monospace;
    }
    
    #info {
      position: absolute;
      top: 12px;
      left: 12px;
      background: rgba(0, 0, 0, 0.8);
      color: #fff;
      padding: 8px 12px;
      font-size: 12px;
      border-radius: 4px;
      z-index: 5;
      border: 1px solid rgba(255,255,255,0.1);
    }
    
    .status-connected { color: #4ade80; }
    .status-disconnected { color: #f87171; }
  </style>
</head>
<body>
  <div id="info">
    <div>Pos: <span id="pos">0.00, 0.00, 0.00</span></div>
    <div>Status: <span id="status" class="status-disconnected">Disconnected</span></div>
  </div>

  <script type="module">
  import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.162.0/build/three.module.js';

  const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  
  const posDisplay = document.getElementById("pos");
  const statusDisplay = document.getElementById("status");

  // Scene setup
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x101418);

  const grid = new THREE.GridHelper(10, 20);
  scene.add(grid);

  // Buildings for depth perception
  const materials = [
    new THREE.MeshLambertMaterial({ color: 0xf0f0f0 }),
    new THREE.MeshLambertMaterial({ color: 0xfff8dc }),
    new THREE.MeshLambertMaterial({ color: 0xf5f5dc }),
    new THREE.MeshLambertMaterial({ color: 0xe6e6fa }),
    new THREE.MeshLambertMaterial({ color: 0xf0fff0 }),
    new THREE.MeshLambertMaterial({ color: 0xfff0f5 }),
    new THREE.MeshLambertMaterial({ color: 0xf8f8ff }),
    new THREE.MeshLambertMaterial({ color: 0xfffacd })
  ];

  const buildingConfigs = [
    [1.5, 1, 0.4, 0.8, 0.4], [-1.2, 0.8, 0.3, 1.2, 0.3], [2.8, 0.5, 0.5, 0.6, 0.5],
    [0.2, -1.5, 0.6, 1.5, 0.6], [-2.5, -1.8, 0.4, 2.0, 0.4], [3.2, -2.0, 0.3, 1.8, 0.3],
    [-0.8, -2.5, 0.5, 1.0, 0.5], [1.8, -3.5, 0.7, 2.2, 0.7], [-1.5, -4.0, 0.4, 1.6, 0.4],
    [0.5, -4.2, 0.6, 2.5, 0.6], [-3.0, -3.8, 0.3, 1.4, 0.3], [2.2, -4.5, 0.5, 1.9, 0.5],
    [4.0, -1.0, 0.4, 1.3, 0.4], [-4.2, -0.5, 0.3, 1.7, 0.3], [3.8, -3.0, 0.5, 2.1, 0.5],
    [-3.8, -2.8, 0.4, 1.1, 0.4], [0, -0.5, 0.8, 3.0, 0.8]
  ];

  buildingConfigs.forEach((cfg, i) => {
    const [x, z, w, h, d] = cfg;
    const m = materials[i % materials.length];
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m);
    mesh.position.set(x, h/2, z);
    scene.add(mesh);
  });

  // Reference cube at origin
  const box = new THREE.Mesh(
    new THREE.BoxGeometry(0.3, 0.3, 0.3),
    new THREE.MeshNormalMaterial()
  );
  box.position.set(0, 0.15, 0);
  scene.add(box);

  scene.add(new THREE.AxesHelper(1));

  // Lighting
  const ambient = new THREE.AmbientLight(0x404040, 0.6);
  scene.add(ambient);
  const dir = new THREE.DirectionalLight(0xffffff, 0.8);
  dir.position.set(5, 10, 5);
  scene.add(dir);

  // Camera
  const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.01, 100);
  camera.lookAt(0, 0, 0);
  camera.up.set(0, 1, 0);

  // Renderer
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(window.innerWidth, window.innerHeight);
  document.body.appendChild(renderer.domElement);

  let latestPose = null;
  let frameCount = 0;
  const SCALE = 2.0;

  // WebSocket connection
  let ws = null;

  function connectWS() {
    try {
      ws = new WebSocket(WS_URL);
      ws.onopen = () => { 
        statusDisplay.textContent = "Connected";
        statusDisplay.className = "status-connected";
      };
      ws.onclose = () => {
        statusDisplay.textContent = "Disconnected";
        statusDisplay.className = "status-disconnected";
        setTimeout(connectWS, 1500);
      };
      ws.onerror = () => { 
        statusDisplay.textContent = "Disconnected";
        statusDisplay.className = "status-disconnected";
      };
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.position && data.rotation) {
            latestPose = data;
          }
        } catch (e) {
          // Ignore non-JSON messages
        }
      };
    } catch (e) {
      statusDisplay.textContent = "Disconnected";
      statusDisplay.className = "status-disconnected";
      setTimeout(connectWS, 2000);
    }
  }

  connectWS();

  function animate() {
    requestAnimationFrame(animate);
    frameCount++;

    if (latestPose) {
      // Apply only position, no rotation
      camera.position.set(
        latestPose.position.x * SCALE,
        latestPose.position.y * SCALE,
        latestPose.position.z * SCALE
      );
      
      // Update position display every 30 frames to prevent lag
      if (frameCount % 30 === 0) {
        const pos = latestPose.position;
        posDisplay.textContent = 
          `${(pos.x * SCALE).toFixed(2)}, ${(pos.y * SCALE).toFixed(2)}, ${(pos.z * SCALE).toFixed(2)}`;
      }
    } else {
      camera.position.set(0, 1.5, 5);
    }

    box.rotation.y += 0.01;
    renderer.render(scene, camera);
  }

  animate();

  window.addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });
  </script>
</body>
</html>
"""

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def handle_pose(request: web.Request) -> web.Response:
    return web.Response(text=POSE_HTML, content_type="text/html")


async def handle_viewer(request: web.Request) -> web.Response:
    return web.Response(text=VIEWER_HTML, content_type="text/html")


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_status(request: web.Request) -> web.Response:
    # Count only open sockets
    open_count = sum(1 for ws in WSClients if not ws.closed)
    return web.json_response({"clients": open_count})

# -----------------------------
# WebSocket Handler (/ws)
# -----------------------------
async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """
    Single WS entry point for both AR senders and viewers.
    We just re-broadcast parsed pose updates to everyone.
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    ws._req = request  # (for logging peer)

    WSClients.add(ws)
    peer = request.headers.get("X-Forwarded-For") or request.remote or "unknown"
    print(f"➡️  WS connected: {peer} (total={len(WSClients)})")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Messages may be pretty JSON strings; parse & validate
                try:
                    data = json.loads(msg.data)
                except Exception as e:
                    print(f"[{peer}] 🔴 Invalid JSON: {e}")
                    continue

                if not isinstance(data, dict):
                    continue

                # Normalize timestamp
                raw_ts = data.get("ts")
                if isinstance(raw_ts, (int, float)):
                    # If looks like ms epoch (>= 1e12), convert to seconds
                    ts_s = raw_ts / 1000.0 if raw_ts > 1e12 else float(raw_ts)
                else:
                    ts_s = now_s()

                pos = data.get("position")
                rot = data.get("rotation")
                if not (isinstance(pos, dict) and isinstance(rot, dict)):
                    continue

                try:
                    px, py, pz = float(pos["x"]), float(pos["y"]), float(pos["z"])
                    rx, ry, rz, rw = float(rot["x"]), float(rot["y"]), float(rot["z"]), float(rot["w"])
                except Exception:
                    continue

                # Velocity
                vel_info = compute_velocity(client_last.get(ws), px, py, pz, ts_s, fallback_now=now_s())

                # Save last pose for this connection
                client_last[ws] = {"pos": {"x": px, "y": py, "z": pz}, "ts": ts_s}

                # Build update
                update = {
                    "clientId": data.get("clientId", "unknown"),
                    "ts": ts_s,
                    "position": {"x": px, "y": py, "z": pz},
                    "rotation": {"x": rx, "y": ry, "z": rz, "w": rw},
                }
                if vel_info:
                    update["velocity"] = vel_info

                # Broadcast to all clients (including sender; viewer ignores if desired)
                await broadcast(update)

            elif msg.type == WSMsgType.ERROR:
                print(f"[{peer}] WS error: {ws.exception()}")

    finally:
        # Cleanup
        client_last.pop(ws, None)
        WSClients.discard(ws)
        print(f"⬅️  WS disconnected: {peer} (total={len(WSClients)})")

    return ws


# -----------------------------
# App factory & runner
# -----------------------------
def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/pose", handle_pose)
    app.router.add_get("/viewer", handle_viewer)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/ws", handle_ws)
    return app


async def start_server(host: str, port: int, ssl_ctx: Optional[ssl.SSLContext]) -> None:
    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx)
    await site.start()

    scheme = "https" if ssl_ctx else "http"
    print(f"🚀 Serving on {scheme}://{host}:{port}")
    print(f"   • /pose   → WebXR AR sender (phone)")
    print(f"   • /viewer → 3D viewer (desktop)")
    print(f"   • /ws     → WebSocket endpoint")
    print(f"   • /status, /healthz")

    # Keep alive forever
    stop = asyncio.Future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.cancel)
        except NotImplementedError:
            # Windows might not support signal handlers in ProactorEventLoop; ignore
            pass
    try:
        await stop
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def make_ssl_context(certfile: Optional[str], keyfile: Optional[str]) -> Optional[ssl.SSLContext]:
    if not (certfile and keyfile):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    # Reasonable defaults
    ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")
    return ctx


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="All-in-one WebXR pose server (HTTP + WebSocket).")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})")
    p.add_argument("--certfile", help="Path to TLS cert (PEM). Enables HTTPS if provided with --keyfile.")
    p.add_argument("--keyfile", help="Path to TLS key (PEM). Enables HTTPS if provided with --certfile.")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    ssl_ctx = make_ssl_context(args.certfile, args.keyfile)
    try:
        asyncio.run(start_server(args.host, args.port, ssl_ctx))
        return 0
    except KeyboardInterrupt:
        print("Shutting down.")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))