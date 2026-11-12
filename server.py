#!/usr/bin/env python3
"""Lift Log backend: stdlib-only HTTP + SQLite + Google SSO.

Reuses the Math Quest SSO pattern verbatim (same Google OAuth client,
same origin https://mtayter.noip.me): GSI popup -> id_token -> verify via
Google tokeninfo -> own 30-day session cookie (lift_session).

Data access rule: after authentication, ALL database access goes through
UserDB, a wrapper bound to the logged-in user's id. Every UserDB method
carries the user_id filter (or, for workout_sets, the ownership join)
inside it, so a handler cannot touch another user's rows — scoping is
guaranteed by construction, not by per-query discipline. The raw
connection is used only in this file's startup/migration block and in
the auth bootstrap functions (get_or_create_user, create_session,
delete_session, user_id_for_token), which are inherently pre-user or
keyed by session token. A test in test_local.py asserts the Handler
class contains no raw db.* calls.

v2: multiple named schedules per user, each fully independent — its own
day rotation and its own workout history. Exercises carry stable IDs;
workout sets link to history by exercise ID + set number, so editing an
exercise (rename / reorder / regroup / change sets) keeps the link,
while deleting it (the ID disappears) loses it. Copying a schedule
regenerates every exercise ID, so a copy starts with no history — by
design, and the UI warns about exactly that.

Routes (an optional /lift prefix is stripped):
  GET    /                        -> index.html
  GET    /static/<path>           -> static files
  GET    /api/config              -> {google_client_id}
  GET    /api/me                  -> user + has_schedule (200 even signed out)
  POST   /api/auth                {id_token}
  POST   /api/logout
  GET    /api/schedules           -> [{id,name,created_at,updated_at,days}] newest first (auth)
  POST   /api/schedules           {name?, schedule?} -> create            (auth)
  GET    /api/schedules/<id>      -> {id,name,data,linked_exercise_ids}   (auth)
  PUT    /api/schedules/<id>      {name, schedule} -> update in place     (auth)
  DELETE /api/schedules/<id>      -> delete (not the last one)            (auth)
  POST   /api/schedules/<id>/copy -> duplicate with fresh exercise IDs    (auth)
  GET    /api/workout/today?schedule_id=<id> -> today's day for that
           schedule (default: newest), rotation + last-same-day scoped
           to that schedule only                                          (auth)
  POST   /api/workouts            {schedule_id, day_index, date, entries,
                                   ended_early}                           (auth)
  GET    /api/history             -> all workouts, newest first           (auth)
  PUT    /api/workout-sets/<id>   {exercise_name, weight, reps, band?,
           measure?} -> edit one saved set in History (owner only)
"""
import copy as _copy
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
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


def _cols(table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


# ---- startup / migration (raw DB; runs once, pre-user) ----
with db_lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, sub TEXT UNIQUE, email TEXT, name TEXT,
      picture TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sessions(
      token_hash TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT,
      expires_at TEXT);
    CREATE TABLE IF NOT EXISTS workouts(
      id INTEGER PRIMARY KEY, user_id INTEGER, schedule_id INTEGER,
      schedule_name TEXT, day_index INTEGER,
      day_label TEXT, date TEXT, ended_early INTEGER DEFAULT 0,
      schedule_snapshot TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS workout_sets(
      id INTEGER PRIMARY KEY, workout_id INTEGER, seq INTEGER,
      step_index INTEGER, exercise_id TEXT, exercise_name TEXT,
      set_number INTEGER, group_id TEXT, group_mode TEXT,
      weight REAL, band TEXT, reps INTEGER);
    """)
    # Column additions must land before the schedules migration below,
    # whose UPDATE touches workouts.schedule_id.
    if "schedule_id" not in _cols("workouts"):
        db.execute("ALTER TABLE workouts ADD COLUMN schedule_id INTEGER")
    if "schedule_name" not in _cols("workouts"):
        db.execute("ALTER TABLE workouts ADD COLUMN schedule_name TEXT")
    if "exercise_id" not in _cols("workout_sets"):
        db.execute("ALTER TABLE workout_sets ADD COLUMN exercise_id TEXT")
    if "band" not in _cols("workout_sets"):
        db.execute("ALTER TABLE workout_sets ADD COLUMN band TEXT")
    if "measure" not in _cols("workout_sets"):
        db.execute("ALTER TABLE workout_sets ADD COLUMN measure TEXT")
    # v1 -> v2 migration: v1 schedules was (user_id PK, data, updated_at)
    if _cols("schedules") and "id" not in _cols("schedules"):
        db.execute("ALTER TABLE schedules RENAME TO schedules_v1")
        db.execute("""
          CREATE TABLE schedules(
            id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT,
            data TEXT, created_at TEXT, updated_at TEXT)""")
        for row in db.execute("SELECT * FROM schedules_v1").fetchall():
            cur = db.execute(
                "INSERT INTO schedules(user_id,name,data,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                (row["user_id"], "My Schedule", row["data"],
                 row["updated_at"], row["updated_at"]))
            db.execute("UPDATE workouts SET schedule_id=? WHERE user_id=?"
                       " AND schedule_id IS NULL",
                       (cur.lastrowid, row["user_id"]))
        db.execute("DROP TABLE schedules_v1")
    db.execute("""
      CREATE TABLE IF NOT EXISTS schedules(
        id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT,
        data TEXT, created_at TEXT, updated_at TEXT)""")
    db.commit()

def _app_version():
    """Deploy fingerprint: hash of the server + static assets. Clients
    poll /api/version and reload when it changes (only when safe —
    never mid-workout or with unsaved edits)."""
    h = hashlib.sha256()
    for p in (os.path.join(HERE, "server.py"),
              os.path.join(STATIC, "index.html"),
              os.path.join(STATIC, "app.js"),
              os.path.join(STATIC, "styles.css")):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


APP_VERSION = _app_version()

TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css",
        ".js": "application/javascript", ".png": "image/png",
        ".svg": "image/svg+xml", ".ico": "image/x-icon"}


# ---- auth bootstrap (raw DB; pre-user or keyed by session token) ----
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


def create_session(uid):
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    exp = (datetime.utcnow() + timedelta(days=30)).isoformat()
    with db_lock:
        db.execute("INSERT INTO sessions(token_hash,user_id,created_at,"
                   "expires_at) VALUES(?,?,?,?)",
                   (th, uid, datetime.utcnow().isoformat(), exp))
        db.commit()
    return token


def delete_session(token):
    if not token:
        return
    th = hashlib.sha256(token.encode()).hexdigest()
    with db_lock:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (th,))
        db.commit()


def user_id_for_token(token):
    """Resolve a session cookie token to a user id, or None. This is the
    one lookup that creates a UserDB, so it lives in the bootstrap."""
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


# ---- pure schedule logic (no DB) ----
def clean_band(v):
    """Canonical band combo: any subset of L/M/H as 'M+H' (ascending
    L<M<H, '+'-joined). Empty/invalid -> None."""
    if not v:
        return None
    letters = {c for c in str(v).upper() if c in "LMH"}
    if not letters:
        return None
    return "+".join(c for c in "LMH" if c in letters)


def day_label(idx):
    return chr(ord("A") + idx) if 0 <= idx < 26 else f"#{idx + 1}"


def validate_schedule(raw):
    """Return (clean_schedule, error). Enforces the Edit-screen invariants:
    1+ days, 1+ exercises per day, supersets are >=2 contiguous exercises
    in one day, group mode is 'entire' or 'each'. Exercises keep a stable
    `id` (assigned here if missing) — that ID is the history link."""
    if not isinstance(raw, dict):
        return None, "schedule must be an object"
    # Timer is per-schedule (Edit screen, left of "Time between sets").
    # Off = no rest input, no per-day total time, and the workout
    # advances on entered values instead of a countdown. Old schedules
    # without the key default to on.
    timer_enabled = raw.get("timer_enabled", True)
    timer_enabled = bool(timer_enabled) if timer_enabled is not None else True
    try:
        rest = float(raw.get("rest_minutes", 2))
    except (TypeError, ValueError):
        return None, "rest_minutes must be a number"
    if timer_enabled and not (0 < rest <= 60):
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
            ex_id = ex.get("id")
            ex_id = str(ex_id)[:40] if ex_id else uuid.uuid4().hex[:12]
            measure = ex.get("measure")
            if measure not in ("weight", "band", "bodyweight"):
                measure = "weight"
            clean_exs.append({"id": ex_id, "name": name, "sets": sets,
                              "group_id": gid, "measure": measure})
        clean_groups = {}
        seen = {}
        for i, ex in enumerate(clean_exs):
            gid = ex["group_id"]
            if gid is None:
                continue
            seen.setdefault(gid, []).append(i)
        for gid, idxs in seen.items():
            if len(idxs) < 2 or idxs != list(range(idxs[0], idxs[0] + len(idxs))):
                for i in idxs:
                    clean_exs[i]["group_id"] = None
                continue
            mode = str((groups_in.get(gid) or {}).get("mode", "each"))
            if mode not in ("entire", "each"):
                mode = "each"
            clean_groups[gid] = {"mode": mode}
            if mode == "entire":
                shared = clean_exs[idxs[0]]["sets"]
                for i in idxs:
                    clean_exs[i]["sets"] = shared
        clean_days.append({"exercises": clean_exs, "groups": clean_groups})
    return {"rest_minutes": rest, "timer_enabled": timer_enabled,
            "days": clean_days}, None


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


def fresh_exercise_ids(data):
    """Deep-copy schedule data with every exercise ID regenerated —
    used by Copy so the copy is history-independent."""
    data = _copy.deepcopy(data)
    for day in data["days"]:
        for ex in day["exercises"]:
            ex["id"] = uuid.uuid4().hex[:12]
    return data


# ---- user-scoped data layer ----
class UserDB:
    """All post-auth DB access for one user. Constructed with the
    logged-in user's id; every query below is filtered by that id
    (workout_sets queries go through an ownership join on workouts),
    so callers physically cannot read or write another user's rows."""

    def __init__(self, uid):
        self.uid = uid

    # users
    def user(self):
        with db_lock:
            return db.execute("SELECT * FROM users WHERE id=?",
                              (self.uid,)).fetchone()

    # schedules
    def schedules(self):
        """Metadata for all of this user's schedules, newest first."""
        with db_lock:
            rows = db.execute(
                "SELECT * FROM schedules WHERE user_id=? ORDER BY"
                " updated_at DESC, id DESC", (self.uid,)).fetchall()
        return [{"id": r["id"], "name": r["name"],
                 "created_at": r["created_at"],
                 "updated_at": r["updated_at"],
                 "days": len(json.loads(r["data"])["days"])} for r in rows]

    def schedule_count(self):
        with db_lock:
            row = db.execute("SELECT COUNT(*) c FROM schedules WHERE user_id=?",
                             (self.uid,)).fetchone()
        return row["c"]

    def schedule_row(self, sid):
        with db_lock:
            return db.execute(
                "SELECT * FROM schedules WHERE user_id=? AND id=?",
                (self.uid, sid)).fetchone()

    def create_schedule(self, name, data):
        now = datetime.utcnow().isoformat()
        with db_lock:
            cur = db.execute(
                "INSERT INTO schedules(user_id,name,data,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                (self.uid, name, json.dumps(data), now, now))
            db.commit()
            return cur.lastrowid

    def update_schedule(self, sid, name, data):
        with db_lock:
            cur = db.execute(
                "UPDATE schedules SET name=?, data=?, updated_at=?"
                " WHERE user_id=? AND id=?",
                (name, json.dumps(data), datetime.utcnow().isoformat(),
                 self.uid, sid))
            db.commit()
            return cur.rowcount > 0

    def persist_schedule_data(self, sid, data):
        """Rewrite a schedule's data WITHOUT touching name/updated_at —
        used to backfill exercise IDs on read."""
        with db_lock:
            db.execute("UPDATE schedules SET data=? WHERE user_id=? AND id=?",
                       (json.dumps(data), self.uid, sid))
            db.commit()

    def delete_schedule(self, sid):
        with db_lock:
            db.execute("DELETE FROM schedules WHERE user_id=? AND id=?",
                       (self.uid, sid))
            db.commit()

    def schedule_data(self, row):
        """Parse a schedule row's data, backfilling (and persisting)
        stable exercise IDs for any exercise that lacks one — e.g. v1
        schedules. Does not touch updated_at, so 'newest' ordering is
        unaffected."""
        data = json.loads(row["data"])
        # Pre-v2.7 schedules have no timer flag: timer on, as before.
        data.setdefault("timer_enabled", True)
        changed = False
        for day in data["days"]:
            for ex in day["exercises"]:
                if not ex.get("id"):
                    ex["id"] = uuid.uuid4().hex[:12]
                    changed = True
        if changed:
            self.persist_schedule_data(row["id"], data)
        return data

    def linked_exercise_ids(self, sid):
        with db_lock:
            rows = db.execute(
                "SELECT DISTINCT ws.exercise_id FROM workout_sets ws"
                " JOIN workouts w ON w.id = ws.workout_id"
                " WHERE w.user_id=? AND w.schedule_id=? AND"
                " ws.exercise_id IS NOT NULL",
                (self.uid, sid)).fetchall()
        return [r["exercise_id"] for r in rows]

    # workouts
    def last_workout(self, sid):
        with db_lock:
            return db.execute(
                "SELECT * FROM workouts WHERE user_id=? AND schedule_id=?"
                " ORDER BY id DESC LIMIT 1", (self.uid, sid)).fetchone()

    def last_workout_for_day(self, sid, day_idx):
        with db_lock:
            return db.execute(
                "SELECT * FROM workouts WHERE user_id=? AND schedule_id=?"
                " AND day_index=? ORDER BY id DESC LIMIT 1",
                (self.uid, sid, day_idx)).fetchone()

    def workouts(self):
        with db_lock:
            return db.execute(
                "SELECT * FROM workouts WHERE user_id=? ORDER BY id DESC",
                (self.uid,)).fetchall()

    def sets_for_workout(self, workout_id):
        """Sets of a workout — but only if the workout is this user's
        (the join enforces it even if a foreign id is passed)."""
        with db_lock:
            return db.execute(
                "SELECT ws.id, ws.seq, ws.step_index, ws.exercise_id,"
                " ws.exercise_name, ws.set_number, ws.group_id,"
                " ws.group_mode, ws.weight, ws.band, ws.measure, ws.reps FROM workout_sets ws"
                " JOIN workouts w ON w.id = ws.workout_id"
                " WHERE ws.workout_id=? AND w.user_id=? ORDER BY ws.seq",
                (workout_id, self.uid)).fetchall()

    def workout_json(self, w):
        return {"id": w["id"], "schedule_id": w["schedule_id"],
                "schedule_name": w["schedule_name"],
                "day_index": w["day_index"], "day_label": w["day_label"],
                "date": w["date"], "ended_early": bool(w["ended_early"]),
                "entries": [dict(s) for s in self.sets_for_workout(w["id"])]}

    def insert_workout(self, sid, schedule_name, day_idx, date_str,
                       ended_early, day_snapshot, entries):
        with db_lock:
            cur = db.execute(
                "INSERT INTO workouts(user_id,schedule_id,schedule_name,"
                "day_index,day_label,date,ended_early,schedule_snapshot,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (self.uid, sid, schedule_name, day_idx, day_label(day_idx),
                 date_str, 1 if ended_early else 0,
                 json.dumps(day_snapshot), datetime.utcnow().isoformat()))
            wid = cur.lastrowid
            db.executemany(
                "INSERT INTO workout_sets(workout_id,seq,step_index,"
                "exercise_id,exercise_name,set_number,group_id,group_mode,"
                "weight,band,measure,reps) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(wid, e["seq"], e["step_index"], e["exercise_id"],
                  e["exercise_name"], e["set_number"], e["group_id"],
                  e["group_mode"], e["weight"], e["band"],
                  e.get("measure"), e["reps"])
                 for e in entries])
            db.commit()
            return wid

    def update_workout_set(self, set_id, name, weight, band, reps,
                           measure=None):
        """Edit one saved set. The UPDATE itself is ownership-guarded:
        the EXISTS clause means a foreign set id updates nothing.
        Returns the updated row, or None if not this user's set.
        A valid measure updates the stored measure too (so a bodyweight
        row edited in History stays bodyweight); omitted/invalid leaves
        the stored measure alone."""
        with db_lock:
            if measure in ("weight", "band", "bodyweight"):
                cur = db.execute(
                    "UPDATE workout_sets SET exercise_name=?, weight=?,"
                    " band=?, measure=?, reps=? WHERE id=? AND EXISTS"
                    " (SELECT 1 FROM workouts w"
                    " WHERE w.id = workout_sets.workout_id AND w.user_id=?)",
                    (name, weight, band, measure, reps, set_id, self.uid))
            else:
                cur = db.execute(
                    "UPDATE workout_sets SET exercise_name=?, weight=?, band=?,"
                    " reps=? WHERE id=? AND EXISTS (SELECT 1 FROM workouts w"
                    " WHERE w.id = workout_sets.workout_id AND w.user_id=?)",
                    (name, weight, band, reps, set_id, self.uid))
            db.commit()
            if cur.rowcount == 0:
                return None
            row = db.execute(
                "SELECT ws.* FROM workout_sets ws"
                " JOIN workouts w ON w.id = ws.workout_id"
                " WHERE ws.id=? AND w.user_id=?",
                (set_id, self.uid)).fetchone()
            return dict(row) if row else None


# ---- HTTP handler (UserDB + auth bootstrap only; no raw db.* here) ----
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

    def _udb(self):
        """UserDB for the session's user, or None if signed out."""
        uid = user_id_for_token(self._session_token())
        return UserDB(uid) if uid else None

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
        parsed = urllib.parse.urlparse(self.path)
        path = self._strip_prefix(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if path in ("/", ""):
            return self._serve_file("index.html")
        if path.startswith("/static/"):
            return self._serve_file(path[len("/static/"):])
        if path == "/api/config":
            return self._json(200, {"google_client_id":
                                    config.get("google_client_id", "")})
        if path == "/api/version":
            return self._json(200, {"ok": True, "version": APP_VERSION})
        if path == "/api/me":
            udb = self._udb()
            if not udb:
                return self._json(200, {"ok": False, "signed_in": False})
            u = udb.user()
            return self._json(200, {
                "ok": True, "signed_in": True,
                "user": {"name": u["name"], "email": u["email"],
                         "picture": u["picture"]},
                "has_schedule": udb.schedule_count() > 0})
        udb = self._udb()
        if path == "/api/schedules":
            if not udb:
                return self._json(401, {"ok": False})
            return self._json(200, {"ok": True, "schedules": udb.schedules()})
        if path.startswith("/api/schedules/"):
            if not udb:
                return self._json(401, {"ok": False})
            try:
                sid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json(404, {"ok": False})
            row = udb.schedule_row(sid)
            if not row:
                return self._json(404, {"ok": False})
            return self._json(200, {
                "ok": True, "id": row["id"], "name": row["name"],
                "schedule": udb.schedule_data(row),
                "linked_exercise_ids": udb.linked_exercise_ids(sid)})
        if path == "/api/workout/today":
            if not udb:
                return self._json(401, {"ok": False})
            schedules = udb.schedules()
            if not schedules:
                return self._json(200, {"ok": True, "has_schedule": False,
                                        "schedules": []})
            sid = None
            if qs.get("schedule_id"):
                try:
                    sid = int(qs["schedule_id"][0])
                except ValueError:
                    sid = None
            row = udb.schedule_row(sid) if sid else None
            if row is None:
                row = udb.schedule_row(schedules[0]["id"])  # newest
            sched = udb.schedule_data(row)
            ndays = len(sched["days"])
            last_any = udb.last_workout(row["id"])
            if last_any:
                day_idx = (last_any["day_index"] + 1) % ndays
                last_same = udb.last_workout_for_day(row["id"], day_idx)
            else:
                day_idx, last_same = 0, None
            day = sched["days"][day_idx]
            return self._json(200, {
                "ok": True, "has_schedule": True, "schedules": schedules,
                "schedule_id": row["id"], "schedule_name": row["name"],
                "day_index": day_idx, "day_label": day_label(day_idx),
                "rest_minutes": sched["rest_minutes"],
                "timer_enabled": sched.get("timer_enabled", True),
                "day": day,
                "counted_sets": counted_sets(day),
                "last_same_day": (udb.workout_json(last_same)
                                  if last_same else None)})
        if path == "/api/history":
            if not udb:
                return self._json(401, {"ok": False})
            return self._json(200, {"ok": True, "workouts":
                                    [udb.workout_json(w)
                                     for w in udb.workouts()]})
        return self._json(404, {"ok": False, "error": "not found"})

    # ---- POST / PUT / DELETE ----
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
            token = create_session(uid)
            udb = UserDB(uid)
            u = udb.user()
            cookie = (f"lift_session={token}; Path=/; Max-Age=2592000; "
                      f"HttpOnly; SameSite=Lax; Secure")
            return self._json(200, {
                "ok": True,
                "user": {"name": u["name"], "email": u["email"],
                         "picture": u["picture"]},
                "has_schedule": udb.schedule_count() > 0},
                cookies=[cookie])
        if path == "/api/logout":
            delete_session(self._session_token())
            return self._json(200, {"ok": True}, cookies=[
                "lift_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax;"
                " Secure"])
        udb = self._udb()
        if path == "/api/schedules":
            if not udb:
                return self._json(401, {"ok": False})
            p = self._json_body()
            if p is None:
                return self._json(400, {"ok": False})
            raw = p.get("schedule") or {
                "rest_minutes": 2,
                "days": [{"exercises": [{"name": "", "sets": 1,
                                         "group_id": None}], "groups": {}}]}
            clean, err = validate_schedule(raw)
            if err:
                return self._json(400, {"ok": False, "error": err})
            name = str(p.get("name") or "My Schedule")[:80]
            sid = udb.create_schedule(name, clean)
            return self._json(200, {"ok": True, "id": sid, "name": name,
                                    "schedule": clean})
        if path.startswith("/api/schedules/") and path.endswith("/copy"):
            if not udb:
                return self._json(401, {"ok": False})
            try:
                sid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json(404, {"ok": False})
            row = udb.schedule_row(sid)
            if not row:
                return self._json(404, {"ok": False})
            data = fresh_exercise_ids(udb.schedule_data(row))
            name = (row["name"] + " (copy)")[:80]
            new_id = udb.create_schedule(name, data)
            return self._json(200, {"ok": True, "id": new_id, "name": name,
                                    "schedule": data})
        if path == "/api/workouts":
            if not udb:
                return self._json(401, {"ok": False})
            p = self._json_body()
            if p is None:
                return self._json(400, {"ok": False})
            try:
                sid = int(p.get("schedule_id"))
            except (TypeError, ValueError):
                return self._json(400, {"ok": False,
                                        "error": "schedule_id required"})
            row = udb.schedule_row(sid)
            if not row:
                return self._json(404, {"ok": False,
                                        "error": "schedule not found"})
            sched = udb.schedule_data(row)
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
                band = clean_band(e.get("band"))
                measure = e.get("measure")
                if band:
                    measure = "band"
                elif measure not in ("weight", "band", "bodyweight"):
                    measure = "weight"
                clean.append({
                    "seq": i,
                    "step_index": num(e.get("step_index"), int) or 0,
                    "exercise_id": (str(e["exercise_id"])[:40]
                                    if e.get("exercise_id") else None),
                    "exercise_name": str(e.get("exercise_name", ""))[:100],
                    "set_number": num(e.get("set_number"), int) or 0,
                    "group_id": (str(e["group_id"])[:40]
                                 if e.get("group_id") else None),
                    "group_mode": (e.get("group_mode")
                                   if e.get("group_mode") in ("entire", "each")
                                   else None),
                    "band": band,
                    "measure": measure,
                    "weight": (None if (band or measure == "bodyweight")
                               else num(e.get("weight"), float)),
                    "reps": num(e.get("reps"), int)})
            ended_early = bool(p.get("ended_early"))
            if ended_early and clean:
                # Ending during the timer of the last set is not ending
                # early: the saved entries already cover every step.
                # (Sarah's 11/09 + 11/11 workouts were flagged this way.)
                total_steps = counted_sets(sched["days"][day_idx])
                if max(e["step_index"] for e in clean) >= total_steps - 1:
                    ended_early = False
            wid = udb.insert_workout(sid, row["name"], day_idx, date_str,
                                     ended_early,
                                     sched["days"][day_idx], clean)
            return self._json(200, {"ok": True, "workout_id": wid})
        return self._json(404, {"ok": False})

    def do_PUT(self):
        path = self._strip_prefix(urllib.parse.urlparse(self.path).path)
        if path.startswith("/api/workout-sets/"):
            udb = self._udb()
            if not udb:
                return self._json(401, {"ok": False})
            try:
                set_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json(404, {"ok": False})
            p = self._json_body()
            if p is None:
                return self._json(400, {"ok": False})

            def num(v, cast):
                try:
                    return cast(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return "BAD"
            weight = num(p.get("weight"), float)
            reps = num(p.get("reps"), int)
            name = str(p.get("exercise_name", ""))[:100]
            if weight == "BAD" or reps == "BAD":
                return self._json(400, {"ok": False,
                                        "error": "weight/reps must be numbers"})
            if (weight is not None and weight < 0) or \
               (reps is not None and reps < 0):
                return self._json(400, {"ok": False,
                                        "error": "weight/reps can't be negative"})
            band = clean_band(p.get("band"))
            measure = p.get("measure")
            if measure not in ("weight", "band", "bodyweight"):
                measure = None
            if band:
                weight = None
                measure = "band"
            if measure == "bodyweight":
                weight = None
                band = None
            entry = udb.update_workout_set(set_id, name, weight, band, reps,
                                           measure)
            if entry is None:
                return self._json(404, {"ok": False})
            return self._json(200, {"ok": True, "entry": entry})
        if path.startswith("/api/schedules/"):
            udb = self._udb()
            if not udb:
                return self._json(401, {"ok": False})
            try:
                sid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json(404, {"ok": False})
            if not udb.schedule_row(sid):
                return self._json(404, {"ok": False})
            p = self._json_body()
            if p is None:
                return self._json(400, {"ok": False})
            clean, err = validate_schedule(p.get("schedule"))
            if err:
                return self._json(400, {"ok": False, "error": err})
            name = str(p.get("name") or "My Schedule")[:80]
            udb.update_schedule(sid, name, clean)
            return self._json(200, {"ok": True, "id": sid, "name": name,
                                    "schedule": clean,
                                    "linked_exercise_ids":
                                        udb.linked_exercise_ids(sid)})
        return self._json(404, {"ok": False})

    def do_DELETE(self):
        path = self._strip_prefix(urllib.parse.urlparse(self.path).path)
        if path.startswith("/api/schedules/"):
            udb = self._udb()
            if not udb:
                return self._json(401, {"ok": False})
            try:
                sid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json(404, {"ok": False})
            if not udb.schedule_row(sid):
                return self._json(404, {"ok": False})
            if udb.schedule_count() <= 1:
                return self._json(400, {"ok": False, "error":
                                        "can't delete the only schedule"})
            udb.delete_schedule(sid)
            return self._json(200, {"ok": True})
        return self._json(404, {"ok": False})


def main():
    port = int(config.get("port", 18794))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"lift-log listening on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
