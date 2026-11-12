# Lift Log

Mobile-first lifting tracker: design a split (days A/B/C…, exercises,
supersets), run each workout on an automatic rest timer, and compare
against the last time you did that day.

- **Google SSO** — same pattern (and, on mtayter.noip.me, the same OAuth
  client) as Math Quest: GSI popup → `id_token` verified server-side via
  Google `tokeninfo` → 30-day `lift_session` cookie.
- **Edit** — add/remove/reorder days and exercises (letters are just
  position). Link contiguous exercises into a superset and choose what
  1 set means for it: *entire superset* (one timer step and one counted
  set per round) or *each exercise* (interleaved, a timer step and a
  counted set per exercise). Per-day total = rest minutes × counted sets.
  Each schedule also has a Timer Yes/No switch (left of "Time between
  sets"): No hides the rest input and the per-day total time.
- **Multiple schedules** — each user can keep several named schedules.
  They are fully *independent*: separate day rotation, separate history.
  Edit updates a schedule in place; Copy duplicates it with fresh
  exercise IDs, so a copy starts with no history (the UI warns about
  this and suggests Edit instead when history matters). New makes a
  blank schedule; Delete removes one (never the last one; saved
  workouts stay in History).
- **History links** — every exercise has a stable ID, and workout sets
  record it. Last-time matching is by exercise ID + set number, so
  editing an exercise (rename / reorder / regroup / change sets) keeps
  its link; deleting the exercise loses it. Linked exercises show a 🔗
  badge in Edit; deleting one asks first, and editing one shows a
  warning banner explaining the rules.
- **Workout** — pick a schedule (defaults to the newest); today's day
  is picked by rotating from your last saved workout *on that
  schedule*. The timer auto-advances the highlight one set per rest
  period, beeping 3 times at zero. With the timer off, there is no
  countdown — the highlight moves on when the reps for the current step
  are entered. Left column = last time you did
  this day on this schedule; right column = today's weight/reps (last
  time's values prefilled in light color and saved unless typed over).
  End Early saves what's done and still advances the rotation — except
  ending during the last set, which counts as a complete workout.
  Bodyweight exercises show just BW + reps (no weight box); in History
  they read "BW × reps".
- **History** — every saved workout, newest first, tagged with its
  schedule's name.

## Data access

After authentication, every DB call goes through `UserDB`, a wrapper
bound to the logged-in user's ID — each method carries the `user_id`
filter (or, for `workout_sets`, an ownership join) inside it, so
handlers cannot touch another user's rows. The raw connection is used
only for startup/migration and the auth bootstrap (user creation,
session lookup by token). `test_local.py` asserts the HTTP handler
contains no raw `db.*` calls.

## Run

Python 3 stdlib only + SQLite. Put a `config.json` next to `server.py`:

```json
{"google_client_id": "...", "port": 18794}
```

The OAuth client must have `https://<your-hostname>` as an authorized
JavaScript origin. Then `python3 server.py`, fronted by Apache using
`deploy/lift.conf`, with `deploy/lift.service` as a systemd user unit.
