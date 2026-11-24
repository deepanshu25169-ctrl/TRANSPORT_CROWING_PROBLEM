let DATA = null;

const el = (id) => document.getElementById(id);
const sec = (s) => { const [h, m, x] = s.split(':').map(Number); return h * 3600 + m * 60 + (x || 0); };

function populate() {
  const pair = el('pair');
  pair.innerHTML = DATA.pairs.map(p => `<option value="${p.id}">${p.board} → ${p.alight}</option>`).join('');
  pair.onchange = fillDates;
  fillDates();
  ['date', 'time', 'win', 'sort'].forEach(id => el(id).onchange = render);
  el('go').onclick = render;
  render();
}

function fillDates() {
  const p = DATA.pairs.find(x => x.id === el('pair').value);
  el('date').innerHTML = Object.keys(p.dates).map(d => `<option>${d}</option>`).join('');
  render();
}

function render() {
  if (!DATA) return;
  const p = DATA.pairs.find(x => x.id === el('pair').value);
  const day = p.dates[el('date').value];
  const target = sec(el('time').value);
  const win = Number(el('win').value) * 60;
  const inWin = day.options.filter(o => o.dep_sec >= target - win && o.dep_sec <= target + win);
  const later = day.options.filter(o => o.dep_sec > target + win).sort((a, b) => a.dep_sec - b.dep_sec);
  const rows = inWin.length ? inWin : (later.length ? [later[0]] : []);
  const use = el('sort').value === 'crowding'
    ? rows.slice().sort((a, b) => (a.load == null) - (b.load == null) || a.load - b.load || a.arr_sec - b.arr_sec)
    : rows.slice().sort((a, b) => a.arr_sec - b.arr_sec || a.dep_sec - b.dep_sec);
  const distinct = new Set(use.map(o => o.route + '|' + o.trip)).size;
  const observed = use.filter(o => o.load != null);
  const avg = observed.length ? (observed.reduce((s, o) => s + o.load, 0) / observed.length).toFixed(1) : '—';
  const ref = DATA.reference || {};
  const bands = ref.lower == null ? '' : ` Recorded bands: Lower ≤${ref.lower}, Typical ≤${ref.typical}, Higher >${ref.typical}.`;
  const html = [];
  html.push(`<p>Window ${fmt(target - win)}–${fmt(target + win)} inclusive; ${distinct} distinct departures. ${day.options.length} direct options that day.${bands}</p>`);
  if (el('date').value > DATA.train_end) html.push(`<p class="note">Based on October 2026 ticket history.</p>`);
  html.push(`<p>Observed-only average of shown options: <strong>${avg}</strong> recorded passengers (${observed.length} with evidence / ${use.length} shown). No-data options are excluded, never treated as empty.</p>`);
  if (!inWin.length && later.length) html.push(`<p class="warn">No departure in the window; showing the next later bus for context only.</p>`);
  if (!rows.length) html.push(`<p class="warn">No direct scheduled option on this date.</p>`);
  if (rows.length) {
    html.push('<table><tr><th>route</th><th>dep</th><th>arr</th><th>platforms</th><th>wait min</th><th>recorded load</th><th>busiest stretch</th><th>high-demand min</th><th>confidence</th><th>evidence</th><th>recorded bands</th></tr>');
    for (const o of use) {
      const load = o.load == null ? 'No data' : o.load;
      const peak = (o.peak_segments || []).map(s => `${s.from}→${s.to} (${s.load}, n=${s.support}, ${s.n_days}d, ${s.method})`).join('; ') || 'No data';
      const crowded = o.crowded_min == null ? 'No data' : o.crowded_min;
      html.push(`<tr><td>${o.route}</td><td>${o.dep}</td><td>${o.arr}</td><td>${o.board_platform || '—'} → ${o.alight_platform || '—'}</td><td>${((o.dep_sec - target) / 60).toFixed(1)}</td><td>${load}</td><td>${peak}</td><td>${crowded}</td><td>${o.confidence}</td><td>${o.evidence}</td><td>${(o.crowding || []).join(', ')}</td></tr>`);
    }
    html.push('</table>');
  }
  html.push('<p class="note">Full stop-by-stop loads, pooling and arbitrary stop pairs are in the <a href="index.html">all-pairs app</a>.</p>');
  el('out').innerHTML = html.join('');
}

function fmt(s) {
  s = ((s % 86400) + 86400) % 86400;
  return String(Math.floor(s / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0');
}

fetch('data.json').then(r => r.json()).then(d => { DATA = d; populate(); });
