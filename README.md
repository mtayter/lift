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
- **Workout** — today's day is picked by rotating from your last saved
  workout. The timer auto-advances the highlight one set per rest period,
  beeping 3 times at zero. Left column = last time you did this day;
  right column = today's weight/reps (last time's values as placeholders).
  End Early saves what's done and still advances the rotation.
- **History** — every saved workout, newest first.

## Run

Python 3 stdlib only + SQLite. Put a `config.json` next to `server.py`:

```json
{"google_client_id": "...", "port": 18794}
```

The OAuth client must have `https://<your-hostname>` as an authorized
JavaScript origin. Then `python3 server.py`, fronted by Apache using
`deploy/lift.conf`, with `deploy/lift.service` as a systemd user unit.
