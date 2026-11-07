#!/usr/bin/env python3
"""Local smoke test: boots server.py on a temp DB/config, seeds a session
directly in SQLite (bypassing Google), and exercises the API end to end."""
import hashlib, json, os, secrets, sqlite3, subprocess, sys, time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 18799
BASE = f"http://127.0.0.1:{PORT}"

# temp config so server uses test port/db in a scratch copy
scratch = "/tmp/lift-test"
subprocess.run(["rm", "-rf", scratch])
os.makedirs(scratch + "/static")
for f in ["index.html", "app.js", "styles.css"]:
    subprocess.run(["cp", f"{ROOT}/static/{f}", scratch + "/static/"])
subprocess.run(["cp", f"{ROOT}/server.py", scratch])
with open(scratch + "/config.json", "w") as f:
    json.dump({"google_client_id": "test-client", "port": PORT}, f)

proc = subprocess.Popen([sys.executable, scratch + "/server.py"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/api/config", timeout=1); break
        except Exception:
            time.sleep(0.1)
    # seed user + session
    db = sqlite3.connect(scratch + "/lift.db")
    db.execute("INSERT INTO users(sub,email,name,picture,created_at)"
               " VALUES('s1','t@t','Tester','','2026-01-01')")
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at)"
               " VALUES(?,1,'2026-01-01','2999-01-01')", (th,))
    db.commit(); db.close()
    H = {"Cookie": f"lift_session={token}", "Content-Type": "application/json"}

    def call(method, path, body=None, headers=H):
        req = urllib.request.Request(BASE + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    fails = []
    def check(name, cond, extra=""):
        print(("PASS " if cond else "FAIL ") + name, extra)
        if not cond: fails.append(name)

    s, b = call("GET", "/api/me")
    check("me signed in", b.get("signed_in") and not b.get("has_schedule"))
    s, b = call("GET", "/api/workout/today")
    check("today: no schedule", b.get("has_schedule") is False)

    sched = {"rest_minutes": 2, "days": [
        {"exercises": [
            {"name": "Bench", "sets": 2, "group_id": None},
            {"name": "Curl", "sets": 3, "group_id": "g1"},
            {"name": "Extension", "sets": 3, "group_id": "g1"}],
         "groups": {"g1": {"mode": "entire"}}},
        {"exercises": [
            {"name": "Squat", "sets": 2, "group_id": None},
            {"name": "Plank", "sets": 2, "group_id": "g2"},
            {"name": "Crunch", "sets": 4, "group_id": "g2"}],
         "groups": {"g2": {"mode": "each"}}},
        {"exercises": [{"name": "Row", "sets": 1, "group_id": None}], "groups": {}}]}
    s, b = call("PUT", "/api/schedule", {"schedule": sched})
    check("put schedule", s == 200 and b.get("ok"), str(b)[:200])

    s, b = call("GET", "/api/workout/today")
    # Day A: Bench 2 + superset-entire counts 3 (rounds) = 5 counted sets
    check("today is A, counted 5", b["day_index"] == 0 and b["counted_sets"] == 5,
          f"got day={b.get('day_index')} counted={b.get('counted_sets')}")
    check("today A: no last_same_day", b["last_same_day"] is None)

    entries = [{"step_index": i, "exercise_name": n, "set_number": 1,
                "group_id": None, "group_mode": None, "weight": 100 + i, "reps": 8}
               for i, n in enumerate(["Bench", "Bench", "Curl", "Curl", "Curl"])]
    s, b = call("POST", "/api/workouts",
                {"day_index": 0, "date": "2026-11-07", "ended_early": False,
                 "entries": entries})
    check("save workout A", b.get("ok"))

    s, b = call("GET", "/api/workout/today")
    # rotation: A done -> today B; B: Squat 2 + each-mode 2+4=6 -> 8 counted
    check("rotation to B, counted 8", b["day_index"] == 1 and b["counted_sets"] == 8,
          f"got day={b.get('day_index')} counted={b.get('counted_sets')}")
    check("B: no last_same_day yet", b["last_same_day"] is None)

    s, b = call("POST", "/api/workouts",
                {"day_index": 1, "date": "2026-11-08", "ended_early": True,
                 "entries": entries[:2]})
    check("save early workout B", b.get("ok"))
    s, b = call("GET", "/api/workout/today")
    check("rotation to C after early B", b["day_index"] == 2)
    s, b = call("POST", "/api/workouts",
                {"day_index": 2, "date": "2026-11-09", "ended_early": False,
                 "entries": entries[:1]})
    s, b = call("GET", "/api/workout/today")
    check("wraps C -> A", b["day_index"] == 0)
    check("A last_same_day has weights", b["last_same_day"] is not None
          and b["last_same_day"]["entries"][0]["weight"] == 100
          and b["last_same_day"]["date"] == "2026-11-07")

    s, b = call("GET", "/api/history")
    check("history newest first", [w["day_label"] for w in b["workouts"]] == ["C", "B", "A"],
          str([w["day_label"] for w in b["workouts"]]))
    check("history B ended_early", b["workouts"][1]["ended_early"] is True)

    s, b = call("GET", "/api/schedule", headers={"Content-Type": "application/json"})
    check("schedule requires auth", s == 401)
    with urllib.request.urlopen(BASE + "/") as r:
        html = r.read().decode()
    check("index served", r.status == 200 and "Lift Log" in html and "app.js" in html)
    with urllib.request.urlopen(BASE + "/lift/static/app.js") as r:
        js = r.read().decode()
    check("app.js served under /lift prefix", r.status == 200 and "buildSteps" in js)

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    sys.exit(1 if fails else 0)
finally:
    proc.terminate()
