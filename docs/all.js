let N = null, profMap = new Map(), pdMap = new Map(), byPattern = [], stopPatterns = [], stopNames = new Map(), partNames = new Map(), normNames = [], aliasMap = new Map(), tripTimeCache = new Map();

const $ = (id) => document.getElementById(id);
const sec = (s) => { const p = s.split(':').map(Number); return p[0] * 3600 + p[1] * 60 + (p[2] || 0); };
const fmt = (s) => { s = ((s % 86400) + 86400) % 86400; return String(Math.floor(s / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0'); };
const TOKENS = { FAMLIY: 'FAMILY', PLANING: 'PLANNING', SATION: 'STATION', STN: 'STATION', DD: 'DDA', SECTOR: 'SEC', BDR: 'BORDER', TERMINAL: 'T', XING: 'CROSSING', X: 'CROSSING', ING: 'CROSSING', OPP: 'OPPOSITE' };
const norm = (s) => {
  let v = String(s).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toUpperCase().replace(/&/g, ' / ');
  v = v.replace(/([A-Z])(\d)/g, '$1 $2').replace(/(\d)([A-Z])/g, '$1 $2');
  return v.replace(/[^A-Z0-9/]+/g, ' ').split(/\s+/).filter(Boolean).map(t => TOKENS[t] || t).join(' ').trim();
};

function boot(data) {
  N = data;
  N.patterns.forEach((stops, pid) => {
    for (const s of new Set(stops)) (stopPatterns[s] ||= []).push(pid);
  });
  byPattern = Array.from({ length: N.patterns.length }, () => []);
  N.trips.forEach((t, i) => byPattern[t[1]].push(i));
  N.stops.forEach((row, i) => {
    const k = norm(row[0]);
    if (!stopNames.has(k)) stopNames.set(k, []);
    stopNames.get(k).push(i);
    for (const part of row[0].split('/')) {
      const pk = norm(part);
      if (pk.length < 4) continue;
      if (!partNames.has(pk)) partNames.set(pk, []);
      if (!partNames.get(pk).includes(i)) partNames.get(pk).push(i);
    }
  });
  normNames = Array.from(stopNames.keys());
  for (const [raw, target] of Object.entries(N.aliases)) aliasMap.set(norm(raw), norm(target));
  N.profiles.forEach(p => profMap.set(p[0] + '|' + p[1] + '|' + p[2], p));
  N.pattern_day.forEach(p => pdMap.set(p[0] + '|' + p[1], p));
  const list = $('stoplist');
  list.innerHTML = N.stops.map(r => `<option value="${r[0].replace(/"/g, '&quot;')}"></option>`).join('');
  $('date').innerHTML = Array.from({ length: 31 }, (_, i) => `<option>2026-10-${String(i + 1).padStart(2, '0')}</option>`).join('');
  $('status').textContent = `${N.stops.length} stops · ${N.routes.length} routes · ${N.trips.length} trips loaded`;
  ['board', 'alight', 'date', 'time', 'win', 'sort'].forEach(id => $(id).onchange = render);
  $('go').onclick = render;
  render();
}

function similarity(a, b) {
  if (a === b) return 1;
  if (!a.length || !b.length) return 0;
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0]; prev[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const old = prev[j];
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diag + (a[i - 1] === b[j - 1] ? 0 : 1));
      diag = old;
    }
  }
  return 1 - prev[b.length] / Math.max(a.length, b.length);
}

function resolve(raw) {
  const k = norm(raw);
  if (aliasMap.has(k)) {
    const ids = stopNames.get(aliasMap.get(k)) || [];
    return { ids, label: ids.length ? 'verified alias' : 'unmatched', matched: ids.length ? N.stops[ids[0]][0] : '' };
  }
  let ids = stopNames.get(k) || [];
  if (ids.length) return { ids, label: 'exact/verified', matched: N.stops[ids[0]][0] };
  ids = partNames.get(k) || [];
  if (ids.length) return { ids, label: 'verified name part', matched: N.stops[ids[0]][0] };
  const tokens = k.split(' ').filter(t => t.length >= 3);
  let best = null, bestScore = 0, second = 0;
  for (const name of normNames) {
    if (tokens.length && !tokens.some(t => name.split(' ').includes(t))) continue;
    const score = similarity(k, name);
    if (score > bestScore) { second = bestScore; bestScore = score; best = name; }
    else if (score > second) second = score;
  }
  if (best && bestScore >= 0.86 && bestScore - second >= 0.03) {
    ids = stopNames.get(best) || [];
    return { ids, label: 'fuzzy typo', matched: N.stops[ids[0]][0] };
  }
  return { ids: [], label: 'unmatched', matched: '' };
}

function active(service, date) {
  const c = N.calendar[service] || N.calendar[0];
  const ymd = date.replace(/-/g, '');
  if (ymd < c[1] || ymd > c[2]) return false;
  const wd = (new Date(date + 'T00:00:00Z').getUTCDay() + 6) % 7;
  return !!c[0][wd];
}

function times(ti) {
  if (tripTimeCache.has(ti)) return tripTimeCache.get(ti);
  const t = N.trips[ti], deps = [t[3]];
  for (let i = 0; i < t[4].length; i++) deps.push(deps[i] + t[4][i]);
  const arrs = deps.map((d, i) => d + t[5][i]);
  const out = [deps, arrs];
  tripTimeCache.set(ti, out);
  return out;
}

function shrink(v, n, rid) {
  const k = N.meta.shrinkage_k, base = N.route_medians[rid] || 0;
  return (!k || !n || !base) ? v : (n * v + k * base) / (n + k);
}

function predict(pid, day, bin, rid) {
  let p = profMap.get(pid + '|' + day + '|' + bin);
  if (p && p[5] >= 3) return { load: shrink(p[3], p[5], rid), p75: p[4], support: p[5] };
  p = profMap.get(pid + '|' + day + '|' + Math.floor(bin / 2));
  if (p && p[5] >= 3) return { load: shrink(p[3], p[5], rid), p75: p[4], support: p[5] };
  const pd = pdMap.get(pid + '|' + day);
  if (pd) return { load: shrink(pd[2], pd[4], rid), p75: pd[3], support: pd[4] };
  return { load: N.route_medians[rid] || 0, p75: null, support: 0 };
}

function recordedClass(v) {
  const r = N.meta.reference;
  if (r.lower == null) return 'Limited data';
  return v < r.lower ? 'Lower' : v <= r.higher ? 'Typical' : 'Higher';
}

function searchAll(boardRaw, alightRaw, date) {
  const board = resolve(boardRaw), alight = resolve(alightRaw);
  const out = [];
  if (!board.ids.length || !alight.ids.length) return { options: out, board, alight };
  const boardSet = new Set(board.ids), alightSet = new Set(alight.ids);
  const candidates = new Set();
  for (const s of board.ids) for (const pid of (stopPatterns[s] || [])) for (const ti of byPattern[pid]) candidates.add(ti);
  const day = new Date(date + 'T00:00:00Z').getUTCDay();
  const weekend = day === 0 || day === 6 ? 1 : 0;
  for (const ti of candidates) {
    const t = N.trips[ti];
    if (!active(t[2], date)) continue;
    const pat = N.patterns[t[1]], [deps, arrs] = times(ti);
    const seen = new Set();
    for (let b = 0; b < pat.length; b++) {
      if (!boardSet.has(pat[b])) continue;
      let a = -1;
      for (let i = b + 1; i < pat.length; i++) if (alightSet.has(pat[i])) { a = i; break; }
      if (a < 0) continue;
      const key = b + '|' + a;
      if (seen.has(key)) continue;
      seen.add(key);
      const loads = [], secs = [], supports = [], classes = [];
      let peak = -1, peakPos = b;
      for (let p = b; p < a; p++) {
        const bin = Math.floor(deps[p] / (N.meta.bin_minutes * 60));
        const pr = predict(t[1], weekend, bin, t[0]);
        loads.push(pr.load); classes.push(recordedClass(pr.load)); supports.push(pr.support);
        const s = Math.max(1, arrs[p + 1] - deps[p]);
        secs.push(s);
        if (pr.load > peak) { peak = pr.load; peakPos = p; }
      }
      const total = secs.reduce((x, y) => x + y, 0) || 1;
      const avg = loads.reduce((x, y, i) => x + y * secs[i], 0) / total;
      const crowded = secs.reduce((x, s, i) => x + (loads[i] > (N.meta.reference.higher || Infinity) ? s : 0), 0) / 60;
      const support = Math.min(...supports);
      const route = N.routes[t[0]];
      const m = route.match(/(UP|DOWN)(\d*)$/);
      out.push({
        route, direction: m ? m[1] : 'unknown', trip: N.trips[ti][6] || ti,
        dep: deps[b], arr: arrs[a], load: avg, peak,
        peakSegment: N.stops[pat[peakPos]][0] + ' → ' + N.stops[pat[peakPos + 1]][0],
        crowded, support, confidence: support >= 5 ? 'high' : support >= 3 ? 'moderate' : 'Limited data',
        classes, evidence: 'model', b, a, pid: t[1], rid: t[0],
      });
    }
  }
  out.sort((x, y) => x.dep - y.dep || x.route.localeCompare(y.route));
  return { options: out, board, alight };
}

function render() {
  if (!N) return;
  const boardRaw = $('board').value, alightRaw = $('alight').value, date = $('date').value;
  const target = sec($('time').value), win = Number($('win').value) * 60;
  const result = searchAll(boardRaw, alightRaw, date);
  const inWin = result.options.filter(o => o.dep >= target - win && o.dep <= target + win);
  const later = result.options.filter(o => o.dep > target + win);
  const shown = inWin.length ? inWin : (later.length ? [later[0]] : []);
  const rows = $('sort').value === 'crowding'
    ? shown.slice().sort((a, b) => a.load - b.load || a.arr - b.arr)
    : shown.slice().sort((a, b) => a.arr - b.arr || a.dep - b.dep);
  const distinct = new Set(rows.map(o => o.route + '|' + o.trip)).size;
  const head = [];
  head.push(`<p>Window ${fmt(target - win)}–${fmt(target + win)} inclusive; ${distinct} distinct departures. ${result.options.length} direct scheduled options on ${date}.</p>`);
  const describe = (m) => `<strong>${m.label}</strong>${m.matched ? ' → ' + m.matched : ''}`;
  head.push(`<p>Stop matching: board ${describe(result.board)}, alight ${describe(result.alight)}.</p>`);
  const r = N.meta.reference;
  head.push(`<p class="note">Recorded-load bands (Oct 1–21): Lower ≤ ${r.lower}, Typical ≤ ${r.typical}, Higher > ${r.typical} e-ticket passengers per stretch; p90 ${r.higher} sets “crowded minutes”. These are not seats or total passengers.</p>`);
  if (!inWin.length && later.length) head.push(`<p class="warn">No departure in the window; showing the next later bus for context only.</p>`);
  if (!result.board.ids.length || !result.alight.ids.length) head.push(`<p class="warn">No matching stop. Use a name from the suggestion list.</p>`);
  let html = head.join('');
  if (rows.length) {
    html += '<table><tr><th>route</th><th>dir</th><th>dep</th><th>arr</th><th>wait min</th><th>recorded load</th><th>busiest stretch</th><th>crowded min</th><th>confidence</th><th>evidence</th><th>crowding</th></tr>';
    for (const o of rows) {
      html += `<tr><td>${o.route}</td><td>${o.direction}</td><td>${fmt(o.dep)}</td><td>${fmt(o.arr)}</td><td>${((o.dep - target) / 60).toFixed(1)}</td><td>${o.load.toFixed(1)}</td><td>${o.peakSegment} (${o.peak.toFixed(1)})</td><td>${o.crowded.toFixed(1)}</td><td>${o.confidence}</td><td>${o.evidence}</td><td>${o.classes.join(', ')}</td></tr>`;
    }
    html += '</table>';
  }
  html += '<p class="note">Browser-index demo: model-based recorded loads from the October training window; no live AVL, ticket coverage q, or fleet-seat mapping. Earlier departures are context only.</p>';
  html += '<p class="note"><a href="demo.html">Compact 3-pair demo</a> · <a href="https://github.com/deepanshu25169-ctrl/TRANSPORT_CROWING_PROBLEM">Source</a></p>';
  $('out').innerHTML = html;
}

fetch('network.json').then(r => r.json()).then(boot).catch(e => {
  $('status').textContent = 'Failed to load index: ' + e;
});
