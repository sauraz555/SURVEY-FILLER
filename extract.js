// Runs inside a page/frame. Finds every answerable survey control (including inside
// open Shadow DOM), groups choice/rating/card-sort controls into questions, stamps
// each control with data-sf="<id>" so Python can find it again, and returns JSON.
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = el => (el ? norm(el.innerText || el.textContent) : '');
  window.__sfN = window.__sfN || 0;
  const stamp = el => el.dataset.sf || (el.dataset.sf = 'e' + (++window.__sfN));

  const queryAll = (sel, root = document) => {
    const out = Array.from(root.querySelectorAll(sel));
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) out.push(...queryAll(sel, el.shadowRoot));
    }
    return out;
  };

  const SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password', 'search']);
  const TEXT_TYPES = new Set(['', 'text', 'email', 'number', 'tel', 'url', 'date', 'time', 'datetime-local', 'month', 'week', 'range']);
  const IGNORE_SEL = '#__sf_hud, #onetrust-consent-sdk, [id*="cookie" i][class*="banner" i], [class*="cookie-banner" i], [class*="cc-window" i]';
  const NAV_BTN_RE = /^(next|continue|proceed|back|previous|submit|finish|done|send|complete|skip|cancel|close|start|begin|get\s+started|reset|clear|ok|sign\s*in|log\s*in|accept\s*all|reject\s*all)\b|^\s*(>+|→|»|<+|←|«)\s*$/i;
  const PLACEHOLDER_OPT_RE = /^(select|choose|pick|--+|none|please\s+(select|choose))\b/i;

  const isShown = el => {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || (+s.opacity === 0 && el.tagName !== 'INPUT')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const usable = el => {
    if (el.disabled || el.readOnly || el.getAttribute('aria-disabled') === 'true') return false;
    if (el.closest && el.closest(IGNORE_SEL)) return false;
    return isShown(el) || !!(el.labels && Array.from(el.labels).some(isShown));
  };

  const getById = (el, id) => {
    const root = el && el.getRootNode ? el.getRootNode() : document;
    return (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
  };
  const byIds = (el, ids) => norm((ids || '').split(/\s+/).map(id => textOf(getById(el, id))).join(' '));
  const parentOf = el => el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
  const containsEl = (anc, c) => { for (let n = c; n; n = parentOf(n)) if (n === anc) return true; return false; };

  const colHeader = el => {
    const cell = el.closest && el.closest('td,th');
    const row = cell && cell.parentElement;
    const table = cell && cell.closest('table');
    if (!table || !row) return '';
    const head = table.querySelector('thead tr') || table.querySelector('tr');
    return !head || head === row ? '' : textOf(head.children[Array.from(row.children).indexOf(cell)]);
  };

  const mediaLabel = el => !el || !el.querySelectorAll ? '' : norm([
    ...Array.from(el.querySelectorAll('[aria-label]')).map(n => n.getAttribute('aria-label')),
    ...Array.from(el.querySelectorAll('img[alt]')).map(n => n.getAttribute('alt')),
    ...Array.from(el.querySelectorAll('svg title')).map(textOf),
  ].filter(Boolean).join(' '));

  const ownLabel = el => {
    let t = (el.getAttribute('aria-labelledby') && byIds(el, el.getAttribute('aria-labelledby'))) ||
      (el.labels && el.labels.length && norm(Array.from(el.labels).map(textOf).join(' '))) ||
      norm(el.getAttribute('aria-label') || el.dataset.label);
    if (!t && (el.getAttribute('role') || el.tagName === 'BUTTON')) t = textOf(el) || mediaLabel(el) || norm(el.dataset.value);
    if (!t && (el.type === 'radio' || el.type === 'checkbox')) {
      let n = el.nextSibling, s = '';
      while (n && !(n.nodeType === 1 && n.matches('input,select,textarea,br,div,p,li,tr,td'))) {
        s += ' ' + (n.textContent || '');
        n = n.nextSibling;
      }
      t = norm(s);
    }
    return t || mediaLabel(el) || colHeader(el) || norm(el.getAttribute('title') || el.getAttribute('placeholder') || el.value);
  };

  const questionFor = (controls, labels) => {
    const sorted = labels.filter(Boolean).sort((a, b) => b.length - a.length);
    for (let node = parentOf(controls[0]), d = 0; node && node !== document.body && d < 12; d++, node = parentOf(node)) {
      if (!controls.every(c => containsEl(node, c))) continue;
      let rest = textOf(node);
      for (const l of sorted) rest = rest.split(l).join(' ');
      rest = norm(rest);
      if (rest.length > 1) {
        if (node.tagName === 'TR') {
          const table = node.closest('table');
          const ctx = textOf(table.querySelector('caption')) || textOf(table.previousElementSibling).slice(0, 300);
          if (ctx) rest = ctx + ' — ' + rest;
        }
        return rest.slice(0, 800);
      }
    }
    return '';
  };

  const errorNear = el => {
    if (el.validationMessage) return norm(el.validationMessage);
    if (el.getAttribute('aria-errormessage')) {
      const msg = byIds(el, el.getAttribute('aria-errormessage'));
      if (msg) return msg;
    }
    for (let node = parentOf(el), d = 0; node && node !== document.body && d < 5; d++, node = parentOf(node)) {
      const alert = node.querySelector && node.querySelector('[role=alert], .error, [class*="error-message" i], [class*="field-error" i], [data-error]');
      if (alert && isShown(alert) && textOf(alert)) return textOf(alert).slice(0, 200);
    }
    return '';
  };

  const checkboxContainer = el => {
    for (let node = parentOf(el), d = 0; node && node !== document.body && d < 12; d++, node = parentOf(node)) {
      if (!node.querySelectorAll) continue;
      const boxes = Array.from(node.querySelectorAll('input[type=checkbox],[role=checkbox]'));
      let rest = textOf(node);
      for (const l of boxes.map(ownLabel).filter(Boolean).sort((a, b) => b.length - a.length)) rest = rest.split(l).join(' ');
      if (norm(rest).length > 1) return node;
    }
    return parentOf(el);
  };

  const gids = new Map();
  const gid = el => (gids.has(el) ? gids.get(el) : (gids.set(el, 'g' + gids.size), gids.get(el)));
  const used = new Set();
  const groups = new Map();
  const singles = [];
  const selector = 'input, textarea, select, [role=radio], [role=checkbox], [role=switch], [role=listbox], [role=combobox], [role=slider], [aria-haspopup=listbox], [contenteditable=""], [contenteditable=true]';

  for (const el of queryAll(selector)) {
    if (used.has(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role');
    if (tag === 'input' && SKIP_TYPES.has(type) && !role) continue;
    if (tag === 'input' && !role && !TEXT_TYPES.has(type) && type !== 'radio' && type !== 'checkbox') continue;
    if (role === 'option' || (el.parentElement && el.parentElement.closest('[role=combobox], [role=listbox]'))) continue;
    if (!usable(el)) continue;

    let key = null, kind = null;
    if (tag === 'input' && type === 'radio') {
      kind = 'radio'; key = 'r:' + (el.name ? (el.form ? gid(el.form) : '') + ':' + el.name : gid(parentOf(el)));
    } else if (role === 'radio') {
      kind = 'radio'; key = 'r:' + gid((el.closest && el.closest('[role=radiogroup]')) || parentOf(el));
    } else if ((tag === 'input' && type === 'checkbox') || role === 'checkbox') {
      kind = 'checkbox'; key = 'c:' + gid(checkboxContainer(el));
    }
    used.add(el);
    if (key) {
      if (!groups.has(key)) groups.set(key, { kind, controls: [] });
      groups.get(key).controls.push(el);
    } else {
      singles.push(el);
    }
  }

  const isChecked = el => (el.getAttribute('role') ? el.getAttribute('aria-checked') === 'true' : el.checked);
  const top = el => ((isShown(el) ? el : (el.labels && Array.from(el.labels).find(isShown)) || el).getBoundingClientRect().top + window.scrollY);
  const questions = [];

  for (const { kind, controls } of groups.values()) {
    const labels = controls.map(ownLabel);
    const text = questionFor(controls, labels);
    const q = {
      type: kind,
      text,
      required: controls.some(c => c.required || c.getAttribute('aria-required') === 'true') || !!(controls[0].closest && controls[0].closest('[aria-required=true]')) || /\*\s*$/.test(text),
      answered: controls.some(isChecked),
      options: controls.map((c, i) => ({ sf: stamp(c), label: labels[i] || ('Option ' + (i + 1)), native: !c.getAttribute('role') })),
      _y: top(controls[0]),
    };
    const err = errorNear(controls[0]);
    if (err) q.error = err;
    questions.push(q);
  }

  for (const el of singles) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    let type, options = [], answered;

    if (tag === 'select') {
      type = el.multiple ? 'multiselect' : 'select';
      options = Array.from(el.options).filter(o => o.value !== '' && !o.disabled).map(o => ({ label: norm(o.textContent), value: o.value }));
      answered = el.multiple ? el.selectedOptions.length > 0 : el.selectedIndex > 0 || (el.selectedIndex === 0 && el.options.length > 0 && el.options[0].value !== '' && el.dataset.sfTouched === '1');
    } else if (role === 'listbox') {
      type = 'listbox';
      const opts = Array.from(el.querySelectorAll('[role=option]'));
      options = opts.filter(o => norm(o.dataset.value) !== '' || (!('value' in o.dataset) && textOf(o) && !PLACEHOLDER_OPT_RE.test(textOf(o))))
        .map(o => ({ label: textOf(o) || norm(o.dataset.value), value: o.dataset.value ?? textOf(o) }));
      answered = el.dataset.sfTouched === '1' || opts.some(o => o.getAttribute('aria-selected') === 'true' && norm(o.dataset.value) !== '' && !PLACEHOLDER_OPT_RE.test(textOf(o)));
    } else if (role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox') {
      type = 'combobox';
      const popupId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
      const popup = (popupId && getById(el, popupId)) || el;
      const opts = popup ? Array.from(popup.querySelectorAll('[role=option], [role=menuitemradio]')) : [];
      options = opts.filter(o => !o.getAttribute('aria-disabled') && (norm(o.dataset.value) !== '' || (textOf(o) && !PLACEHOLDER_OPT_RE.test(textOf(o)))))
        .map(o => ({ label: textOf(o) || norm(o.dataset.value), value: o.dataset.value ?? textOf(o) }));
      answered = el.dataset.sfTouched === '1' || (tag === 'input' && !!norm(el.value)) ||
        opts.some(o => o.getAttribute('aria-selected') === 'true' && norm(o.dataset.value) !== '' && !PLACEHOLDER_OPT_RE.test(textOf(o)));
    } else if (role === 'slider') {
      type = 'slider'; answered = el.dataset.sfTouched === '1';
    } else if (role === 'switch') {
      type = 'switch'; options = [{ label: 'Yes', value: 'true' }, { label: 'No', value: 'false' }]; answered = el.dataset.sfTouched === '1';
    } else if (tag === 'textarea') {
      type = 'textarea'; answered = !!el.value.trim();
    } else if (tag === 'input') {
      type = (el.getAttribute('type') || 'text').toLowerCase();
      answered = type === 'range' ? el.dataset.sfTouched === '1' : !!el.value.trim();
    } else {
      type = 'contenteditable'; answered = !!textOf(el);
    }
    if ((type === 'select' || type === 'listbox') && !options.length) continue;

    let text = (el.getAttribute('aria-labelledby') && byIds(el, el.getAttribute('aria-labelledby'))) ||
      (el.labels && el.labels.length && norm(Array.from(el.labels).map(textOf).join(' '))) ||
      norm(el.getAttribute('aria-label'));
    if (!text) {
      const weak = norm(el.getAttribute('title') || el.getAttribute('placeholder'));
      text = norm(questionFor([el], [weak, ...options.map(o => o.label), norm(el.value || textOf(el))]).slice(0, 300) + ' ' + weak) || colHeader(el);
    }

    const q = {
      type, text: text.slice(0, 800),
      required: el.required || el.getAttribute('aria-required') === 'true' || !!(el.closest && el.closest('[aria-required=true]')) || /\*\s*$/.test(text),
      answered, sf: stamp(el), options, _y: top(el),
    };
    if (type === 'range' || type === 'number') {
      for (const a of ['min', 'max', 'step']) if (el.getAttribute(a) !== null) q[a] = el.getAttribute(a);
    } else if (type === 'slider') {
      q.min = el.getAttribute('aria-valuemin') ?? el.getAttribute('min') ?? '0';
      q.max = el.getAttribute('aria-valuemax') ?? el.getAttribute('max') ?? '100';
      q.step = el.getAttribute('aria-valuestep') ?? el.getAttribute('step') ?? '1';
    }
    if (el.getAttribute('maxlength')) q.maxlength = +el.getAttribute('maxlength');
    if (el.getAttribute('minlength')) q.minlength = +el.getAttribute('minlength');
    if (el.getAttribute('pattern')) q.pattern = el.getAttribute('pattern');
    if (el.getAttribute('placeholder')) q.placeholder = norm(el.getAttribute('placeholder'));
    const err = errorNear(el);
    if (err) q.error = err;
    questions.push(q);
  }

  // Button-group choices, NPS strips, and star ratings.
  const btnSel = 'button:not([type=submit]):not([type=reset]), [role=button]:not([aria-haspopup])';
  const btnParents = new Set();
  for (const b of queryAll(btnSel)) {
    if (used.has(b) || !usable(b)) continue;
    if (b.closest && b.closest('nav, header, footer, [role=navigation], [role=toolbar], ' + IGNORE_SEL)) continue;
    const p = (b.closest && b.closest('[role=radiogroup], [role=group], [data-rating], [class*="rating" i], [class*="scale" i], [class*="choices" i], [class*="options" i]')) || parentOf(b);
    if (p && p !== document.body) btnParents.add(p);
  }
  for (const grp of btnParents) {
    const btns = Array.from(grp.querySelectorAll(btnSel)).filter(b => !used.has(b) && usable(b) && !b.querySelector('input,select,textarea'));
    if (btns.length < 2 || btns.length > 15) continue;
    const labels = btns.map(ownLabel);
    if (labels.some(l => !l || NAV_BTN_RE.test(l))) continue;
    const text = (grp.getAttribute && grp.getAttribute('aria-labelledby') && byIds(grp, grp.getAttribute('aria-labelledby'))) ||
      (grp.getAttribute && norm(grp.getAttribute('aria-label'))) || questionFor(btns, labels);
    if (!text) continue;
    btns.forEach(b => used.add(b));
    const isMulti = /\b(select\s+all|choose\s+all|all\s+that\s+apply|multiple)\b/i.test(text) ||
      (btns.every(b => b.hasAttribute('aria-pressed')) && !grp.matches('[role=radiogroup]'));
    const active = b => b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true' ||
      b.getAttribute('aria-selected') === 'true' || b.dataset.state === 'checked' || b.dataset.selected === 'true' ||
      /\b(selected|is-selected|checked|active)\b/i.test(typeof b.className === 'string' ? b.className : '');
    questions.push({
      type: isMulti ? 'button_multi' : 'button_group',
      text: text.slice(0, 800),
      required: (grp.getAttribute && grp.getAttribute('aria-required') === 'true') || /\*\s*$/.test(text),
      answered: grp.dataset.sfTouched === '1' || btns.some(active),
      sf: stamp(grp),
      options: btns.map((b, i) => ({ sf: stamp(b), label: labels[i] || ('Option ' + (i + 1)) })),
      _y: top(btns[0]),
    });
  }

  // Drag-into-bucket card sorts.
  const bucketSel = '[data-bucket], [class*="bucket" i], [class*="dropzone" i], [class*="drop-zone" i], [aria-dropeffect]';
  const bucketRoots = new Set();
  for (const b of queryAll(bucketSel)) {
    if (!isShown(b)) continue;
    for (let anc = parentOf(b), d = 0; anc && anc !== document.body && d < 5; d++, anc = parentOf(anc)) {
      if (anc.querySelectorAll && anc.querySelectorAll(bucketSel).length >= 2 && anc.querySelector('[draggable=true]')) {
        bucketRoots.add(anc); break;
      }
    }
  }
  for (const root of bucketRoots) {
    const raw = Array.from(root.querySelectorAll(bucketSel)).filter(isShown);
    const buckets = raw.filter(b => !raw.some(o => o !== b && b.contains(o)));
    const allCards = Array.from(root.querySelectorAll('[draggable=true]')).filter(c => isShown(c) && !buckets.includes(c));
    if (buckets.length < 2 || !allCards.length) continue;
    allCards.forEach(c => used.add(c));
    buckets.forEach(b => used.add(b));
    const unsorted = allCards.filter(c => !buckets.some(b => b.contains(c)));
    const cards = unsorted.length ? unsorted : allCards;
    const bucketLabels = buckets.map((b, i) => {
      let t = norm(b.dataset.bucket || b.getAttribute('aria-label') || textOf(b.querySelector('h1,h2,h3,h4,h5,strong,.bucket-title,[class*="title" i]')) || textOf(b));
      for (const c of allCards) if (b.contains(c)) t = norm(t.split(textOf(c)).join(' '));
      return t || ('Category ' + (i + 1));
    });
    questions.push({
      type: 'bucket',
      text: questionFor(buckets, [...bucketLabels, ...allCards.map(textOf)]) || questionFor([root], [...bucketLabels, ...allCards.map(textOf)]),
      required: false,
      answered: unsorted.length === 0,
      sf: stamp(root),
      items: cards.map((c, i) => ({ sf: stamp(c), label: textOf(c) || ('Item ' + (i + 1)) })),
      options: buckets.map((b, i) => ({ sf: stamp(b), label: bucketLabels[i] })),
      _y: top(root),
    });
  }

  // Drag-and-drop ranking lists.
  const rankSel = '[draggable=true], [class*=sortable] > *, [class*=Sortable] > *, ul[class*=rank] > li, ol[class*=rank] > li, [data-sortable] > *, [aria-roledescription~=sortable]';
  const lists = new Set();
  for (const item of queryAll(rankSel)) {
    if (!used.has(item) && item.parentElement && isShown(item)) lists.add(item.parentElement);
  }
  for (const list of lists) {
    const items = Array.from(list.children).filter(c => !used.has(c) && c.matches(rankSel) && isShown(c));
    if (items.length < 2 || items.some(i => i.querySelector('input,select,textarea'))) continue;
    const labels = items.map(textOf);
    questions.push({
      type: 'rank',
      text: questionFor([list], labels),
      required: false,
      answered: list.dataset.sfTouched === '1',
      sf: stamp(list),
      options: items.map((it, i) => ({ sf: stamp(it), label: labels[i] || ('Item ' + (i + 1)) })),
      _y: top(list),
    });
  }

  questions.sort((a, b) => a._y - b._y);
  questions.forEach(q => delete q._y);
  return questions;
}
