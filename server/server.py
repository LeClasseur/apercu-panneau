#!/usr/bin/env python3
"""Collecte des avis et de la telemetrie anonyme de la page d'essai du panneau LeClasseur.

Deux ports, deux portes :
- PUBLIC_PORT (derriere le tunnel Cloudflare) : seulement POST /api/t, POST /api/vote,
  GET /api/vote?visitor=..., GET /api/health. Rien d'autre ne repond.
- PRIVATE_PORT (tailnet / localhost seulement) : GET /resultats et GET /api/results.

Aucune dependance hors de la bibliotheque standard. Les donnees vont dans un SQLite
(DATA_DIR/apercu.db). Les adresses IP ne sont jamais gardees : seulement un hachage sale,
qui sert uniquement a signaler deux avis venant du meme reseau.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import results  # noqa: E402  (same directory)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
BIND = os.environ.get("BIND", "0.0.0.0")
PUBLIC_PORT = int(os.environ.get("PUBLIC_PORT", "8080"))
PRIVATE_PORT = int(os.environ.get("PRIVATE_PORT", "8081"))
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "https://leclasseur.github.io").split(",") if o.strip()}
MAX_BODY = 64 * 1024
MAX_EVENTS = 300
MAX_ROWS = 2_000_000
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
RATE_PER_MIN = 90

_db_lock = threading.Lock()
_rate: dict[str, list[float]] = {}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DATA_DIR / "apercu.db", timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, visitor_id TEXT NOT NULL,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                active_total INTEGER NOT NULL DEFAULT 0, active TEXT NOT NULL DEFAULT '{}',
                ua TEXT NOT NULL DEFAULT '{}', ip_hash TEXT NOT NULL DEFAULT '', last_seq INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS sessions_visitor ON sessions(visitor_id);
            CREATE TABLE IF NOT EXISTS events (
                session_id TEXT NOT NULL, seq INTEGER NOT NULL, idx INTEGER NOT NULL,
                t INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL, received REAL NOT NULL,
                PRIMARY KEY (session_id, seq, idx));
            CREATE TABLE IF NOT EXISTS votes (
                visitor_id TEXT PRIMARY KEY, vote TEXT NOT NULL, missions TEXT NOT NULL, mission_order TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, revisions INTEGER NOT NULL DEFAULT 1,
                ip_hash TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS vote_history (
                visitor_id TEXT NOT NULL, vote TEXT NOT NULL, at REAL NOT NULL);
            """
        )


def salt() -> bytes:
    p = DATA_DIR / "salt"
    if not p.exists():
        old = os.umask(0o077)
        try:
            p.write_bytes(secrets.token_bytes(32))
        finally:
            os.umask(old)
    return p.read_bytes()


SALT = b""


def ip_hash(ip: str) -> str:
    return hashlib.sha256(SALT + ip.encode()).hexdigest()[:16] if ip else ""


def clean(value, depth=0):
    """Keep only small JSON scalars/containers: strings cut at 200 chars, no deep nesting."""
    if depth > 4:
        return None
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if abs(value) < 1e13 else None
    if isinstance(value, list):
        return [clean(v, depth + 1) for v in value[:50]]
    if isinstance(value, dict):
        return {str(k)[:40]: clean(v, depth + 1) for k, v in list(value.items())[:60]}
    return None


def counter_map(value) -> dict:
    """active.* maps: {key: seconds}, keys short, values bounded integers."""
    out = {}
    if isinstance(value, dict):
        for group, inner in list(value.items())[:8]:
            if isinstance(inner, dict):
                out[str(group)[:20]] = {str(k)[:60]: int(v) for k, v in list(inner.items())[:200]
                                        if isinstance(v, (int, float)) and 0 <= v < 10**7}
    return out


def handle_telemetry(body: dict, iph: str) -> tuple[int, dict]:
    visitor, session = str(body.get("visitor", "")), str(body.get("session", ""))
    if not (ID_RE.match(visitor) and ID_RE.match(session)):
        return 400, {"error": "bad ids"}
    seq = body.get("seq")
    if not isinstance(seq, int) or not 0 < seq < 10**6:
        return 400, {"error": "bad seq"}
    events = body.get("events") or []
    if not isinstance(events, list):
        return 400, {"error": "bad events"}
    active_total = body.get("activeTotal") if isinstance(body.get("activeTotal"), int) else 0
    now = time.time()
    with _db_lock, db() as con:
        (rows,) = con.execute("SELECT COUNT(*) FROM events").fetchone()
        if rows > MAX_ROWS:
            return 507, {"error": "full"}
        cur = con.execute("SELECT last_seq, active_total FROM sessions WHERE session_id=?", (session,)).fetchone()
        active = json.dumps(counter_map(body.get("active")))
        ua = json.dumps(clean(body.get("ua") or {}))
        if cur is None:
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                        (session, visitor, now, now, active_total, active, ua, iph, seq))
        else:
            # active counters are cumulative per session: keep the largest snapshot (beacons can arrive out of order)
            if active_total >= cur[1]:
                con.execute("UPDATE sessions SET active_total=?, active=?, ua=? WHERE session_id=?", (active_total, active, ua, session))
            con.execute("UPDATE sessions SET last_seen=?, last_seq=MAX(last_seq, ?) WHERE session_id=?", (now, seq, session))
        for i, ev in enumerate(events[:MAX_EVENTS]):
            if not isinstance(ev, dict):
                continue
            ev = clean(ev)
            t = ev.pop("t", 0) if isinstance(ev.get("t"), int) else 0
            typ = str(ev.pop("type", "?"))[:40]
            con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)", (session, seq, i, t, typ, json.dumps(ev), now))
    return 200, {"ok": True}


VOTE_KEYS = {"pref", "least", "color", "role", "comment", "name", "ratings"}


def handle_vote(body: dict, iph: str) -> tuple[int, dict]:
    visitor = str(body.get("visitor", ""))
    if not ID_RE.match(visitor):
        return 400, {"error": "bad visitor"}
    raw = body.get("vote")
    if not isinstance(raw, dict):
        return 400, {"error": "bad vote"}
    vote = {k: clean(v) for k, v in raw.items() if k in VOTE_KEYS}
    vote["comment"] = str(raw.get("comment", ""))[:2000]
    vote["name"] = str(raw.get("name", ""))[:60]
    ratings = raw.get("ratings") if isinstance(raw.get("ratings"), dict) else {}
    vote["ratings"] = {str(k)[:20]: int(v) for k, v in list(ratings.items())[:10] if isinstance(v, (int, float)) and 1 <= v <= 5}
    missions = json.dumps(clean(body.get("missions") or [])[:20])
    order = json.dumps(clean(body.get("order") or [])[:8])
    now = time.time()
    with _db_lock, db() as con:
        cur = con.execute("SELECT revisions FROM votes WHERE visitor_id=?", (visitor,)).fetchone()
        if cur is None:
            con.execute("INSERT INTO votes VALUES (?,?,?,?,?,?,?,?)", (visitor, json.dumps(vote), missions, order, now, now, 1, iph))
        else:
            con.execute("UPDATE votes SET vote=?, missions=?, mission_order=?, updated=?, revisions=revisions+1, ip_hash=? WHERE visitor_id=?",
                        (json.dumps(vote), missions, order, now, iph, visitor))
        con.execute("INSERT INTO vote_history VALUES (?,?,?)", (visitor, json.dumps(vote), now))
    return 200, {"ok": True, "updated": cur is not None}


def get_vote(visitor: str) -> tuple[int, dict]:
    if not ID_RE.match(visitor):
        return 400, {"error": "bad visitor"}
    with db() as con:
        row = con.execute("SELECT vote FROM votes WHERE visitor_id=?", (visitor,)).fetchone()
    return 200, {"vote": json.loads(row[0]) if row else None}


def rate_ok(key: str) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(key, []) if now - t < 60]
    hits.append(now)
    _rate[key] = hits
    if len(_rate) > 5000:
        _rate.clear()
    return len(hits) <= RATE_PER_MIN


class Base(BaseHTTPRequestHandler):
    server_version = "apercu"
    sys_version = ""

    def log_message(self, fmt, *args):  # no IPs in logs
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def send(self, code: int, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)


class Public(Base):
    def client_ip(self) -> str:
        # This port is bound to the machine only; Tailscale Funnel proxies to it and appends the real
        # visitor address as the LAST X-Forwarded-For entry (earlier entries are client-supplied).
        xff = [p.strip() for p in self.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        return xff[-1] if xff else self.client_address[0]

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/health":
            return self.send(200, {"ok": True})
        if u.path == "/api/vote":
            if not rate_ok(self.client_ip()):
                return self.send(429, {"error": "slow down"})
            return self.send(*get_vote((parse_qs(u.query).get("visitor") or [""])[0]))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/api/t", "/api/vote"):
            return self.send(404, {"error": "not found"})
        ip = self.client_ip()
        if not rate_ok(ip):
            return self.send(429, {"error": "slow down"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send(400, {"error": "bad length"})
        if not 0 < length <= MAX_BODY:
            return self.send(413, {"error": "too big"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self.send(400, {"error": "bad json"})
        handler = handle_telemetry if u.path == "/api/t" else handle_vote
        try:
            self.send(*handler(body, ip_hash(ip)))
        except sqlite3.Error as e:
            sys.stderr.write(f"db error: {e}\n")
            self.send(500, {"error": "db"})


class Private(Base):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/resultats"):
            return self.send(200, results.page().encode(), "text/html; charset=utf-8")
        if u.path == "/api/results":
            with db() as con:
                return self.send(200, results.compute(con))
        if u.path == "/api/health":
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def main():
    global SALT
    init_db()
    SALT = salt()
    pub = ThreadingHTTPServer((BIND, PUBLIC_PORT), Public)
    priv = ThreadingHTTPServer((BIND, PRIVATE_PORT), Private)
    threading.Thread(target=priv.serve_forever, daemon=True).start()
    sys.stderr.write(f"apercu: public :{PUBLIC_PORT}, private :{PRIVATE_PORT}, data {DATA_DIR}\n")
    pub.serve_forever()


if __name__ == "__main__":
    main()
