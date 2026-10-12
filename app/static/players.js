/* Players page: live filtering, URL sync, client sort. Progressive enhancement
   of the GET form in players.html -- every param name and format here mirrors
   app/players.py (parse_filters / apply_filters), so a reload gives the view
   this script has on screen. Position is the one exception: it changes the
   column set, so it stays a server round trip. */
(function () {
  'use strict';
  var form = document.getElementById('pf');
  var table = document.querySelector('table.ptab');
  if (!form || !table) return;

  var tbody = table.tBodies[0];
  var scroller = table.closest('.ptab-scroll');
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var countBox = document.querySelector('.pcount');
  var countText = countBox.querySelector('[role=status]');
  var chipBox = document.getElementById('fchips');
  var noResult = tbody.querySelector('tr.noresult');
  var nrChips = noResult && noResult.querySelector('.nr-chips');
  var total = parseInt(countBox.getAttribute('data-total'), 10) || 0;
  var sortInput = form.elements.sort, dirInput = form.elements.dir;
  var applyBtn = form.querySelector('.apply');
  var sheetLabel = form.querySelector('.s-closed');
  var fetched = false;       // true once tbody holds every row, not just the server's filtered set
  var firstLoadFiltered = !!chipBox.children.length;
  var timer = null;
  var injAny = new URLSearchParams(location.search).get('inj') === 'any';

  document.documentElement.classList.add('js-pf');
  // (seedMulti runs once helpers below exist; see first paint)

  /* ---------- state read from the form ---------- */
  /* team and owner are single selects, but the server also takes lists
     (team=KC,DAL, owner=1&owner=2). Such a URL gets a synthetic option holding
     the whole list, so the form can show it and syncUrl never drops it. Tokens
     are normalised like parse_filters: teams upper-cased, sorted, unique;
     owners `fa` or digits. */
  function seedMulti(name) {
    var sel = form.elements[name];
    if (!sel) return;
    var toks = [];
    new URLSearchParams(location.search).getAll(name).forEach(function (v) {
      v.split(',').forEach(function (t) { t = t.trim(); if (t) toks.push(t); });
    });
    if (name === 'team') toks = toks.map(function (t) { return t.toUpperCase(); }).sort();
    else toks = toks.map(function (t) { return t.toLowerCase() === 'fa' ? 'fa' : /^\d+$/.test(t) ? String(parseInt(t, 10)) : ''; });
    toks = toks.filter(function (t, i) { return t && toks.indexOf(t) === i; });
    if (!toks.length) return;
    var value = toks.join(',');
    if (!Array.prototype.some.call(sel.options, function (o) { return o.value === value; })) {
      var label = toks.map(function (t) {
        if (name === 'team') return t;
        if (t === 'fa') return 'Free agents';
        var o = sel.querySelector('option[value="' + t + '"]');
        return o ? o.textContent : 'Team ' + t;
      }).join(', ');
      var opt = el('option', '', label);
      opt.value = value;
      sel.insertBefore(opt, sel.options[1] || null);
    }
    sel.value = value;
  }

  function csv(s) { return s.split(',').map(function (t) { return t.trim(); }).filter(Boolean); }
  function numVal(name, lo, hi) {
    var el = form.elements[name];
    if (!el || el.value.trim() === '') return null;
    var v = parseFloat(el.value);
    if (!isFinite(v)) return null;
    if (lo !== undefined) v = Math.min(Math.max(v, lo), hi);
    return v;
  }
  function checkedVals(name) {
    return Array.prototype.filter.call(form.querySelectorAll('input[name=' + name + ']'),
      function (i) { return i.checked; }).map(function (i) { return i.value; });
  }
  function readState() {
    var st = {};
    var q = form.elements.q.value.trim();
    if (q) st.q = q;
    if (form.elements.team.value) st.team = csv(form.elements.team.value.toUpperCase());
    if (form.elements.owner.value) st.owner = csv(form.elements.owner.value);
    var avail = form.querySelector('input[name=avail]:checked');
    if (avail && avail.value !== 'all') st.avail = avail.value;
    [['avg_min'], ['proj_min'], ['own_min', 0, 100], ['own_max', 0, 100], ['snap_min', 0, 100]]
      .forEach(function (a) { var v = numVal(a[0], a[1], a[2]); if (v !== null) st[a[0]] = v; });
    ['rookie', 'starter', 'plays'].forEach(function (k) {
      if (form.elements[k] && form.elements[k].checked) st[k] = 1;
    });
    var inj = checkedVals('inj');
    if (inj.length) st.inj = (injAny && inj.length === 4) ? ['any'] : inj;
    return st;
  }

  /* ---------- matching (same AND semantics as apply_filters) ---------- */
  function num(s) { return s === '' || s === undefined ? null : parseFloat(s); }
  function matcher(st) {
    var needle = st.q ? st.q.toLowerCase() : null;
    var tests = [];
    if (needle) tests.push(function (d) { return d.name.indexOf(needle) !== -1; });
    if (st.team) tests.push(function (d) { return st.team.indexOf(d.team) !== -1; });
    if (st.owner) tests.push(function (d) { return st.owner.indexOf(String(d.owner)) !== -1; });
    if (st.avail === 'free') tests.push(function (d) { return d.owner === 'fa'; });
    if (st.avail === 'rostered') tests.push(function (d) { return d.owner !== 'fa'; });
    [['avg_min', 'avg', 1], ['proj_min', 'proj', 1], ['own_min', 'own', 1],
     ['own_max', 'own', 0], ['snap_min', 'snap', 1]].forEach(function (a) {
      if (st[a[0]] === undefined) return;
      var lim = st[a[0]];
      tests.push(function (d) {
        var v = num(d[a[1]]);
        return v !== null && (a[2] ? v >= lim : v <= lim);
      });
    });
    if (st.rookie) tests.push(function (d) { return d.rookie === '1'; });
    if (st.starter) tests.push(function (d) { return d.starter === '1'; });
    if (st.plays) tests.push(function (d) { return d.plays === '1'; });
    if (st.inj) tests.push(function (d) {
      return st.inj[0] === 'any' ? d.inj !== '' : st.inj.indexOf(d.inj) !== -1;
    });
    return function (row) {
      var d = row.dataset;
      for (var i = 0; i < tests.length; i++) if (!tests[i](d)) return false;
      return true;
    };
  }

  function dataRows() {
    return Array.prototype.filter.call(tbody.rows, function (r) { return r !== noResult; });
  }

  /* ---------- chips (same labels as players.active_filters) ---------- */
  var LABELS = { q: 'Name', team: 'NFL team', owner: 'Owner', avg_min: 'Avg ≥', proj_min: 'Proj ≥',
    own_min: 'Own% ≥', own_max: 'Own% ≤', snap_min: 'Snap% ≥', rookie: 'Rookies',
    starter: 'Starters', plays: 'Has a game', inj: 'Injury' };
  var ORDER = ['q', 'team', 'owner', 'avail', 'avg_min', 'proj_min', 'own_min', 'own_max', 'snap_min',
    'rookie', 'starter', 'plays', 'inj'];
  function fmt(v) { return typeof v === 'number' ? String(parseFloat(v)) : String(v); }
  function ownerName(tok) {
    if (tok === 'fa') return 'Free agents';
    var o = form.querySelector('select[name=owner] option[value="' + tok + '"]');
    return o ? o.textContent : 'Team ' + tok;
  }
  function chipInfo(st) {
    return ORDER.filter(function (k) { return st[k] !== undefined; }).map(function (k) {
      var label, value = '';
      if (k === 'avail') label = st[k] === 'free' ? 'Free agents' : 'Rostered';
      else if (k === 'rookie' || k === 'starter' || k === 'plays') label = LABELS[k];
      else if (k === 'owner') { label = LABELS[k]; value = st[k].map(ownerName).join(', '); }
      else if (Array.isArray(st[k])) { label = LABELS[k]; value = st[k].join(', '); }
      else { label = LABELS[k]; value = fmt(st[k]); }
      return { key: k, label: label, value: value };
    });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }
  function chip(c, prefix) {
    var a = el('a', 'fchip');
    a.href = '#';
    a.setAttribute('data-remove', c.key);
    a.setAttribute('aria-label', 'Remove filter: ' + c.label + ' ' + c.value);
    a.appendChild(document.createTextNode((prefix || '') + c.label + (c.value ? ' ' : '')));
    if (c.value) a.appendChild(el('b', '', c.value));
    var x = el('span', 'x', '×');
    x.setAttribute('aria-hidden', 'true');
    a.appendChild(x);
    return a;
  }
  function resetLink() {
    var a = el('a', 'freset', 'Reset all');
    a.href = resetHref();
    a.setAttribute('data-reset', '1');
    return a;
  }
  function renderChips(chips) {
    chipBox.textContent = '';
    chips.forEach(function (c) { chipBox.appendChild(chip(c)); });
    if (chips.length) chipBox.appendChild(resetLink());
    if (nrChips) {
      nrChips.textContent = '';
      chips.forEach(function (c) { nrChips.appendChild(chip(c, 'Remove ')); nrChips.appendChild(document.createTextNode(' ')); });
      nrChips.appendChild(resetLink());
    }
    if (sheetLabel) sheetLabel.textContent = 'Filters' + (chips.length ? ' (' + chips.length + ')' : '');
  }

  /* ---------- URL ---------- */
  function currentSort() {
    var th = table.querySelector('th[aria-sort]');
    return { key: th ? th.getAttribute('data-key') : sortInput.value,
             desc: th ? th.getAttribute('aria-sort') === 'descending' : dirInput.value === 'desc' };
  }
  /* Phone default columns: with no `cols` in the URL on a <=640px screen the
     heavy groups start off. The chips are unticked to match, but nothing is
     written to the URL (and the chips are not submitted) until the user
     touches a column chip, so a shared link never carries the phone defaults. */
  var PHONE_OFF = ['weeks', 'usage', 'pass', 'rush', 'rec', 'kick', 'def'];
  var phoneCols = !/(^|[?&])cols=/.test(location.search) && window.matchMedia &&
                  window.matchMedia('(max-width: 640px)').matches;
  function phoneApply() {
    if (!phoneCols) return;
    Array.prototype.forEach.call(form.querySelectorAll('input[name=cols]'), function (b) {
      if (PHONE_OFF.indexOf(b.value) !== -1) b.checked = false;
    });
  }
  /* Both the submit event and the position branch go through here: the phone
     default chips must never be sent as `cols`. */
  function holdCols() {
    if (!phoneCols) return;
    Array.prototype.forEach.call(form.querySelectorAll('input[name=cols]'), function (b) { b.disabled = true; });
    setTimeout(function () {
      Array.prototype.forEach.call(form.querySelectorAll('input[name=cols]'), function (b) { b.disabled = false; });
    }, 0);
  }
  function submitNoCols() { holdCols(); form.submit(); }
  form.addEventListener('submit', holdCols);
  function colsParam() {
    if (phoneCols) return '';
    var boxes = form.querySelectorAll('input[name=cols]');
    var on = checkedVals('cols');
    if (!boxes.length || !on.length || on.length === boxes.length) return '';
    return on.join(',');
  }
  /* Every query starts from the page's own query string, so params this script
     does not own (legacy `available`, anything the route learns later) survive.
     Only the keys listed in OWNED are removed and re-set. */
  var OWNED = ORDER.concat(['available', 'pos', 'sort', 'dir', 'cols']);
  function toQuery(p) { return p.toString().replace(/%2C/gi, ','); }
  function build(st, sort, desc, withFilters) {
    var p = new URLSearchParams(location.search);
    OWNED.forEach(function (k) { p.delete(k); });
    if (withFilters) ORDER.forEach(function (k) {
      if (st[k] === undefined) return;
      p.set(k, Array.isArray(st[k]) ? st[k].map(fmt).join(',') : fmt(st[k]));
    });
    var pos = form.querySelector('input[name=pos]:checked');
    if (pos && pos.value) p.set('pos', pos.value);
    p.set('sort', sort);
    p.set('dir', desc ? 'desc' : 'asc');
    var cols = colsParam();
    if (cols) p.set('cols', cols);
    return location.pathname + '?' + toQuery(p);
  }
  function resetHref() {
    var s = currentSort();
    var p = new URLSearchParams(location.search);
    OWNED.forEach(function (k) { p.delete(k); });
    p.set('sort', s.key);
    p.set('dir', s.desc ? 'desc' : 'asc');
    return location.pathname + '?' + toQuery(p);
  }

  /* ---------- apply ---------- */
  var lastSt = {};
  function applyFilters() {
    var st = readState();
    lastSt = st;
    var ok = matcher(st), shown = 0, n = 0;
    dataRows().forEach(function (r) {
      var show = ok(r);
      if (r.hidden === show) r.hidden = !show;
      if (show) { shown++; r.cells[0].textContent = ++n; }
    });
    countText.innerHTML = '';
    countText.appendChild(el('b', '', String(shown)));
    countText.appendChild(document.createTextNode(' of ' + total + ' players'));
    if (noResult) noResult.hidden = shown !== 0;
    if (applyBtn) applyBtn.textContent = 'Show ' + shown + ' players';
    renderChips(chipInfo(st));
    syncUrl();
  }

  function syncUrl() {
    var s = currentSort();
    history.replaceState(null, '', build(lastSt, s.key, s.desc, true));
    sortInput.value = s.key;
    dirInput.value = s.desc ? 'desc' : 'asc';
    // Header links must carry the live state, or a click would drop the filters.
    Array.prototype.forEach.call(table.querySelectorAll('th[data-key] a'), function (a) {
      var th = a.parentNode, key = th.getAttribute('data-key');
      var desc = key === s.key ? !s.desc : th.getAttribute('data-desc') === '1';
      a.href = build(lastSt, key, desc, true);
    });
  }

  /* The server only sent the rows matching the URL's filters. Widening a
     filter needs the rest, so fetch the unfiltered page once, on first use. */
  function ensureAllRows() {
    if (fetched || !firstLoadFiltered) { fetched = true; return Promise.resolve(); }
    var s = currentSort();
    var pos = form.querySelector('input[name=pos]:checked');
    return fetch(build({}, s.key, s.desc, false), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var rows = doc.querySelectorAll('table.ptab tbody tr:not(.noresult)');
        if (!rows.length) return;
        dataRows().forEach(function (r) { r.remove(); });
        var frag = document.createDocumentFragment();
        Array.prototype.forEach.call(rows, function (r) { frag.appendChild(document.importNode(r, true)); });
        tbody.insertBefore(frag, noResult);
        stripHidden();
        var m = doc.querySelector('.pcount');
        if (m) total = parseInt(m.getAttribute('data-total'), 10) || total;
        fetched = true;
      })
      .catch(function () { /* stay on the server's rows; the form still submits normally */ });
  }

  function onChange(immediate) {
    clearTimeout(timer);
    var run = function () { ensureAllRows().then(applyFilters); };
    if (immediate) run(); else timer = setTimeout(run, 120);
  }

  form.addEventListener('input', function (e) {
    var t = e.target;
    if (t.type === 'search' || t.type === 'number') onChange(false);
  });
  form.addEventListener('change', function (e) {
    var t = e.target;
    if (t.name === 'pos') { submitNoCols(); return; }
    if (t.name === 'cols') { phoneCols = false; applyCols(); syncUrl(); return; }
    if (t.type === 'search' || t.type === 'number') return;   // handled by 'input'
    if (t.name === 'inj') injAny = false;
    onChange(true);
  });
  // Enter in a field submits the GET form as before; nothing to intercept.

  /* chips and reset */
  function removeFilter(key) {
    var f = form.elements;
    if (key === 'q') f.q.value = '';
    else if (key === 'team') f.team.value = '';
    else if (key === 'owner') f.owner.value = '';
    else if (key === 'avail') form.querySelector('input[name=avail][value=all]').checked = true;
    else if (key === 'inj') Array.prototype.forEach.call(form.querySelectorAll('input[name=inj]'), function (i) { i.checked = false; });
    else if (f[key] && f[key].type === 'checkbox') f[key].checked = false;
    else if (f[key]) f[key].value = '';
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[data-remove], a[data-reset], .freset');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var pos = form.querySelector('input[name=pos]:checked');
    if (a.hasAttribute('data-remove')) {
      e.preventDefault();
      removeFilter(a.getAttribute('data-remove'));
      onChange(true);
      countText.setAttribute('tabindex', '-1');
      setTimeout(function () { var n = chipBox.querySelector('a.fchip') || countText; n.focus(); }, 0);
    } else {
      if (pos && pos.value) return;           // a position is a server filter: follow the link
      e.preventDefault();
      form.reset();
      phoneApply(); applyCols();
      ['q', 'team', 'owner', 'avg_min', 'proj_min', 'own_min', 'own_max', 'snap_min'].forEach(function (k) { form.elements[k].value = ''; });
      Array.prototype.forEach.call(form.querySelectorAll('input[type=checkbox]:not([name=cols])'), function (i) { i.checked = false; });
      form.querySelector('input[name=avail][value=all]').checked = true;
      onChange(true);
    }
  });

  /* ---------- column groups ---------- */
  function stripHidden() {
    Array.prototype.forEach.call(table.querySelectorAll('[class*="f-"][hidden]'), function (c) { c.hidden = false; });
  }
  function applyCols() {
    var boxes = form.querySelectorAll('input[name=cols]');
    if (!boxes.length) return;
    var anyOn = checkedVals('cols').length > 0;
    Array.prototype.forEach.call(boxes, function (b) {
      table.classList.toggle('off-' + b.value, anyOn && !b.checked);
    });
  }

  /* ---------- sort ---------- */
  function sortKey(row, idx) {
    var v = row.cells[idx].getAttribute('data-v');
    if (v === null || v === '') return null;
    return /^-?\d+(\.\d+)?$/.test(v) ? parseFloat(v) : v.toLowerCase();
  }
  function cmp(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  function sortBy(key, desc) {
    var ths = Array.prototype.slice.call(table.tHead.rows[0].cells);
    var idx = ths.map(function (t) { return t.getAttribute('data-key'); }).indexOf(key);
    if (idx < 0) return;
    var rows = dataRows();
    var before = new Map();
    var animate = !reduced && table.rows.length > 0;
    var box = scroller ? scroller.getBoundingClientRect() : { top: 0, bottom: innerHeight };
    if (animate) rows.forEach(function (r) { if (!r.hidden) before.set(r, r.getBoundingClientRect().top); });

    var present = [], missing = [];
    rows.forEach(function (r) {
      var k = sortKey(r, idx);
      var item = { r: r, k: k, proj: num(r.getAttribute('data-proj-sort')), name: r.getAttribute('data-name') || '' };
      (k === null ? missing : present).push(item);
    });
    // Ties: projection high-to-low (blank last), then name A-Z -- as sort_rows.
    function tie(a, b) {
      if ((a.proj === null) !== (b.proj === null)) return a.proj === null ? 1 : -1;
      if (a.proj !== b.proj) return b.proj - a.proj;
      return cmp(a.name, b.name);
    }
    present.sort(function (a, b) {
      var c = cmp(a.k, b.k);
      if (c) return desc ? -c : c;
      return tie(a, b);
    });
    missing.sort(tie);
    var frag = document.createDocumentFragment();
    present.concat(missing).forEach(function (i) { frag.appendChild(i.r); });
    tbody.insertBefore(frag, noResult);

    ths.forEach(function (th, i) {
      var on = i === idx;
      th.classList.toggle('on', on);
      if (on) th.setAttribute('aria-sort', desc ? 'descending' : 'ascending'); else th.removeAttribute('aria-sort');
      var arrow = th.querySelector('.arrow');
      if (arrow && !on) arrow.remove();
      if (on) {
        if (!arrow) { arrow = el('span', 'arrow'); th.appendChild(arrow); }
        arrow.textContent = desc ? '▼' : '▲';
      }
    });
    Array.prototype.forEach.call(tbody.querySelectorAll('td.on'), function (c) { c.classList.remove('on'); });
    rows.forEach(function (r) { r.cells[idx].classList.add('on'); });

    var n = 0;
    dataRows().forEach(function (r) { if (!r.hidden) r.cells[0].textContent = ++n; });
    sortInput.value = key; dirInput.value = desc ? 'desc' : 'asc';
    syncUrl();

    if (animate) flip(before, box);
  }

  /* FLIP: rows near the viewport glide from their old position to the new one.
     Skipped when too many rows move -- the work would outweigh the effect. */
  function flip(before, box) {
    var moves = [];
    before.forEach(function (oldTop, r) {
      var newTop = r.getBoundingClientRect().top;
      var dy = oldTop - newTop;
      if (!dy) return;
      var seen = (oldTop < box.bottom && oldTop > box.top - 40) || (newTop < box.bottom && newTop > box.top - 40);
      moves.push({ r: r, dy: dy, seen: seen });
    });
    if (moves.length > 300) return;
    var shown = moves.filter(function (m) { return m.seen; }).slice(0, 60);
    shown.forEach(function (m) {
      if (!m.r.animate) return;
      m.r.animate([{ transform: 'translateY(' + m.dy + 'px)' }, { transform: 'none' }],
        { duration: 320, easing: 'cubic-bezier(.4,0,.2,1)' });
    });
  }

  table.tHead.addEventListener('click', function (e) {
    var a = e.target.closest('th[data-key] a');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault();
    var th = a.parentNode, key = th.getAttribute('data-key'), s = currentSort();
    var desc = key === s.key ? !s.desc : th.getAttribute('data-desc') === '1';
    sortBy(key, desc);
  });

  /* ---------- shortcuts ---------- */
  var search = form.elements.q;
  document.addEventListener('keydown', function (e) {
    var t = e.target, typing = t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName));
    if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault(); search.focus(); search.select();
    } else if (e.key === 'Escape' && t === search) {
      if (search.value) { search.value = ''; onChange(true); } else search.blur();
    }
  });

  /* ---------- first paint ---------- */
  seedMulti('team');
  seedMulti('owner');
  stripHidden();
  phoneApply();
  applyCols();
  var s0 = currentSort();
  lastSt = readState();
  renderChips(chipInfo(lastSt));
  syncUrl();
  if (!reduced) {
    tbody.classList.add('reveal');
    setTimeout(function () { tbody.classList.remove('reveal'); }, 900);
  }
  // Browsers restore form values on back/forward; reconcile the table with them.
  window.addEventListener('pageshow', function (e) { if (e.persisted) onChange(true); });
  void s0;
})();
