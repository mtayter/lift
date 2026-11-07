'use strict';
/* Lift Log SPA */
const $ = id => document.getElementById(id);
const API = '/lift/api';
let user = null, hasSchedule = false;
let schedule = null;          // editable copy in Edit view
let editDirty = false;
let currentView = 'workout';

/* ---------- helpers ---------- */
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2600);
}
function fmtDate(ymd) {
  if (!ymd) return '';
  const [y, m, d] = ymd.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined,
    { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
}
function todayYMD() {
  const n = new Date();
  const p = v => String(v).padStart(2, '0');
  return `${n.getFullYear()}-${p(n.getMonth() + 1)}-${p(n.getDate())}`;
}
function fmtMins(mins) {
  if (!isFinite(mins)) return '';
  return (Math.round(mins * 10) / 10).toString().replace(/\.0$/, '') + ' min';
}
function fmtClock(sec) {
  sec = Math.max(0, Math.ceil(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}
function dayLabel(i) { return i < 26 ? String.fromCharCode(65 + i) : '#' + (i + 1); }
function uid() { return 'x' + Math.random().toString(36).slice(2, 10); }
async function api(path, opts) {
  const r = await fetch(API + path, opts);
  return { status: r.status, body: await r.json().catch(() => ({})) };
}

/* ---------- superset / step logic (shared by Edit totals + Workout) ---------- */
// Group members: contiguous exercises with the same non-null group_id.
function groupRuns(day) {
  const runs = []; let cur = null;
  day.exercises.forEach((ex, i) => {
    if (ex.group_id && cur && cur.gid === ex.group_id) cur.idxs.push(i);
    else if (ex.group_id) { cur = { gid: ex.group_id, idxs: [i] }; runs.push(cur); }
    else cur = null;
  });
  return runs.filter(r => r.idxs.length >= 2);
}
function groupMode(day, gid) { return (day.groups && day.groups[gid] && day.groups[gid].mode) || 'each'; }
function groupOf(day, idx) {
  const gid = day.exercises[idx].group_id;
  if (!gid) return null;
  const run = groupRuns(day).find(r => r.gid === gid);
  return run ? { gid, run, mode: groupMode(day, gid) } : null;
}
// Build timer steps. Each step is one counted "set": {entries:[{name,set_number,group_id,group_mode}]}
// mode 'entire': one step holds every exercise in the round.
// mode 'each':   interleaved, one step per exercise occurrence.
function buildSteps(day) {
  const steps = []; let i = 0;
  while (i < day.exercises.length) {
    const ex = day.exercises[i];
    const g = groupOf(day, i);
    if (!g) {
      for (let s = 1; s <= ex.sets; s++)
        steps.push({ entries: [{ name: ex.name, set_number: s, group_id: null, group_mode: null }] });
      i++;
    } else if (g.mode === 'each') {
      const members = g.run.idxs.map(j => day.exercises[j]);
      const rounds = Math.max(...members.map(m => m.sets));
      for (let r = 0; r < rounds; r++)
        for (const m of members)
          if (r < m.sets)
            steps.push({ entries: [{ name: m.name, set_number: r + 1, group_id: g.gid, group_mode: 'each' }] });
      i = g.run.idxs[g.run.idxs.length - 1] + 1;
    } else { // entire
      const members = g.run.idxs.map(j => day.exercises[j]);
      const rounds = members[0].sets;
      for (let r = 0; r < rounds; r++)
        steps.push({ entries: members.map(m => ({ name: m.name, set_number: r + 1, group_id: g.gid, group_mode: 'entire' })) });
      i = g.run.idxs[g.run.idxs.length - 1] + 1;
    }
  }
  return steps;
}
function countedSets(day) { return buildSteps(day).length; }

/* ---------- auth ---------- */
async function initAuth() {
  const cfg = await api('/config');
  const me = await api('/me');
  if (me.body.signed_in) return enterApp(me.body);
  if (!cfg.body.google_client_id) { $('signin-error').textContent = 'Server not configured for sign-in yet.'; return; }
  const wait = setInterval(() => {
    if (!window.google || !google.accounts) return;
    clearInterval(wait);
    google.accounts.id.initialize({
      client_id: cfg.body.google_client_id,
      callback: async resp => {
        const r = await api('/auth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id_token: resp.credential }) });
        if (r.body.ok) enterApp(r.body);
        else $('signin-error').textContent = 'Sign-in failed. Please try again.';
      }
    });
    google.accounts.id.renderButton($('sso-box'), { theme: 'filled_black', size: 'large', shape: 'pill' });
  }, 150);
}
function enterApp(data) {
  user = data.user; hasSchedule = !!data.has_schedule;
  $('screen-signin').style.display = 'none';
  $('screen-app').style.display = 'block';
  $('user-name').textContent = (user.name || '').split(' ')[0];
  if (user.picture) { $('user-pic').src = user.picture; $('user-pic').style.display = 'block'; }
  updateNav();
  showView(hasSchedule ? 'workout' : 'edit');
}
async function logout() {
  stopTimer();
  await api('/logout', { method: 'POST' });
  location.reload();
}

/* ---------- navigation ---------- */
function updateNav() {
  for (const v of ['workout', 'edit', 'history'])
    $('nav-' + v).classList.toggle('active', currentView === v);
  $('nav-workout').classList.toggle('disabled', !hasSchedule);
}
async function showView(v) {
  if (v === currentView && v !== 'workout') { /* re-render anyway below for edit/history */ }
  if (v !== currentView) {
    if (wo.running) {
      if (!confirm('A workout timer is running. Leave and discard this workout?')) return;
      stopTimer(); wo = freshWorkoutState();
    }
    if (currentView === 'edit' && editDirty) {
      if (!confirm('You have unsaved schedule changes. Discard them?')) return;
      editDirty = false;
    }
  }
  currentView = v; updateNav();
  for (const name of ['workout', 'edit', 'history'])
    $('view-' + name).style.display = name === v ? 'block' : 'none';
  if (v === 'workout') loadWorkout();
  if (v === 'edit') loadEdit();
  if (v === 'history') loadHistory();
}
$('nav-workout').onclick = () => { if (!hasSchedule) { toast('Save a schedule in Edit first'); showView('edit'); } else showView('workout'); };
$('nav-edit').onclick = () => showView('edit');
$('nav-history').onclick = () => showView('history');
$('nav-logout').onclick = logout;

/* ---------- EDIT ---------- */
function defaultSchedule() {
  return { rest_minutes: 2, days: [{ exercises: [{ name: '', sets: 1, group_id: null }], groups: {} }] };
}
async function loadEdit() {
  if (!schedule) {
    const r = await api('/schedule');
    schedule = r.body.schedule || defaultSchedule();
  }
  renderEdit();
}
function markDirty() { editDirty = true; $('save-msg').textContent = ''; $('save-msg').className = ''; }

function renderEdit() {
  $('rest-input').value = schedule.rest_minutes;
  const c = $('days-container'); c.innerHTML = '';
  schedule.days.forEach((day, di) => {
    const card = document.createElement('div'); card.className = 'day-card';
    const total = countedSets(day);
    card.innerHTML = `
      <div class="day-head">
        <span class="day-title">Day ${dayLabel(di)}</span>
        <button class="iconbtn" data-act="day-up" ${di === 0 ? 'disabled' : ''} title="Move day up">↑</button>
        <button class="iconbtn" data-act="day-down" ${di === schedule.days.length - 1 ? 'disabled' : ''} title="Move day down">↓</button>
        <button class="iconbtn" data-act="day-del" ${schedule.days.length <= 1 ? 'disabled' : ''} title="Remove day">🗑</button>
        <span class="day-total" id="day-total-${di}">${total} sets<br>${fmtMins(schedule.rest_minutes * total)} total</span>
      </div>
      <div class="col-heads"><span></span><span></span><span>Exercise</span><span>Sets</span><span></span></div>
      <div class="ex-list"></div>
      <button class="btn add-ex" data-act="ex-add">+ Add Exercise</button>`;
    const list = card.querySelector('.ex-list');
    day.exercises.forEach((ex, ei) => {
      const g = groupOf(day, ei);
      if (g && g.run.idxs[0] === ei) list.appendChild(groupHeadEl(day, g));
      list.appendChild(exRowEl(day, ex, ei, g));
      if (ei < day.exercises.length - 1) list.appendChild(linkRowEl(day, ei));
    });
    card.querySelectorAll('[data-act]').forEach(b => b.onclick = () => editAction(b.dataset.act, di, card));
    c.appendChild(card);
  });
  refreshDayTotals();
}
function groupHeadEl(day, g) {
  const el = document.createElement('div'); el.className = 'group-head';
  el.innerHTML = `<span class="glabel">SUPERSET</span>
    <span class="muted">1 set =</span>
    <span class="seg">
      <button data-mode="entire" class="${g.mode === 'entire' ? 'on' : ''}">Entire superset</button>
      <button data-mode="each" class="${g.mode === 'each' ? 'on' : ''}">Each exercise</button>
    </span>`;
  el.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => {
    day.groups[g.gid] = { mode: b.dataset.mode };
    if (b.dataset.mode === 'entire') {
      const shared = day.exercises[g.run.idxs[0]].sets;
      g.run.idxs.forEach(j => day.exercises[j].sets = shared);
    }
    markDirty(); renderEdit();
  });
  return el;
}
function exRowEl(day, ex, ei, g) {
  const row = document.createElement('div');
  row.className = 'ex-row' + (g ? ' in-group' : '');
  const isFirstOfGroup = g && g.run.idxs[0] === ei;
  const sharedCell = g && g.mode === 'entire' && !isFirstOfGroup;
  row.innerHTML = `
    <button class="iconbtn" data-x="up" ${ei === 0 ? 'disabled' : ''} title="Move up">↑</button>
    <button class="iconbtn" data-x="down" ${ei === day.exercises.length - 1 ? 'disabled' : ''} title="Move down">↓</button>
    <input type="text" placeholder="Exercise name" value="${ex.name.replace(/"/g, '&quot;')}" autocomplete="off">
    ${sharedCell ? '<span class="shared">shared</span>' :
      `<input type="number" min="1" max="20" step="1" value="${ex.sets}" inputmode="numeric" title="Sets">`}
    <button class="iconbtn" data-x="del" ${day.exercises.length <= 1 ? 'disabled' : ''} title="Remove exercise">🗑</button>`;
  const [nameIn, setsIn] = [row.querySelector('input[type=text]'), row.querySelector('input[type=number]')];
  nameIn.oninput = () => { ex.name = nameIn.value; markDirty(); };
  if (setsIn) setsIn.oninput = () => {
    let v = parseInt(setsIn.value, 10); if (!(v >= 1)) v = 1; if (v > 20) v = 20;
    if (g && g.mode === 'entire') g.run.idxs.forEach(j => day.exercises[j].sets = v);
    else ex.sets = v;
    markDirty(); refreshDayTotals();
  };
  row.querySelector('[data-x=up]').onclick = () => moveExercise(day, ei, -1);
  row.querySelector('[data-x=down]').onclick = () => moveExercise(day, ei, 1);
  row.querySelector('[data-x=del]').onclick = () => {
    const gid = ex.group_id;
    day.exercises.splice(ei, 1);
    dissolveSmallGroups(day, gid ? [gid] : []);
    markDirty(); renderEdit();
  };
  return row;
}
function linkRowEl(day, ei) {
  const a = day.exercises[ei], b = day.exercises[ei + 1];
  const linked = a.group_id && a.group_id === b.group_id && groupOf(day, ei);
  const wrap = document.createElement('div'); wrap.className = 'link-row';
  const btn = document.createElement('button');
  btn.className = 'link-btn' + (linked ? ' linked' : '');
  btn.textContent = linked ? '🔗 Superset — tap to split here' : '🔗 Link as superset';
  btn.onclick = () => { linked ? splitGroupAt(day, ei) : mergeGroupsAt(day, ei); markDirty(); renderEdit(); };
  wrap.appendChild(btn);
  return wrap;
}
function dissolveSmallGroups(day, gids) {
  for (const gid of gids) {
    const members = day.exercises.filter(e => e.group_id === gid);
    if (members.length < 2) { members.forEach(e => e.group_id = null); delete day.groups[gid]; }
  }
}
function mergeGroupsAt(day, ei) {
  const a = day.exercises[ei], b = day.exercises[ei + 1];
  const gid = a.group_id || b.group_id || uid();
  const mode = (a.group_id && groupMode(day, a.group_id)) || (b.group_id && groupMode(day, b.group_id)) || 'each';
  const olds = [a.group_id, b.group_id].filter(Boolean);
  day.exercises.forEach(e => { if (olds.includes(e.group_id)) e.group_id = gid; });
  a.group_id = gid; b.group_id = gid;
  olds.forEach(o => { if (o !== gid) delete day.groups[o]; });
  day.groups[gid] = { mode };
  if (mode === 'entire') {
    const run = groupOf(day, ei).run; const shared = day.exercises[run.idxs[0]].sets;
    run.idxs.forEach(j => day.exercises[j].sets = shared);
  }
}
function splitGroupAt(day, ei) {
  const gid = day.exercises[ei].group_id; if (!gid) return;
  const mode = groupMode(day, gid);
  const below = uid();
  day.exercises.forEach((e, j) => { if (e.group_id === gid && j > ei) e.group_id = below; });
  day.groups[below] = { mode };
  dissolveSmallGroups(day, [gid, below]);
}
function moveExercise(day, ei, dir) {
  const j = ei + dir; if (j < 0 || j >= day.exercises.length) return;
  const a = day.exercises[ei], b = day.exercises[j];
  // Swapping across a superset boundary takes the moved exercise out of its
  // group, so groups always stay contiguous.
  if (a.group_id && a.group_id !== b.group_id) { const g = a.group_id; a.group_id = null; dissolveSmallGroups(day, [g]); }
  else if (b.group_id && a.group_id !== b.group_id) { const g = b.group_id; b.group_id = null; dissolveSmallGroups(day, [g]); }
  day.exercises[j] = a; day.exercises[ei] = b;
  markDirty(); renderEdit();
}
function editAction(act, di, card) {
  const day = schedule.days[di];
  if (act === 'day-up' && di > 0) { [schedule.days[di - 1], schedule.days[di]] = [schedule.days[di], schedule.days[di - 1]]; markDirty(); renderEdit(); }
  if (act === 'day-down' && di < schedule.days.length - 1) { [schedule.days[di + 1], schedule.days[di]] = [schedule.days[di], schedule.days[di + 1]]; markDirty(); renderEdit(); }
  if (act === 'day-del') { schedule.days.splice(di, 1); markDirty(); renderEdit(); }
  if (act === 'ex-add') { day.exercises.push({ name: '', sets: 1, group_id: null }); markDirty(); renderEdit(); }
}
$('btn-add-day').onclick = () => {
  schedule.days.push({ exercises: [{ name: '', sets: 1, group_id: null }], groups: {} });
  markDirty(); renderEdit();
};
$('rest-input').oninput = () => {
  let v = parseFloat($('rest-input').value);
  schedule.rest_minutes = (v > 0 && v <= 60) ? v : schedule.rest_minutes;
  markDirty(); refreshDayTotals();
};
function refreshDayTotals() {
  schedule.days.forEach((day, di) => {
    const el = $('day-total-' + di); if (!el) return;
    const total = countedSets(day);
    el.innerHTML = `${total} sets<br>${fmtMins(schedule.rest_minutes * total)} total`;
  });
}
$('btn-save-schedule').onclick = async () => {
  const v = parseFloat($('rest-input').value);
  if (!(v > 0 && v <= 60)) { $('save-msg').textContent = 'Time between sets must be between 0 and 60 minutes.'; $('save-msg').className = 'err'; return; }
  schedule.rest_minutes = v;
  const r = await api('/schedule', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schedule }) });
  if (r.body.ok) {
    schedule = r.body.schedule; editDirty = false; hasSchedule = true; updateNav();
    $('save-msg').textContent = 'Saved ✓'; $('save-msg').className = 'ok';
    toast('Schedule saved');
  } else {
    $('save-msg').textContent = 'Save failed: ' + (r.body.error || 'unknown error');
    $('save-msg').className = 'err';
  }
};

/* ---------- WORKOUT ---------- */
function freshWorkoutState() {
  return { running: false, steps: [], flat: [], current: 0, deadline: 0, timerId: null, restSec: 120, data: null, wakeLock: null };
}
let wo = freshWorkoutState();

async function loadWorkout() {
  stopTimer();
  wo = freshWorkoutState();
  const r = await api('/workout/today');
  const body = $('workout-body');
  if (!r.body.has_schedule) {
    $('workout-day-label').textContent = 'No schedule yet';
    $('workout-sub').textContent = '';
    body.innerHTML = '<div class="empty-note">Create and save a schedule in Edit first — then your first workout (Day A) will appear here.</div>';
    $('btn-start').disabled = true; return;
  }
  wo.data = r.body;
  wo.restSec = Math.round(r.body.rest_minutes * 60);
  wo.steps = buildSteps(r.body.day);
  // flatten with seq + step_index
  wo.flat = [];
  wo.steps.forEach((step, si) => step.entries.forEach(e =>
    wo.flat.push({ ...e, step_index: si, seq: wo.flat.length, weight: '', reps: '' })));
  $('workout-day-label').textContent = 'Day ' + r.body.day_label;
  $('workout-sub').textContent = `${wo.steps.length} sets • ${r.body.rest_minutes} min between sets`;
  $('timer-display').textContent = fmtClock(wo.restSec);
  $('timer-fill').style.width = '0%';
  $('btn-start').disabled = false; $('btn-end-early').disabled = true;
  renderWorkoutTable();
}
function lastEntryFor(seq) {
  const last = wo.data && wo.data.last_same_day;
  if (!last) return null;
  return last.entries.find(e => e.seq === seq) || null;
}
function renderWorkoutTable() {
  const body = $('workout-body');
  const last = wo.data.last_same_day;
  const lastHead = last ? fmtDate(last.date) : 'First time';
  let html = `<div class="wo-head"><span class="num">#</span><span>${lastHead}</span><span>Today — ${fmtDate(todayYMD())}</span></div>`;
  wo.steps.forEach((step, si) => {
    html += `<div class="step" id="step-${si}">`;
    wo.flat.filter(f => f.step_index === si).forEach(flat => {
      const e = flat;
      const le = lastEntryFor(flat.seq);
      const ssTag = e.group_id ? '<span class="ss-tag">SS</span>' : '';
      let left;
      if (last) {
        left = le
          ? `<div class="exname">${esc(le.exercise_name) || esc(e.name)}${ssTag}</div><div class="exvals">${le.weight != null ? le.weight : '—'} × ${le.reps != null ? le.reps : '—'}</div>`
          : `<div class="exname">${esc(e.name)}${ssTag}</div><div class="exvals">— × —</div>`;
      } else {
        left = `<div class="exname">${esc(e.name) || '(unnamed)'}${ssTag}</div><div class="settag">Set ${e.set_number}</div>`;
      }
      html += `<div class="wo-row">
        <span class="num">${flat.seq + 1}</span>
        <div class="wo-cell">${left}</div>
        <div class="inputs">
          <input type="number" inputmode="decimal" step="any" min="0" placeholder="${le && le.weight != null ? le.weight : 'wt'}" data-seq="${flat.seq}" data-field="weight" aria-label="Weight">
          <input type="number" inputmode="numeric" step="1" min="0" placeholder="${le && le.reps != null ? le.reps : 'reps'}" data-seq="${flat.seq}" data-field="reps" aria-label="Reps">
        </div></div>`;
    });
    html += '</div>';
  });
  body.innerHTML = html;
  body.querySelectorAll('input[data-seq]').forEach(inp => {
    inp.oninput = () => { wo.flat[Number(inp.dataset.seq)][inp.dataset.field] = inp.value; };
  });
  highlightStep();
}
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function highlightStep() {
  wo.steps.forEach((_, si) => {
    const el = $('step-' + si); if (!el) return;
    el.classList.toggle('current', wo.running && si === wo.current);
    el.classList.toggle('done', si < wo.current);
  });
  const cur = $('step-' + wo.current);
  if (wo.running && cur) cur.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

/* timer */
let audioCtx = null;
function beep3() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t0 = audioCtx.currentTime;
    for (let k = 0; k < 3; k++) {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = 'square'; o.frequency.value = 880;
      g.gain.setValueAtTime(0.6, t0 + k * 0.32);
      g.gain.setValueAtTime(0.6, t0 + k * 0.32 + 0.18);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + k * 0.32 + 0.2);
      o.connect(g); g.connect(audioCtx.destination);
      o.start(t0 + k * 0.32); o.stop(t0 + k * 0.32 + 0.22);
    }
  } catch (e) { /* audio unavailable */ }
  if (navigator.vibrate) navigator.vibrate([220, 110, 220, 110, 220]);
}
function startTimer() {
  if (!wo.steps.length) return;
  wo.running = true; wo.current = 0;
  try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); audioCtx.resume(); } catch (e) {}
  if (navigator.wakeLock) navigator.wakeLock.request('screen').then(w => wo.wakeLock = w).catch(() => {});
  $('btn-start').disabled = true; $('btn-end-early').disabled = false;
  wo.deadline = Date.now() + wo.restSec * 1000;
  clearInterval(wo.timerId);
  wo.timerId = setInterval(tick, 200);
  highlightStep(); tick();
}
function tick() {
  const left = (wo.deadline - Date.now()) / 1000;
  $('timer-display').textContent = fmtClock(Math.max(0, left));
  $('timer-fill').style.width = `${Math.min(100, Math.max(0, (1 - left / wo.restSec) * 100))}%`;
  if (left > 0) return;
  beep3();
  wo.current++;
  if (wo.current >= wo.steps.length) { finishWorkout(false); return; }
  wo.deadline = Date.now() + wo.restSec * 1000;
  highlightStep();
}
function stopTimer(releaseWake = true) {
  if (wo.timerId) clearInterval(wo.timerId);
  wo.timerId = null; wo.running = false;
  if (releaseWake && wo.wakeLock) { wo.wakeLock.release().catch(() => {}); wo.wakeLock = null; }
}
$('btn-start').onclick = startTimer;
$('btn-end-early').onclick = () => { if (wo.running) finishWorkout(true); };

/* save prompt */
let pendingEarly = false;
function finishWorkout(early) {
  stopTimer();
  pendingEarly = early;
  highlightStep();
  $('modal-title').textContent = early ? 'End workout early?' : 'Workout complete 🎉';
  const doneEntries = collectEntries(early).length;
  $('modal-text').textContent = early
    ? `Save the ${doneEntries} set(s) done so far? This counts as today's Day ${wo.data.day_label} — next time you'll move to the next day.`
    : `Save this Day ${wo.data.day_label} workout to your history?`;
  $('modal-resume').style.display = early ? 'block' : 'none';
  $('modal-backdrop').style.display = 'flex';
}
function collectEntries(early) {
  return wo.flat
    .filter(f => !early || f.step_index < wo.current || f.weight !== '' || f.reps !== '')
    .filter(f => !early || f.step_index <= wo.current)
    .map(f => ({
      step_index: f.step_index, exercise_name: f.name, set_number: f.set_number,
      group_id: f.group_id, group_mode: f.group_mode,
      weight: f.weight === '' ? null : Number(f.weight),
      reps: f.reps === '' ? null : Number(f.reps)
    }));
}
$('modal-resume').onclick = () => {
  $('modal-backdrop').style.display = 'none';
  // resume where we left off with a fresh timer for the current step
  wo.running = true;
  $('btn-start').disabled = true; $('btn-end-early').disabled = false;
  wo.deadline = Date.now() + wo.restSec * 1000;
  clearInterval(wo.timerId); wo.timerId = setInterval(tick, 200);
  highlightStep();
};
$('modal-discard').onclick = () => { $('modal-backdrop').style.display = 'none'; loadWorkout(); };
$('modal-save').onclick = async () => {
  const payload = { day_index: wo.data.day_index, date: todayYMD(), ended_early: pendingEarly, entries: collectEntries(pendingEarly) };
  const r = await api('/workouts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  $('modal-backdrop').style.display = 'none';
  if (r.body.ok) { toast('Workout saved ✓'); showView('history'); }
  else { toast('Save failed — please try again'); loadWorkout(); }
};

/* ---------- HISTORY ---------- */
async function loadHistory() {
  const r = await api('/history');
  const list = $('history-list');
  if (!r.body.workouts || !r.body.workouts.length) {
    list.innerHTML = '<div class="empty-note">No workouts yet. Your saved workouts will show up here, newest first.</div>';
    return;
  }
  list.innerHTML = '';
  for (const w of r.body.workouts) {
    const card = document.createElement('div'); card.className = 'hist-card';
    const rows = w.entries.map(e => `
      <div class="hrow"><span class="hn">${e.seq + 1}</span><span>${esc(e.exercise_name) || '(unnamed)'}${e.group_mode ? ' <span class="ss-tag">SS</span>' : ''}</span>
      <span class="hv">${e.weight != null ? e.weight : '—'} × ${e.reps != null ? e.reps : '—'}</span></div>`).join('');
    card.innerHTML = `
      <div class="hist-head"><span class="hist-day">Day ${w.day_label}</span>
      <span class="hist-date">${fmtDate(w.date)}</span>
      ${w.ended_early ? '<span class="badge">ENDED EARLY</span>' : ''}
      <span class="muted">${w.entries.length} sets</span></div>
      <div class="hist-sets">${rows || '<span class="muted">No sets recorded</span>'}</div>`;
    list.appendChild(card);
  }
}

/* ---------- boot ---------- */
initAuth();
