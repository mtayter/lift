#!/usr/bin/env python3
"""Local smoke test v2: boots server.py on a scratch DB, seeds a session
directly in SQLite (bypassing Google), exercises the API end to end:
multiple schedules, independence, copy semantics, exercise-ID links,
rotation, supersets, history, plus the v1->v2 schema migration."""
import hashlib, json, os, secrets, sqlite3, subprocess, sys, time
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 18799
BASE = f"http://127.0.0.1:{PORT}"
fails = []

def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)

def boot(scratch):
    subprocess.run(["rm", "-rf", scratch])
    os.makedirs(scratch + "/static")
    for f in ["index.html", "app.js", "styles.css"]:
        subprocess.run(["cp", f"{ROOT}/static/{f}", scratch + "/static/"])
    subprocess.run(["cp", f"{ROOT}/server.py", scratch])
    with open(scratch + "/config.json", "w") as f:
        json.dump({"google_client_id": "test-client", "port": PORT}, f)
    proc = subprocess.Popen([sys.executable, scratch + "/server.py"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/config", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return proc

def seed_session(scratch):
    db = sqlite3.connect(scratch + "/lift.db")
    db.execute("INSERT OR IGNORE INTO users(sub,email,name,picture,created_at)"
               " VALUES('s1','t@t','Tester','','2026-01-01')")
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at)"
               " VALUES(?,1,'2026-01-01','2999-01-01')", (th,))
    db.commit(); db.close()
    return {"Cookie": f"lift_session={token}", "Content-Type": "application/json"}

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# ============ part 0: structural guarantee — Handler has no raw DB access ====
_src = open(os.path.join(ROOT, "server.py")).read()
_handler_src = _src[_src.index("class Handler"):]
check("Handler uses no raw db.* calls (UserDB only)",
      all(tok not in _handler_src for tok in
          ("db.execute", "db.executemany", "db.executescript", "db.commit")),
      "")
check("UserDB wrapper exists and is uid-bound",
      "class UserDB" in _src and "def __init__(self, uid)" in _src)

# ============ part 1: fresh v2 flow ============
scratch = "/tmp/lift-test2"
proc = boot(scratch)
try:
    H = seed_session(scratch)
    s, b = call("GET", "/api/me", headers=H)
    check("me: no schedule yet", b.get("signed_in") and not b.get("has_schedule"))

    sched = {"rest_minutes": 2, "days": [
        {"exercises": [
            {"id": "bench1", "name": "Bench", "sets": 2, "group_id": None},
            {"id": "curl1", "name": "Curl", "sets": 3, "group_id": "g1"},
            {"id": "ext1", "name": "Extension", "sets": 3, "group_id": "g1"}],
         "groups": {"g1": {"mode": "entire"}}},
        {"exercises": [{"id": "row1", "name": "Row", "sets": 1, "group_id": None}],
         "groups": {}}]}
    s, b = call("POST", "/api/schedules", {"name": "Main", "schedule": sched}, H)
    check("create schedule Main", b.get("ok") and b.get("id"))
    main_id = b["id"]

    s, b = call("GET", "/api/workout/today", headers=H)
    check("today: Main day A counted 5 (entire superset)",
          b["schedule_id"] == main_id and b["day_index"] == 0
          and b["counted_sets"] == 5, f"counted={b.get('counted_sets')}")

    entries = [{"step_index": i, "exercise_id": eid, "exercise_name": n,
                "set_number": sn, "group_id": None, "group_mode": None,
                "weight": 100 + i, "reps": 8}
               for i, (eid, n, sn) in enumerate(
                   [("bench1", "Bench", 1), ("bench1", "Bench", 2),
                    ("curl1", "Curl", 1), ("curl1", "Curl", 2),
                    ("curl1", "Curl", 3)])]
    s, b = call("POST", "/api/workouts",
                {"schedule_id": main_id, "day_index": 0, "date": "2026-11-07",
                 "ended_early": False, "entries": entries}, H)
    check("save workout under Main", b.get("ok"))

    s, b = call("GET", f"/api/schedules/{main_id}", headers=H)
    check("linked ids include bench1+curl1",
          set(b["linked_exercise_ids"]) == {"bench1", "curl1"},
          str(b.get("linked_exercise_ids")))

    s, b = call("GET", "/api/workout/today", headers=H)
    check("Main rotates A->B", b["day_index"] == 1 and b["schedule_id"] == main_id)

    # --- copy: fresh IDs, fresh history ---
    s, b = call("POST", f"/api/schedules/{main_id}/copy", None, H)
    check("copy created", b.get("ok") and b.get("id") != main_id)
    copy_id = b["id"]
    copy_ids = {e["id"] for d in b["schedule"]["days"] for e in d["exercises"]}
    check("copy exercise IDs all fresh",
          not (copy_ids & {"bench1", "curl1", "ext1", "row1"}), str(copy_ids))
    check("copy name", b["name"] == "Main (copy)")

    # newest is the copy (just created) -> default today is the copy, day A, no last
    s, b = call("GET", "/api/workout/today", headers=H)
    check("default today = newest (copy), independent day A, no last_same",
          b["schedule_id"] == copy_id and b["day_index"] == 0
          and b["last_same_day"] is None)
    # explicit older schedule keeps its own state
    s, b = call("GET", f"/api/workout/today?schedule_id={main_id}", headers=H)
    check("Main state untouched by copy", b["day_index"] == 1)

    # do day B on Main, then day A again -> last_same matched by exercise ID
    s, b = call("POST", "/api/workouts",
                {"schedule_id": main_id, "day_index": 1, "date": "2026-11-08",
                 "ended_early": False,
                 "entries": [{"step_index": 0, "exercise_id": "row1",
                              "exercise_name": "Row", "set_number": 1,
                              "group_id": None, "group_mode": None,
                              "weight": 90, "reps": 10}]}, H)
    s, b = call("GET", f"/api/workout/today?schedule_id={main_id}", headers=H)
    check("Main wraps B->A with last_same from first A",
          b["day_index"] == 0 and b["last_same_day"] is not None
          and b["last_same_day"]["entries"][0]["exercise_id"] == "bench1"
          and b["last_same_day"]["entries"][0]["weight"] == 100)

    # --- edit in place: rename bench1 + move it; ID (and link) survives ---
    edited = {"rest_minutes": 2, "days": [
        {"exercises": [
            {"id": "curl1", "name": "Curl", "sets": 3, "group_id": "g1"},
            {"id": "ext1", "name": "Extension", "sets": 3, "group_id": "g1"},
            {"id": "bench1", "name": "Bench Press", "sets": 2, "group_id": None}],
         "groups": {"g1": {"mode": "entire"}}},
        sched["days"][1]]}
    s, b = call("PUT", f"/api/schedules/{main_id}",
                {"name": "Main", "schedule": edited}, H)
    check("edit in place keeps ids + linked ids",
          b.get("ok") and set(b["linked_exercise_ids"]) == {"bench1", "curl1", "row1"})
    names = [e["name"] for e in b["schedule"]["days"][0]["exercises"]]
    check("rename + reorder applied", names == ["Curl", "Extension", "Bench Press"],
          str(names))

    # --- history across schedules, newest first, with schedule names ---
    s, b = call("GET", "/api/history", headers=H)
    check("history order + names",
          [(w["schedule_name"], w["day_label"]) for w in b["workouts"]]
          == [("Main", "B"), ("Main", "A")],
          str([(w["schedule_name"], w["day_label"]) for w in b["workouts"]]))

    # --- history editing: change a saved set, incl. cross-user refusal ---
    s, b = call("GET", "/api/history", headers=H)
    a_workout = [w for w in b["workouts"] if w["day_label"] == "A"][0]
    set0 = a_workout["entries"][0]
    check("history entries expose set id", isinstance(set0.get("id"), int))
    s, b = call("PUT", f"/api/workout-sets/{set0['id']}",
                {"exercise_name": "Bench Press", "weight": 105.5, "reps": 6}, H)
    check("edit history set", b.get("ok") and b["entry"]["weight"] == 105.5
          and b["entry"]["reps"] == 6
          and b["entry"]["exercise_name"] == "Bench Press", str(b)[:160])
    s, b = call("PUT", f"/api/workout-sets/{set0['id']}",
                {"exercise_name": "Bench Press", "weight": -5, "reps": 6}, H)
    check("negative weight refused", s == 400)
    s, b = call("PUT", f"/api/workout-sets/{set0['id']}",
                {"exercise_name": "X", "weight": 1, "reps": 1},
                {"Content-Type": "application/json"})
    check("edit history requires auth", s == 401)
    db = sqlite3.connect(scratch + "/lift.db")
    db.execute("INSERT INTO users(sub,email,name,picture,created_at)"
               " VALUES('s2','u2@t','Other','','2026-01-01')")
    tok2 = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at)"
               " VALUES(?,2,'2026-01-01','2999-01-01')",
               (hashlib.sha256(tok2.encode()).hexdigest(),))
    db.commit(); db.close()
    H2 = {"Cookie": f"lift_session={tok2}", "Content-Type": "application/json"}
    s, b = call("PUT", f"/api/workout-sets/{set0['id']}",
                {"exercise_name": "Hacked", "weight": 1, "reps": 1}, H2)
    check("other user's set not editable (404)", s == 404)
    s, b = call("GET", "/api/history", headers=H)
    a_workout = [w for w in b["workouts"] if w["day_label"] == "A"][0]
    check("edited values persist in history",
          a_workout["entries"][0]["weight"] == 105.5
          and a_workout["entries"][0]["exercise_name"] == "Bench Press")

    # --- band / bodyweight measures ---
    s, b = call("POST", "/api/schedules",
                {"name": "BandTest", "schedule": {
                    "rest_minutes": 2, "days": [{"exercises": [
                        {"name": "Band Row", "sets": 2, "measure": "band"},
                        {"name": "Push-up", "sets": 2,
                         "measure": "bodyweight"},
                        {"name": "Curl", "sets": 1}], "groups": {}}]}}, H)
    check("band schedule created", s == 200 and b.get("ok"), str(b)[:120])
    band_sid = b["id"]
    ms = [e["measure"] for e in b["schedule"]["days"][0]["exercises"]]
    check("measures stored + weight default",
          ms == ["band", "bodyweight", "weight"], str(ms))
    ids = {e["name"]: e["id"] for e in b["schedule"]["days"][0]["exercises"]}
    s, b = call("POST", "/api/workouts",
                {"schedule_id": band_sid, "day_index": 0,
                 "date": "2026-11-07", "ended_early": False, "entries": [
                     {"step_index": 0, "exercise_id": ids["Band Row"],
                      "exercise_name": "Band Row", "set_number": 1,
                      "band": "H+M", "weight": 999, "reps": 15},
                     {"step_index": 1, "exercise_id": ids["Band Row"],
                      "exercise_name": "Band Row", "set_number": 2,
                      "band": "h", "weight": None, "reps": 12},
                     {"step_index": 2, "exercise_id": ids["Push-up"],
                      "exercise_name": "Push-up", "set_number": 1,
                      "band": None, "weight": None, "reps": 20},
                     {"step_index": 3, "exercise_id": ids["Curl"],
                      "exercise_name": "Curl", "set_number": 1,
                      "band": "X", "weight": 20, "reps": 10}]}, H)
    check("band workout saved", b.get("ok"), str(b)[:120])
    s, b = call("GET", f"/api/workout/today?schedule_id={band_sid}",
                headers=H)
    ent = b["last_same_day"]["entries"]
    check("band canonicalized H+M -> M+H, weight dropped",
          ent[0]["band"] == "M+H" and ent[0]["weight"] is None, str(ent[0]))
    check("band case-normalized", ent[1]["band"] == "H")
    check("bodyweight entry has no weight/band",
          ent[2]["weight"] is None and ent[2]["band"] is None)
    check("invalid band cleared, weight kept",
          ent[3]["band"] is None and ent[3]["weight"] == 20)
    s, b = call("PUT", f"/api/workout-sets/{ent[0]['id']}",
                {"exercise_name": "Band Row", "weight": None, "band": "L",
                 "reps": 15}, H)
    check("history edit band", b.get("ok") and b["entry"]["band"] == "L"
          and b["entry"]["weight"] is None, str(b)[:140])
    s, b = call("DELETE", f"/api/schedules/{band_sid}", headers=H)
    check("band test schedule deleted", b.get("ok"))

    # --- delete rules ---
    s, b = call("DELETE", f"/api/schedules/{copy_id}", headers=H)
    check("delete copy ok", b.get("ok"))
    s, b = call("DELETE", f"/api/schedules/{main_id}", headers=H)
    check("delete last schedule refused", s == 400)
    s, b = call("GET", "/api/schedules", headers={"Content-Type": "application/json"})
    check("list requires auth", s == 401)
finally:
    proc.terminate()
    proc.wait(timeout=5)
    for _ in range(50):  # wait for the port to free up
        try:
            urllib.request.urlopen(BASE + "/api/config", timeout=1)
            time.sleep(0.1)
        except Exception:
            break

# ============ part 2: v1 -> v2 migration ============
scratch = "/tmp/lift-test2-mig"
subprocess.run(["rm", "-rf", scratch]); os.makedirs(scratch)
db = sqlite3.connect(scratch + "/lift.db")
db.executescript("""
CREATE TABLE users(id INTEGER PRIMARY KEY, sub TEXT UNIQUE, email TEXT,
  name TEXT, picture TEXT, created_at TEXT);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER,
  created_at TEXT, expires_at TEXT);
CREATE TABLE schedules(user_id INTEGER PRIMARY KEY, data TEXT, updated_at TEXT);
CREATE TABLE workouts(id INTEGER PRIMARY KEY, user_id INTEGER, day_index INTEGER,
  day_label TEXT, date TEXT, ended_early INTEGER DEFAULT 0,
  schedule_snapshot TEXT, created_at TEXT);
CREATE TABLE workout_sets(id INTEGER PRIMARY KEY, workout_id INTEGER, seq INTEGER,
  step_index INTEGER, exercise_name TEXT, set_number INTEGER, group_id TEXT,
  group_mode TEXT, weight REAL, reps INTEGER);
INSERT INTO users VALUES(1,'s1','t@t','Tester','','2026-01-01');
INSERT INTO schedules VALUES(1, '{"rest_minutes": 3, "days": [{"exercises":
  [{"name": "Squat", "sets": 2, "group_id": null}], "groups": {}}]}',
  '2026-11-01T00:00:00');
INSERT INTO workouts VALUES(1,1,0,'A','2026-11-02',0,'{}','2026-11-02');
""")
db.commit(); db.close()
# boot() wipes the dir, so boot manually here instead:
os.makedirs(scratch + "/static", exist_ok=True)
for f in ["index.html", "app.js", "styles.css"]:
    subprocess.run(["cp", f"{ROOT}/static/{f}", scratch + "/static/"])
subprocess.run(["cp", f"{ROOT}/server.py", scratch])
with open(scratch + "/config.json", "w") as f:
    json.dump({"google_client_id": "test-client", "port": PORT}, f)
proc = subprocess.Popen([sys.executable, scratch + "/server.py"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/config", timeout=1); break
        except Exception:
            time.sleep(0.1)
    H = seed_session(scratch)
    s, b = call("GET", "/api/schedules", headers=H)
    check("migration: v1 schedule listed as 'My Schedule'",
          len(b["schedules"]) == 1 and b["schedules"][0]["name"] == "My Schedule",
          str(b.get("schedules")))
    mig_id = b["schedules"][0]["id"]
    s, b = call("GET", "/api/history", headers=H)
    check("migration: old workout got schedule_id",
          b["workouts"] and b["workouts"][0]["schedule_id"] == mig_id)
    s, b = call("GET", "/api/workout/today", headers=H)
    check("migration: today works, exercise got an ID assigned",
          b["schedule_id"] == mig_id and b["day"]["exercises"][0].get("id"))
finally:
    proc.terminate()

print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
