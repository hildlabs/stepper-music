"""Stepper Dashboard (Pi-side).

Hosts UI + MIDI library + conversion 24/7. For hardware-touching actions
(flash / stop / motor-test) it proxies to the PC bridge via HTTP. UI keeps
working when the PC is off; only flashing reports "bridge unreachable".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent  # dashboard/
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for sibling sync.py

from src.converter import convert_midi, ConvertOptions  # noqa: E402
from sync import SyncEngine  # noqa: E402
from effect_engine import EffectEngine  # noqa: E402
from effects_lib import list_effects  # noqa: E402

WEB_DIR = ROOT / "web"
LIBRARY_DIR = ROOT / "midi_library"
OUTPUT_DIR = ROOT / "out"
LIBRARY_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

BRIDGE_URL = os.environ.get("STEPPER_BRIDGE_URL", "http://127.0.0.1:8766")
LED_BRIDGE_URL = os.environ.get("LED_BRIDGE_URL", "http://127.0.0.1:8765")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stepper-dashboard")

app = FastAPI(title="Stepper Lab")
client = httpx.AsyncClient(base_url=BRIDGE_URL, timeout=300.0)
led_client = httpx.AsyncClient(base_url=LED_BRIDGE_URL, timeout=2.0)
sync_engine = SyncEngine(LED_BRIDGE_URL)
effect_engine = EffectEngine(LED_BRIDGE_URL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await sync_engine.stop()
    await effect_engine.stop()

_NOTE_RE = re.compile(r"\{\s*([0-9.]+)f?\s*,\s*([0-9]+)\s*\}")


# ── helpers ──────────────────────────────────────────────────────────────
def _meta_path(stem: str) -> Path: return LIBRARY_DIR / f"{stem}.json"
def _mid_path(stem: str) -> Path: return LIBRARY_DIR / f"{stem}.mid"
def _header_path(stem: str) -> Path: return OUTPUT_DIR / f"{stem}.h"


def parse_header_notes(header_path: Path) -> dict:
    text = header_path.read_text(encoding="utf-8")
    voices: dict[str, list[dict]] = {f"voice{i}": [] for i in (1, 2, 3, 4)}
    for key in voices:
        m = re.search(
            rf"const\s+NoteEvent\s+{key}\[\]\s+PROGMEM\s*=\s*\{{(.*?)\}};",
            text, re.DOTALL,
        )
        if not m:
            continue
        for f_s, d_s in _NOTE_RE.findall(m.group(1)):
            voices[key].append({"f": float(f_s), "d": int(d_s)})
    return voices


def load_library() -> list[dict]:
    entries: list[dict] = []
    for meta in LIBRARY_DIR.glob("*.json"):
        try:
            entries.append(json.loads(meta.read_text()))
        except Exception:
            continue
    entries.sort(key=lambda e: e.get("uploaded_at", ""), reverse=True)
    return entries


def write_meta(stem: str, data: dict) -> None:
    _meta_path(stem).write_text(json.dumps(data, indent=2))


def _unique_stem(name: str) -> str:
    base = "".join(c for c in Path(name).stem if c.isalnum() or c in ("_", "-")) or "song"
    stem, n = base, 1
    while _mid_path(stem).exists() or _meta_path(stem).exists():
        n += 1
        stem = f"{base}_{n}"
    return stem


# ── UI ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


# ── library ──────────────────────────────────────────────────────────────
@app.get("/api/library")
def api_library() -> list[dict]:
    return load_library()


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> dict:
    name = file.filename or "song.mid"
    if not name.lower().endswith((".mid", ".midi")):
        raise HTTPException(400, "only .mid / .midi files are accepted")

    stem = _unique_stem(name)
    mid_path = _mid_path(stem)
    with open(mid_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    size = mid_path.stat().st_size

    result = convert_midi(
        str(mid_path), str(_header_path(stem)),
        options=ConvertOptions(), song_name=stem,
    )

    entry = {
        "stem": stem, "filename": name, "size_bytes": size,
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "success": result.success, "error": result.error,
        "duration_s": result.metadata.get("duration_seconds"),
        "num_tracks": result.metadata.get("num_tracks"),
        "num_raw_events": result.metadata.get("num_raw_events"),
        "channels": result.metadata.get("channels_used"),
        "bpm": result.metadata.get("bpm_used"),
        "grid_ms": result.metadata.get("grid_ms"),
        "voice1_events": result.voice1_events,
        "voice2_events": result.voice2_events,
        "voice3_events": result.voice3_events,
        "voice4_events": result.voice4_events,
        "total_events": result.total_events,
        "bytes_progmem": result.bytes_progmem,
    }
    write_meta(stem, entry)
    return entry


@app.delete("/api/songs/{stem}")
def api_delete(stem: str) -> dict:
    found = False
    for p in (_mid_path(stem), _meta_path(stem), _header_path(stem)):
        if p.exists():
            p.unlink()
            found = True
    if not found:
        raise HTTPException(404, "unknown song")
    return {"deleted": stem}


@app.get("/api/songs/{stem}/header")
def api_header(stem: str) -> FileResponse:
    p = _header_path(stem)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), filename=f"{stem}.h", media_type="text/plain")


@app.get("/api/songs/{stem}/midi")
def api_midi(stem: str) -> FileResponse:
    p = _mid_path(stem)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), filename=f"{stem}.mid", media_type="audio/midi")


@app.get("/api/songs/{stem}/notes")
def api_notes(stem: str) -> dict:
    p = _header_path(stem)
    if not p.exists():
        raise HTTPException(404)
    voices = parse_header_notes(p)
    total = lambda evs: sum(e["d"] for e in evs)
    return {
        "stem": stem,
        **voices,
        "total_ms": max(total(voices[f"voice{i}"]) for i in (1, 2, 3, 4)),
    }


# ── bridge proxy ─────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status() -> dict:
    try:
        r = await client.get("/health", timeout=2.0)
        return {"bridge": r.json(), "reachable": True}
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return {"bridge": None, "reachable": False}
    except Exception as e:
        log.warning("bridge probe failed: %s", e)
        return {"bridge": None, "reachable": False, "error": str(e)}


class FlashRequest(BaseModel):
    sync: bool = False
    sync_offset_ms: int = 0
    sync_renderer: str = "pulsewall"   # "pulsewall" | "wave" | "reactive"


async def _send_go() -> None:
    """Tell the stepper bridge to release the player from its GO-wait."""
    try:
        await client.post("/go", timeout=3.0)
    except Exception as e:
        log.warning("could not send GO to stepper bridge: %s", e)


async def _send_tick() -> None:
    """Fire the A/V sync clapper at the pre-roll midpoint (all motors)."""
    try:
        await client.post("/tick", timeout=3.0)
    except Exception as e:
        log.warning("could not send TICK to stepper bridge: %s", e)


@app.post("/api/songs/{stem}/flash")
async def api_flash(stem: str, req: FlashRequest = FlashRequest()) -> dict:
    header = _header_path(stem)
    if not header.exists():
        raise HTTPException(404, "header missing")
    header_text = header.read_text(encoding="utf-8")
    try:
        r = await client.post(
            "/flash", json={"stem": stem, "header": header_text},
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise HTTPException(503, "bridge unreachable (PC off?)")
    payload = r.json()

    if not payload.get("success"):
        if sync_engine.active:
            await sync_engine.stop()
        return JSONResponse(content=payload, status_code=r.status_code)

    # Upload succeeded — UNO is now in player setup() waiting for GO.
    if req.sync:
        # Effects are mutually exclusive with song-sync. Pause any running
        # effect so it doesn't fight the sync engine for the LED bridge.
        if effect_engine.active:
            await effect_engine.stop(restore_mode=False)
        voices = parse_header_notes(header)
        await sync_engine.start(
            voices, stem=stem, offset_ms=req.sync_offset_ms,
            renderer=req.sync_renderer,
        )
        await sync_engine.warm_up()
        sync_engine.trigger_go()
        # Hold back the player (motors) so the LED pre-roll can colour-tune
        # the wall before the first note sounds. At the pre-roll midpoint,
        # fire the sync clapper: all motors do one short burst, captured in
        # both the audio and video takes for perfect alignment.
        pr = sync_engine.preroll_ms
        if pr > 0:
            half = pr / 2.0
            await asyncio.sleep(half / 1000.0)
            await _send_tick()
            await asyncio.sleep((pr - half) / 1000.0)
        await _send_go()
    else:
        if sync_engine.active:
            await sync_engine.stop()
        await _send_go()

    return JSONResponse(content=payload, status_code=r.status_code)


# ── effect endpoints ─────────────────────────────────────────────────────
@app.get("/api/effects/list")
def api_effects_list() -> list[dict]:
    return list_effects()


@app.get("/api/effects/status")
def api_effects_status() -> dict:
    return {"active": effect_engine.active, "name": effect_engine.current_effect}


class EffectRequest(BaseModel):
    name: str


@app.post("/api/effects/start")
async def api_effects_start(req: EffectRequest) -> dict:
    # Sync owns the bridge during a song; refuse to start an effect on top.
    if sync_engine.active:
        raise HTTPException(409, "sync engine is active — cannot start effect")
    try:
        await effect_engine.start(req.name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"active": True, "name": req.name}


@app.post("/api/effects/stop")
async def api_effects_stop() -> dict:
    await effect_engine.stop()
    return {"active": False, "name": None}


@app.post("/api/stop")
async def api_stop() -> dict:
    await sync_engine.stop()
    try:
        r = await client.post("/stop")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise HTTPException(503, "bridge unreachable")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@app.get("/api/sync/status")
async def api_sync_status() -> dict:
    return {"active": sync_engine.active, "song": sync_engine.current_song}


@app.get("/api/sync/renderers")
def api_sync_renderers() -> list[dict]:
    return [
        {"name": "wave",     "label": "Wave"},
        {"name": "reactive", "label": "Reactive Orchestra"},
    ]


@app.get("/api/led-bridge/status")
async def api_led_bridge_status() -> dict:
    try:
        r = await led_client.get("/health", timeout=1.0)
        body = r.json()
        return {
            "reachable": True,
            "connected": bool(body.get("connected")),
            "port": body.get("port"),
        }
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return {"reachable": False, "connected": False, "port": None}
    except Exception as e:
        log.warning("led bridge probe failed: %s", e)
        return {"reachable": False, "connected": False, "port": None}


@app.post("/api/motor-test")
async def api_motor_test() -> dict:
    try:
        r = await client.post("/motor-test")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise HTTPException(503, "bridge unreachable")
    return JSONResponse(content=r.json(), status_code=r.status_code)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("STEPPER_DASHBOARD_PORT", "8788"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
