"""Stepper Bridge (PC-side).

Runs on the machine the Arduino UNO is USB-tethered to. Exposes a small
FastAPI surface that the Pi dashboard talks to over LAN.

Endpoints:
  GET  /health        -> {ok, port, sketches_dir, uno_open}
  POST /flash         -> body: {stem, header} -> writes player/song.h, compiles + uploads player
  POST /go            -> writes "GO\\n" over serial (release player from GO-wait)
  POST /tick          -> writes "TICK\\n" (A/V sync clapper during GO-wait)
  POST /stop          -> compiles + uploads silence sketch
  POST /motor-test    -> compiles + uploads motor_test sketch
"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Optional

import serial as pyserial
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stepper-bridge")

ARDUINO_FQBN = "arduino:avr:uno"
CH340_VENDOR = "1a86"
CH340_PRODUCT = "7523"

BRIDGE_DIR = Path(__file__).resolve().parent
SKETCHES_DIR = BRIDGE_DIR / "sketches"
PLAYER_DIR = SKETCHES_DIR / "player"
SILENCE_DIR = SKETCHES_DIR / "silence"
MOTOR_TEST_DIR = SKETCHES_DIR / "motor_test"


UNO_BAUD = 115200
UNO_BOOT_SETTLE_S = 1.5  # DTR-reset settle after pyserial open


class UnoLink:
    """Persistent pyserial connection to the Arduino UNO."""

    def __init__(self) -> None:
        self.ser: Optional[pyserial.Serial] = None
        self.port: Optional[str] = None
        self._lock = Lock()

    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def close(self) -> None:
        with self._lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self.port = None

    def open(self) -> bool:
        port = find_ch340_port()
        if not port:
            return False
        with self._lock:
            try:
                # dsrdtr=False prevents most platforms from pulsing DTR on
                # open (which would re-reset a UNO that's already booted).
                self.ser = pyserial.Serial(port, UNO_BAUD, timeout=0.1, dsrdtr=False)
                self.port = port
                # If the open did toggle DTR, give the UNO time to settle.
                time.sleep(UNO_BOOT_SETTLE_S)
                log.info("UNO serial open: %s", port)
                return True
            except Exception as e:
                log.warning("open %s failed: %s", port, e)
                self.ser = None
                self.port = None
                return False

    def write_line(self, line: str) -> None:
        if not self.is_open():
            if not self.open():
                raise HTTPException(503, "UNO serial not available")
        with self._lock:
            try:
                payload = (line.rstrip("\r\n") + "\n").encode("utf-8")
                self.ser.write(payload)
                self.ser.flush()
                log.info("UNO ← %r", line)
            except (pyserial.SerialException, OSError) as e:
                log.warning("UNO write failed: %s — dropping link", e)
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self.port = None
                raise HTTPException(503, f"UNO write failed: {e}")


uno_link = UnoLink()


def find_ch340_port() -> Optional[str]:
    for dev in sorted(glob.glob("/dev/ttyUSB*")):
        try:
            out = subprocess.check_output(
                ["udevadm", "info", "-q", "property", "-n", dev],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode()
        except Exception:
            continue
        props = dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line
        )
        if (props.get("ID_VENDOR_ID") == CH340_VENDOR
                and props.get("ID_MODEL_ID") == CH340_PRODUCT):
            return dev
    return None


def _run(cmd: list[str], timeout: int = 180) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def compile_and_upload(sketch_dir: Path, label: str) -> dict:
    if not sketch_dir.exists():
        raise HTTPException(500, f"{label} sketch missing at {sketch_dir}")
    port = find_ch340_port()
    if not port:
        raise HTTPException(503, "CH340 (Arduino) not found on any /dev/ttyUSB*")

    logs: list[str] = []
    try:
        rc, out = _run(
            ["arduino-cli", "compile", "--fqbn", ARDUINO_FQBN, str(sketch_dir)],
            timeout=180,
        )
    except FileNotFoundError:
        raise HTTPException(500, "arduino-cli not installed")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "compile timed out")
    logs.append(f"$ arduino-cli compile {label}\n{out}")
    if rc != 0:
        return {"success": False, "stage": "compile", "log": "\n\n".join(logs)}

    upload_cmd = (
        f"arduino-cli upload -p {port} --fqbn {ARDUINO_FQBN} {sketch_dir}"
    )
    try:
        rc, out = _run(["sg", "uucp", "-c", upload_cmd], timeout=120)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "upload timed out")
    logs.append(f"$ sg uucp -c '{upload_cmd}'\n{out}")
    if rc != 0:
        return {"success": False, "stage": "upload", "log": "\n\n".join(logs)}

    return {"success": True, "stage": "done", "port": port, "log": "\n\n".join(logs)}


app = FastAPI(title="Stepper Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class FlashPayload(BaseModel):
    stem: str
    header: str


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "port": find_ch340_port(),
        "sketches_dir": str(SKETCHES_DIR),
        "uno_open": uno_link.is_open(),
    }


@app.post("/flash")
def flash(payload: FlashPayload) -> dict:
    if not PLAYER_DIR.exists():
        raise HTTPException(500, f"player sketch dir missing: {PLAYER_DIR}")
    (PLAYER_DIR / "song.h").write_text(payload.header, encoding="utf-8")
    log.info("flash request stem=%s header_len=%d", payload.stem, len(payload.header))
    # Release any persistent UNO connection so arduino-cli can grab the port.
    uno_link.close()
    result = compile_and_upload(PLAYER_DIR, f"player[{payload.stem}]")
    if result.get("success"):
        # UNO is now in player setup() waiting for "GO\n". Reopen the link
        # so /go can fire immediately. open() includes the DTR-settle wait.
        uno_link.open()
    return result


@app.post("/go")
def go() -> dict:
    """Release the player from its GO-wait so it starts the first notes."""
    uno_link.write_line("GO")
    return {"ok": True, "port": uno_link.port}


@app.post("/tick")
def tick() -> dict:
    """Fire the A/V sync clapper: all motors do one short burst while the
    player is still in its GO-wait. Sent at the pre-roll midpoint."""
    uno_link.write_line("TICK")
    return {"ok": True, "port": uno_link.port}


@app.post("/stop")
def stop() -> dict:
    log.info("stop request")
    uno_link.close()
    return compile_and_upload(SILENCE_DIR, "silence")


@app.post("/motor-test")
def motor_test() -> dict:
    log.info("motor-test request")
    uno_link.close()
    return compile_and_upload(MOTOR_TEST_DIR, "motor_test")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("STEPPER_BRIDGE_PORT", "8766"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
