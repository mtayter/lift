#!/usr/bin/env python3
"""Lift Log backend: stdlib-only HTTP + SQLite + Google SSO.

Reuses the Math Quest SSO pattern verbatim (same Google OAuth client,
same origin https://mtayter.noip.me): GSI popup -> id_token -> verify via
Google tokeninfo -> own 30-day session cookie (lift_session).

Routes (an optional /lift prefix is stripped, so it works behind the
Apache /lift/ proxy as well as directly):
  GET  /                      -> index.html
  GET  /static/<path>         -> static files
  GET  /api/config            -> {google_client_id}
  GET  /api/me                -> user + has_schedule (200 even signed out)
  POST /api/auth              {id_token}
  POST /api/logout
  GET  /api/schedule          -> {schedule|null}            (auth)
  PUT  /api/schedule          {schedule}                     (auth)
  GET  /api/workout/today     -> today's day + last-same-day  (auth)
  POST /api/workouts          {day_index, date, entries, ended_early} (auth)
  GET  /api/history           -> all workouts, newest first   (auth)
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DB_PATH = os.path.join(HERE, "lift.db")
CONFIG_PATH = os.path.join(HERE, "config.json")

config = {"google_client_id": "", "port": 18794}
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as f:
        config.update(json.load(f))

db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

with db_lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, sub TEXT UNIQUE, email TEXT, name TEXT,
      picture TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sessions(
      token_hash TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT,
      expires_at TEXT);
    CREATE TABLE IF NOT EXISTS schedules(
      user_id INTEGER PRIMARY KEY, data TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS workouts(
      id INTEGER PRIMARY KEY, user_id INTEGER, day_index INTEGER,
      day_label TEXT, date TEXT, ended_early INTEGER DEFAULT 0,
      schedule_snapshot TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS workout_sets(
      id INTEGER PRIMARY KEY, workout_id INTEGER, seq INTEGER,
      step_index INTEGER, exercise_name TEXT, set_number INTEGER,
      group_id TEXT, group_mode TEXT, weight REAL, reps INTEGER);
    """)
    db.commit()

TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css",
        ".js": "application/javascript", ".png": "image/png",
        ".svg": "image/svg+xml", ".ico": "image/x-icon"}


def verify_google_token(id_token):
    if not config.get("google_client_id"):
        return None, "server not configured"
    url = TOKENINFO + "?" + urllib.parse.urlencode({"id_token": id_token})
    req = urllib.request.Request(url, headers={"User-Agent": "lift-log"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read().decode())
    except Exception:
        return None, "token rejected"
    if info.get("aud") != config["google_client_id"]:
        return None, "audience mismatch"
    if int(info.get("exp", 0)) < time.time():
        return None, "token expired"
    return info, None


def get_user(uid):
    with db_lock:
        return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def get_or_create_user(info):
    with db_lock:
        row = db.execute("SELECT * FROM users WHERE sub=?",
                         (info["sub"],)).fetchone()
        if row:
            db.execute("UPDATE users SET name=?, picture=?, email=? WHERE id=?",
                       (info.get("name"), info.get("picture"),
                        info.get("email"), row["id"]))
            db.commit()
            return row["id"]
        cur = db.execute(
            "INSERT INTO users(sub,email,name,picture,created_at) VALUES(?,?,?,?,?)",
            (info["sub"], info.get("email"), info.get("name"),
             info.get("picture"), datetime.utcnow().isoformat()))
        db.commit()
        return cur.lastrowid


def day_label(idx):
    return chr(ord("A") + idx) if 0 <= idx < 26 else f"#{idx + 1}"


def validate_schedule(raw):
    """Return (clean_schedule, error). Enforces the Edit-screen invariants:
    1+ days, 1+ exercises per day, supersets are >=2 contiguous exercises
    in one day, group mode is 'entire' or 'each'."""
    if not isinstance(raw, dict):
        return None, "schedule must be an object"
    try:
        rest = float(raw.get("rest_minutes", 2))
    except (TypeError, ValueError):
        return None, "rest_minutes must be a number"
    if not (0 < rest <= 60):
        return None, "rest_minutes must be between 0 and 60"
    days = raw.get("days")
    if not isinstance(days, list) or not (1 <= len(days) <= 14):
        return None, "schedule needs 1-14 days"
    clean_days = []
    for day in days:
        if not isinstance(day, dict):
            return None, "day must be an object"
        exs = day.get("exercises")
        if not isinstance(exs, list) or not (1 <= len(exs) <= 50):
            return None, "each day needs 1-50 exercises"
        groups_in = day.get("groups") or {}
        if not isinstance(groups_in, dict):
            return None, "groups must be an object"
        clean_exs = []
        for ex in exs:
            if not isinstance(ex, dict):
                return None, "exercise must be an object"
            name = str(ex.get("name", ""))[:100]
            try:
                sets = int(ex.get("sets", 1))
            except (TypeError, ValueError):
                return None, "sets must be a number"
            if not (1 <= sets <= 20):
                return None, "sets must be 1-20"
            gid = ex.get("group_id")
            if gid is not None:
                gid = str(gid)[:40]
            clean_exs.append({"name": name, "sets": sets, "group_id": gid})
        # validate groups: >=2 members, contiguous, valid mode
        clean_groups = {}
        seen = {}
        for i, ex in enumerate(clean_exs):
            gid = ex["group_id"]
            if gid is None:
                continue
            seen.setdefault(gid, []).append(i)
        for gid, idxs in seen.items():
            if len(idxs) < 2 or idxs != list(range(idxs[0], idxs[0] + len(idxs))):
                # dissolve invalid groups rather than rejecting the save
                for i in idxs:
                    clean_exs[i]["group_id"] = None
                continue
            mode = str((groups_in.get(gid) or {}).get("mode", "each"))
            if mode not in ("entire", "each"):
                mode = "each"
            clean_groups[gid] = {"mode": mode}
            if mode == "entire":
                # one shared set count for the whole superset
                shared = clean_exs[idxs[0]]["sets"]
                for i in idxs:
                    clean_exs[i]["sets"] = shared
        clean_days.append({"exercises": clean_exs, "groups": clean_groups})
    return {"rest_minutes": rest, "days": clean_days}, None


def counted_sets(day):
    """Timer steps / counted sets for a day, honoring superset mode:
    standalone = its sets; group 'each' = sum of members' sets;
    group 'entire' = the shared round count (once, not per exercise)."""
    total = 0
    seen_groups = set()
    for ex in day["exercises"]:
        gid = ex["group_id"]
        if gid is None:
            total += ex["sets"]
        elif gid not in seen_groups:
            seen_groups.add(gid)
            mode = day["groups"].get(gid, {}).get("mode", "each")
            members = [e for e in day["exercises"] if e["group_id"] == gid]
            total += members[0]["sets"] if mode == "entire" else sum(
                e["sets"] for e in members)
    return total


def get_schedule(uid):
    with db_lock:
        row = db.execute("SELECT data FROM schedules WHERE user_id=?",
                         (uid,)).fetchone()
    return json.loads(row["data"]) if row else None


def workout_to_json(w):
    with db_lock:
        sets = db.execute(
            "SELECT seq, step_index, exercise_name, set_number, group_id,"
            " group_mode, weight, reps FROM workout_sets WHERE workout_id=?"
            " ORDER BY seq", (w["id"],)).fetchall()
    return {"id": w["id"], "day_index": w["day_index"],
            "day_label": w["day_label"], "date": w["date"],
            "ended_early": bool(w["ended_early"]),
            "entries": [dict(s) for s in sets]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _strip_prefix(self, path):
        if path == "/lift" or path.startswith("/lift/"):
            path = path[5:] or "/"
        return path

    def _send(self, code, body, ctype="application/json", cookies=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Opener-Policy",
                         "same-origin-allow-popups")
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj, cookies=None):
        self._send(code, json.dumps(obj), cookies=cookies)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        try:
            return json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return None

    def _session_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("lift_session="):
                return part[len("lift_session="):]
        return None

    def _session_user(self):
        token = self._session_token()
        if not token:
            return None
        th = hashlib.sha256(token.encode()).hexdigest()
        with db_lock:
            row = db.execute(
                "SELECT user_id, expires_at FROM sessions WHERE token_hash=?",
                (th,)).fetchone()
        if not row or row["expires_at"] < datetime.utcnow().isoformat():
            return None
        return row["user_id"]

    def _serve_file(self, rel):
        safe = os.path.normpath(rel).lstrip("/")
        full = os.path.join(STATIC, safe)
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._json(404, {"ok": False})
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Opener-Policy",
                         "same-origin-allow-popups")
        self.end_headers()
        self.wfile.write(data)

    # ---- GET ----
    def do_GET(self):
        path = self._strip_prefix(urllib.parse.urlparse(self.path).path)
        if path in ("/", ""):
            return self._serve_file("index.html")
        if path.startswith("/static/"):
            return self._serve_file(path[len("/static/"):])
        if path == "/api/config":
            return self._json(200, {"google_client_id":
                                    config.get("google_client_id", "")})
        if path == "/api/me":
            uid = self._session_user()
            if not uid:
                return self._json(200, {"ok": False, "signed_in": False})
            u = get_user(uid)
            return self._json(200, {
                "ok": True, "signed_in": True,
                "user": {"name": u["name"], "email": u["email"],
                         "picture": u["picture"]},
                "has_schedule": get_schedule(uid) is not None})
        uid = self._session_user()
        if path == "/api/schedule":
            if not uid:
                return self._json(401, {"ok": False})
            return self._json(200, {"ok": True,
                                    "schedule": get_schedule(uid)})
        if path == "/api/workout/today":
            if not uid:
                return self._json(401, {"ok": False})
            sched = get_schedule(uid)
            if not sched:
                return self._json(200, {"ok": True, "has_schedule": False})
            ndays = len(sched["days"])
            with db_lock:
                last_any = db.execute(
                    "SELECT * FROM workouts WHERE user_id=? ORDER BY id DESC"
                    " LIMIT 1", (uid,)).fetchone()
                if last_any:
                    day_idx = (last_any["day_index"] + 1) % ndays
                    last_same = db.execute(
                        "SELECT * FROM workouts WHERE user_id=? AND"
                        " day_index=? ORDER BY id DESC LIMIT 1",
                        (uid, day_idx)).fetchone()
                else:
                    day_idx, last_same = 0, None
            day = sched["days"][day_idx]
            return self._json(200, {
                "ok": True, "has_schedule": True, "day_index": day_idx,
                "day_label": day_label(day_idx),
                "rest_minutes": sched["rest_minutes"], "day": day,
                "counted_sets": counted_sets(day),
                "last_same_day": (workout_to_json(last_same)
                                  if last_same else None)})
        if path == "/api/history":
            if not uid:
                return self._json(401, {"ok": False})
            with db_lock:
                rows = db.execute(
                    "SELECT * FROM workouts WHERE user_id=? ORDER BY id DESC",
                    (uid,)).fetchall()
            return self._json(200, {"ok": True,
                                    "workouts": [workout_to_json(w)
                                                 for w in rows]})
        return self._json(404, {"ok": False, "error": "not found"})

    # ---- POST / PUT ----
    def do_POST(self):
        path = self._strip_prefix(urllib.parse.urlparse(self.path).path)
        if path == "/api/auth":
            payload = self._json_body()
            if payload is None:
                return self._json(400, {"ok": False})
            info, err = verify_google_token(payload.get("id_token", ""))
            if err:
                return self._json(401, {"ok": False, "error": err})
            uid = get_or_create_user(info)
            token = secrets.token_urlsafe(32)
            th = hashlib.sha256(token.encode()).hexdigest()
            exp = (datetime.utcnow() + timedelta(days=30)).isoformat()
            with db_lock:
                db.execute("INSERT INTO sessions(token_hash,user_id,"
                           "created_at,expires_at) VALUES(?,?,?,?)",
                           (th, uid, datetime.utcnow().isoformat(), exp))
                db.commit()
            u = get_user(uid)
            cookie = (f"lift_session={token}; Path=/; Max-Age=2592000; "
                      f"HttpOnly; SameSite=Lax; Secure")
            return self._json(200, {
                "ok": True,
                "user": {"name": u["name"], "email": u["email"],
                         "picture": u["picture"]},
                "has_schedule": get_schedule(uid) is not None},
                cookies=[cookie])
        if path == "/api/logout":
            token = self._session_token()
            if token:
                th = hashlib.sha256(token.encode()).hexdigest()
                with db_lock:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
                    db.commit()
            return self._json(200, {"ok": True}, cookies=[
                "lift_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax;"
                " Secure"])
        uid = self._session_user()
        if path == "/api/workouts":
            if not uid:
                return self._json(401, {"ok": False})
            p = self._json_body()
            if p is None:
                return self._json(400, {"ok": False})
            sched = get_schedule(uid)
            if not sched:
                return self._json(400, {"ok": False, "error": "no schedule"})
            try:
                day_idx = int(p.get("day_index", 0))
            except (TypeError, ValueError):
                return self._json(400, {"ok": False})
            if not (0 <= day_idx < len(sched["days"])):
                return self._json(400, {"ok": False,
                                        "error": "bad day_index"})
            date_str = str(p.get("date", ""))[:10]
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                date_str = datetime.now().date().isoformat()
            entries = p.get("entries")
            if not isinstance(entries, list) or len(entries) > 500:
                return self._json(400, {"ok": False, "error": "bad entries"})
            clean = []
            for i, e in enumerate(entries):
                if not isinstance(e, dict):
                    return self._json(400, {"ok": False})
                def num(v, cast):
                    try:
                        return cast(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        return None
                clean.append({
                    "seq": i,
                    "step_index": num(e.get("step_index"), int) or 0,
                    "exercise_name": str(e.get("exercise_name", ""))[:100],
                    "set_number": num(e.get("set_number"), int) or 0,
                    "group_id": (str(e["group_id"])[:40]
                                 if e.get("group_id") else None),
                    "group_mode": (e.get("group_mode")
                                   if e.get("group_mode") in ("entire", "each")
                                   else None),
                    "weight": num(e.get("weight"), float),
                    "reps": num(e.get("reps"), int)})
            with db_lock:
                cur = db.execute(
                    "INSERT INTO workouts(user_id,day_index,day_label,date,"
                    "ended_early,schedule_snapshot,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (uid, day_idx, day_label(day_idx), date_str,
                     1 if p.get("ended_early") else 0,
                     json.dumps(sched["days"][day_idx]),
                     datetime.utcnow().isoformat()))
                wid = cur.lastrowid
                db.executemany(
                    "INSERT INTO workout_sets(workout_id,seq,step_index,"
                    "exercise_name,set_number,group_id,group_mode,weight,"
                    "reps) VALUES(?,?,?,?,?,?,?,?,?)",
                    [(wid, e["seq"], e["step_index"], e["exercise_name"],
                      e["set_number"], e["group_id"], e["group_mode"],
                      e["weight"], e["reps"]) for e in clean])
                db.commit()
            return self._json(200, {"ok": True, "workout_id": wid})
        return self._json(404, {"ok": False})

    def do_PUT(self):
        path = self._strip_prefix(urllib.parse.urlparse(self.path).path)
        if path == "/api/schedule":
            uid = self._session_user()
            if not uid:
                return self._json(401, {"ok": False})
            p = self._json_body()
            if p is None:
                return self._json(400, {"ok": False})
            clean, err = validate_schedule(p.get("schedule"))
            if err:
                return self._json(400, {"ok": False, "error": err})
            with db_lock:
                db.execute(
                    "INSERT INTO schedules(user_id,data,updated_at)"
                    " VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET"
                    " data=excluded.data, updated_at=excluded.updated_at",
                    (uid, json.dumps(clean), datetime.utcnow().isoformat()))
                db.commit()
            return self._json(200, {"ok": True, "schedule": clean})
        return self._json(404, {"ok": False})


def main():
    port = int(config.get("port", 18794))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"lift-log listening on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
