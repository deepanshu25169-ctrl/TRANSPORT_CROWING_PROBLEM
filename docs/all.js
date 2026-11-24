let N = null, profMap = new Map(), dayMap = new Map(), byPattern = [], stopPatterns = [], stopNames = new Map(), partNames = new Map(), normNames = [], aliasMap = new Map(), tripTimeCache = new Map();

const $ = (id) => document.getElementById(id);
const sec = (s) => { const p = s.split(':').map(Number); return p[0] * 3600 + p[1] * 60 + (p[2] || 0); };
const fmt = (s) => { s = ((s % 86400) + 86400) % 86400; return String(Math.floor(s / 3600)).padStart(2, '0') + ':' + String(Math.floor(s % 3600 / 60)).padStart(2, '0'); };
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
  N.profiles.forEach(p => profMap.set(p[0] + '|' + p[1] + '|' + p[2] + '|' + p[3], p));
  N.stretch_day.forEach(p => dayMap.set(p[0] + '|' + p[1] + '|' + p[2], p));
  const list = $('stoplist');
  list.innerHTML = N.stops.map(r => `<option value="${esc(r[0])}"></option>`).join('');
  $('status').textContent = `${N.stops.length} stops · ${N.routes.length} routes · ${N.trips.length} trips loaded · ${N.meta.data_vintage}`;
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
    return { ids, label: ids.length ? 'verified alias' : 'unmatched' };
  }
  let ids = stopNames.get(k) || [];
  if (ids.length) return { ids, label: 'exact/verified' };
  ids = partNames.get(k) || [];
  if (ids.length) return { ids, label: 'verified name part' };
  const tokens = k.split(' ').filter(t => t.length >= 3);
  let best = null, bestScore = 0, second = 0;
  for (const name of normNames) {
    if (tokens.length && !tokens.some(t => name.split(' ').includes(t))) continue;
    const score = similarity(k, name);
    if (score > bestScore) { second = bestScore; bestScore = score; best = name; }
    else if (score > second) second = score;
  }
  if (best && bestScore >= 0.86 && bestScore - second >= 0.03) {
    return { ids: stopNames.get(best) || [], label: 'fuzzy typo' };
  }
  return { ids: [], label: 'unmatched' };
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
  const k = N.meta.shrinkage_k || 0, base = N.route_medians[rid] || 0;
  return (!k || !n || !base) ? v : (n * v + k * base) / (n + k);
}

function weighted(items, pct) {
  const total = items.reduce((s, x) => s + x[1], 0);
  if (!total) return null;
  const target = total * pct / 100;
  let cumulative = 0;
  const sorted = items.slice().sort((a, b) => a[0] - b[0]);
  for (const [value, count] of sorted) {
    cumulative += count;
    if (cumulative >= target) return value;
  }
  return sorted[sorted.length - 1][0];
}

function statusFor(n, days) {
  if (n >= 5 && days >= 2) return 'high';
  if (n >= N.meta.min_support) return 'supported';
  return 'Limited data';
}

function predict(pid, day, pos, scheduledSec, rid) {
  const binMinutes = N.meta.bin_minutes;
  const bins = Math.max(1, Math.round(86400 / (binMinutes * 60)));
  const local = ((scheduledSec % 86400) + 86400) % 86400;
  const bin = Math.floor(local / (binMinutes * 60));
  const exact = profMap.get(pid + '|' + day + '|' + bin + '|' + pos);
  if (exact && exact[6] >= N.meta.min_support) {
    return { load: shrink(exact[4], exact[6], rid), p75: exact[5], n: exact[6], days: exact[7],
             method: 'exact_stretch', window: binMinutes, status: statusFor(exact[6], exact[7]), evidence: true };
  }
  for (let radius = 0; radius <= N.meta.max_stretch_radius; radius++) {
    const medianItems = [], upperItems = [];
    let days = 0;
    for (let delta = -radius; delta <= radius; delta++) {
      const p = profMap.get(pid + '|' + day + '|' + ((bin + delta) % bins + bins) % bins + '|' + pos);
      if (!p || p[6] < N.meta.min_support) continue;
      medianItems.push([p[4], Math.max(1, p[6])]);
      upperItems.push([p[5], Math.max(1, p[6])]);
      days += p[7];
    }
    const n = medianItems.reduce((s, x) => s + x[1], 0);
    if (n >= N.meta.min_support) {
      return { load: shrink(weighted(medianItems, 50), n, rid), p75: weighted(upperItems, 50),
               n, days, method: 'nearby_stretch', window: (2 * radius + 1) * binMinutes,
               status: statusFor(n, days), evidence: true };
    }
  }
  const pd = dayMap.get(pid + '|' + day + '|' + pos);
  if (pd) {
    const n = pd[5], days = pd[6];
    return { load: shrink(pd[3], n, rid), p75: pd[4], n, days, method: 'same_stretch_all_day',
             window: 1440, status: statusFor(n, days), evidence: n >= N.meta.min_support };
  }
  if (local < 3600 || local >= 82800) {
    const other = day === 1 ? 0 : 1;
    const medianItems = [], upperItems = [];
    let days = 0;
    for (const delta of [-1, 0, 1]) {
      const p = profMap.get(pid + '|' + other + '|' + ((bin + delta) % bins + bins) % bins + '|' + pos);
      if (!p || p[6] < N.meta.min_support) continue;
      medianItems.push([p[4], Math.max(1, p[6])]);
      upperItems.push([p[5], Math.max(1, p[6])]);
      days += p[7];
    }
    const n = medianItems.reduce((s, x) => s + x[1], 0);
    if (n) {
      return { load: shrink(weighted(medianItems, 50), n, rid), p75: weighted(upperItems, 50),
               n, days, method: 'adjacent_day_type_boundary', window: 3 * binMinutes,
               status: statusFor(n, days), evidence: n >= N.meta.min_support };
    }
  }
  const base = N.route_medians[rid] || null;
  return { load: base, p75: base, n: 0, days: 0, method: 'route_only_no_stretch_evidence',
           window: null, status: 'Limited data', evidence: false };
}

function recordedClass(v) {
  const r = N.meta.reference;
  if (v == null || r.lower == null) return 'No data';
  return v <= r.lower ? 'Lower' : v <= r.typical ? 'Typical' : 'Higher';
}

function stopLabel(i) {
  return N.stops[i][0] + ' [' + N.stops[i][1] + ']';
}

function searchAll(boardRaw, alightRaw, date) {
  const board = resolve(boardRaw), alight = resolve(alightRaw);
  const out = [];
  if (!board.ids.length || !alight.ids.length) return { options: out, board, alight };
  const boardSet = new Set(board.ids), alightSet = new Set(alight.ids);
  const candidates = new Set();
  for (const s of board.ids) for (const pid of (stopPatterns[s] || [])) for (const ti of byPattern[pid]) candidates.add(ti);
  const wd = (new Date(date + 'T00:00:00Z').getUTCDay() + 6) % 7;
  const weekend = wd >= 5 ? 1 : 0;
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
      const segments = [], loads = [], classes = [], methods = [];
      let elapsed = 0, support = Infinity, days = Infinity;
      for (let p = b; p < a; p++) {
        const pr = predict(t[1], weekend, p, deps[b] + elapsed, t[0]);
        const seconds = Math.max(1, arrs[p + 1] - deps[p]);
        elapsed += seconds;
        const use = pr.evidence;
        const load = use ? pr.load : null;
        segments.push({ from: pat[p], to: pat[p + 1], seconds, load, p75: pr.p75,
                        n: pr.n, days: pr.days, method: pr.method, status: pr.status,
                        window: pr.window, evidence: use });
        if (use) {
          loads.push({ pos: p, load, pr, seconds });
          classes.push(recordedClass(load));
          methods.push(pr.method);
        } else {
          classes.push('No data');
        }
        support = Math.min(support, pr.n);
        days = Math.min(days, pr.days);
      }
      const weight = loads.reduce((s, x) => s + x.seconds, 0);
      const avg = weight ? loads.reduce((s, x) => s + x.load * x.seconds, 0) / weight : null;
      const peak = loads.length ? Math.max(...loads.map(x => x.load)) : null;
      const peakItems = loads.filter(x => Math.abs(x.load - peak) < 1e-9);
      const peakSegments = peakItems.map(x => ({
        from: N.stops[pat[x.pos]][0], to: N.stops[pat[x.pos + 1]][0], load: x.load,
        platform_from: N.stops[pat[x.pos]][1], platform_to: N.stops[pat[x.pos + 1]][1],
        support: x.pr.n, days: x.pr.days
      }));
      const threshold = N.meta.reference.higher;
      const crowded = loads.length && threshold != null
        ? loads.reduce((s, x) => s + (x.load > threshold ? x.seconds : 0), 0) / 60 : null;
      const confidence = !loads.length ? 'No data'
        : loads.every(x => x.pr.status === 'high') ? 'high'
        : loads.every(x => x.pr.status === 'high' || x.pr.status === 'supported') ? 'moderate'
        : 'Limited data';
      const route = N.routes[t[0]];
      const m = route.match(/(UP|DOWN)(\d*)$/);
      out.push({
        route, direction: m ? m[1] : 'unknown', trip: t[6] || ti, dep: deps[b], arr: arrs[a],
        boardPlatform: N.stops[pat[b]][1], alightPlatform: N.stops[pat[a]][1],
        load: avg, peak, peakSegments, crowded, confidence,
        support: loads.length ? support : 0, days: loads.length ? days : 0,
        evidence: Array.from(new Set(methods)).join('; ') || 'no evidence',
        classes, segments, b, a, pid: t[1], rid: t[0],
      });
    }
  }
  out.sort((x, y) => x.dep - y.dep || x.route.localeCompare(y.route));
  return { options: out, board, alight };
}

function num(v, digits = 1) {
  return v == null ? 'No data' : v.toFixed(digits);
}

function render() {
  if (!N) return;
  const boardRaw = $('board').value, alightRaw = $('alight').value, date = $('date').value;
  const target = sec($('time').value), win = Number($('win').value) * 60;
  const result = searchAll(boardRaw, alightRaw, date);
  const inWin = result.options.filter(o => o.dep >= target - win && o.dep <= target + win);
  const later = result.options.filter(o => o.dep > target + win);
  const allLeft = !!result.options.length && !inWin.length && !later.length;
  const next = later.length ? later[0] : (allLeft ? result.options[0] : null);
  const shown = inWin.length ? inWin : (next ? [next] : []);
  const crowdCompare = (a, b) => (a.load == null ? 1 : 0) - (b.load == null ? 1 : 0)
    || (a.load == null ? 0 : a.load) - (b.load == null ? 0 : b.load) || a.arr - b.arr;
  const rows = $('sort').value === 'crowding' ? shown.slice().sort(crowdCompare)
    : shown.slice().sort((a, b) => a.arr - b.arr || a.dep - b.dep);
  const distinct = new Set(inWin.map(o => o.route + '|' + o.trip)).size;
  const head = [];
  head.push(`<p>Window ${fmt(target - win)}–${fmt(target + win)} inclusive; ${distinct} distinct departures in the window. ${result.options.length} direct scheduled options on ${date}.</p>`);
  const describe = (m) => `<strong>${esc(m.label)}</strong>${m.ids.length ? ' → ' + m.ids.map(stopLabel).map(esc).join(' / ') : ''}`;
  head.push(`<p>Stop matching: board ${describe(result.board)}, alight ${describe(result.alight)}.</p>`);
  const r = N.meta.reference;
  head.push(`<p class="note">Recorded-load bands: Lower ≤ ${r.lower}, Typical ≤ ${r.typical}, Higher > ${r.typical} e-ticket passengers per stretch; p90 ${r.higher} defines “minutes of high recorded demand”. These are not seats or total passengers.</p>`);
  head.push(`<p class="note">${date > '2026-10-31' ? 'Based on October 2026 ticket history.' : 'Based on recorded October 2026 ticket history up to 21 October.'}</p>`);
  if (!inWin.length && later.length) head.push(`<p class="warn">No departure in the window; showing the next later bus for context only, not as a boarding recommendation.</p>`);
  if (allLeft) head.push(`<p class="warn">All nearby departures have already left; showing the next scheduled service for context.</p>`);
  if (!result.board.ids.length || !result.alight.ids.length) head.push(`<p class="warn">No matching stop. Use a name from the suggestion list or a common abbreviation.</p>`);
  let html = head.join('');
  if (rows.length) {
    html += '<table><tr><th>route</th><th>dir</th><th>dep</th><th>arr</th><th>platforms</th><th>wait min</th><th>recorded load</th><th>busiest stretch (ties shown)</th><th>high-demand min</th><th>confidence</th><th>evidence</th><th>crowding</th></tr>';
    for (const o of rows) {
      const peaks = o.peakSegments.length
        ? o.peakSegments.map(p => `${esc(p.from)} → ${esc(p.to)} (${num(p.load)}, n=${p.support}, ${p.days}d)`).join('<br>') : 'No data';
      const detail = o.segments.map(s => `<li>${esc(N.stops[s.from][0])} [${esc(N.stops[s.from][1])}] → ${esc(N.stops[s.to][0])} [${esc(N.stops[s.to][1])}]: ${(s.seconds / 60).toFixed(1)} min, recorded load ${num(s.load)} (p75 ${num(s.p75)}, n=${s.n}, ${s.days}d, ${esc(s.method)}, window ${s.window == null ? '–' : s.window + ' min'}), ${esc(recordedClass(s.load))}</li>`).join('');
      html += `<tr><td>${esc(o.route)}</td><td>${esc(o.direction)}</td><td>${fmt(o.dep)}</td><td>${fmt(o.arr)}</td><td>${esc(o.boardPlatform)} → ${esc(o.alightPlatform)}</td><td>${((o.dep - target) / 60).toFixed(1)}</td><td>${num(o.load)}</td><td>${peaks}</td><td>${num(o.crowded)}</td><td>${esc(o.confidence)}</td><td>${esc(o.evidence)}</td><td>${o.classes.map(esc).join(', ')}</td></tr>`;
      html += `<tr><td colspan="12"><details><summary>Stop-by-stop stretch evidence</summary><ul>${detail}</ul></details></td></tr>`;
    }
    html += '</table>';
  }
  html += '<p class="note">No live AVL, ticket coverage q, or fleet-seat mapping. Thin or missing evidence is labelled and never treated as an empty bus.</p>';
  html += '<p class="note"><a href="demo.html">Compact 3-pair demo</a> · <a href="https://github.com/deepanshu25169-ctrl/TRANSPORT_CROWING_PROBLEM">Source</a></p>';
  $('out').innerHTML = html;
}

fetch('network.json').then(r => r.json()).then(boot).catch(e => {
  $('status').textContent = 'Failed to load index: ' + e;
});
