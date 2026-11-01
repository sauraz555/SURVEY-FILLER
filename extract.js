// Runs inside a page/frame. Finds every answerable control, groups choice
// controls into questions, stamps each control with data-sf="<id>" so Python
// can find it again, and returns a JSON description of the questions.
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = el => (el ? norm(el.innerText || el.textContent) : '');
  window.__sfN = window.__sfN || 0;
  const stamp = el => {
    if (!el.dataset.sf) el.dataset.sf = 'e' + (++window.__sfN);
    return el.dataset.sf;
  };

  const SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password', 'search']);
  const TEXT_TYPES = new Set(['', 'text', 'email', 'number', 'tel', 'url', 'date', 'time',
    'datetime-local', 'month', 'week', 'range']);

  const isShown = el => {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  // Custom-styled radios/checkboxes hide the real input; accept it if its label shows.
  const usable = el => {
    if (el.disabled || el.readOnly || el.getAttribute('aria-disabled') === 'true') return false;
    if (isShown(el)) return true;
    return !!(el.labels && Array.from(el.labels).some(isShown));
  };

  const byIds = ids => norm((ids || '').split(/\s+/).map(id => textOf(document.getElementById(id))).join(' '));

  // Header cell of the same column, for matrix/Likert tables.
  const colHeader = el => {
    const cell = el.closest('td,th');
    const row = cell && cell.parentElement;
    const table = cell && cell.closest('table');
    if (!table || !row) return '';
    const idx = Array.from(row.children).indexOf(cell);
    const head = table.querySelector('thead tr') || table.querySelector('tr');
    if (!head || head === row) return '';
    return textOf(head.children[idx]);
  };

  const ownLabel = el => {
    let t = '';
    if (el.getAttribute('aria-labelledby')) t = byIds(el.getAttribute('aria-labelledby'));
    if (!t && el.labels && el.labels.length) t = norm(Array.from(el.labels).map(textOf).join(' '));
    if (!t) t = norm(el.getAttribute('aria-label'));
    if (!t && el.getAttribute('role')) t = textOf(el) || norm(el.dataset.value);
    if (!t && (el.type === 'radio' || el.type === 'checkbox')) {
      // <input type=radio> Yes<br>
      let n = el.nextSibling, s = '';
      while (n && !(n.nodeType === 1 && n.matches('input,select,textarea,br,div,p,li,tr,td'))) {
        s += ' ' + (n.textContent || '');
        n = n.nextSibling;
      }
      t = norm(s);
    }
    if (!t) t = colHeader(el);
    if (!t) t = norm(el.getAttribute('title') || el.getAttribute('placeholder') || el.value);
    return t;
  };

  // Walk up from the controls until an ancestor holds text that is not just
  // the controls' own labels: that leftover text is the question.
  const questionFor = (controls, labels) => {
    const sorted = labels.filter(Boolean).sort((a, b) => b.length - a.length);
    let node = controls[0].parentElement;
    for (let depth = 0; node && node !== document.body && depth < 12; depth++, node = node.parentElement) {
      if (!controls.every(c => node.contains(c))) continue;
      let rest = textOf(node);
      for (const l of sorted) rest = rest.split(l).join(' ');
      rest = norm(rest);
      if (rest.length > 1) {
        if (node.tagName === 'TR') {
          // Matrix row: prefix the table's caption/heading for context.
          const table = node.closest('table');
          const ctx = textOf(table.querySelector('caption')) || textOf(table.previousElementSibling).slice(0, 300);
          if (ctx) rest = ctx + ' — ' + rest;
        }
        return rest.slice(0, 800);
      }
    }
    return '';
  };

  // Checkbox group = nearest ancestor that has text beyond its checkboxes' labels.
  const checkboxContainer = el => {
    let node = el.parentElement;
    for (let depth = 0; node && node !== document.body && depth < 12; depth++, node = node.parentElement) {
      const boxes = Array.from(node.querySelectorAll('input[type=checkbox],[role=checkbox]'));
      let rest = textOf(node);
      for (const l of boxes.map(ownLabel).filter(Boolean).sort((a, b) => b.length - a.length)) rest = rest.split(l).join(' ');
      if (norm(rest).length > 1) return node;
    }
    return el.parentElement;
  };

  const gids = new Map();
  const gid = el => {
    if (!gids.has(el)) gids.set(el, 'g' + gids.size);
    return gids.get(el);
  };

  const groups = new Map();
  const singles = [];
  const selector = 'input, textarea, select, [role=radio], [role=checkbox], [role=listbox], [contenteditable=""], [contenteditable=true]';
  for (const el of document.querySelectorAll(selector)) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role');
    if (tag === 'input' && SKIP_TYPES.has(type)) continue;
    if (tag === 'input' && !role && !TEXT_TYPES.has(type) && type !== 'radio' && type !== 'checkbox') continue;
    if (!usable(el)) continue;

    let key = null, kind = null;
    if (tag === 'input' && type === 'radio') {
      kind = 'radio';
      key = 'r:' + (el.name ? (el.form ? gid(el.form) : '') + ':' + el.name : gid(el.parentElement));
    } else if (role === 'radio') {
      kind = 'radio';
      key = 'r:' + gid(el.closest('[role=radiogroup]') || el.parentElement);
    } else if ((tag === 'input' && type === 'checkbox') || role === 'checkbox') {
      kind = 'checkbox';
      key = 'c:' + gid(checkboxContainer(el));
    }
    if (key) {
      if (!groups.has(key)) groups.set(key, { kind, controls: [] });
      groups.get(key).controls.push(el);
    } else {
      singles.push(el);
    }
  }

  const isChecked = el => (el.getAttribute('role') ? el.getAttribute('aria-checked') === 'true' : el.checked);
  const questions = [];
  const top = el => el.getBoundingClientRect().top + window.scrollY;

  for (const { kind, controls } of groups.values()) {
    const labels = controls.map(ownLabel);
    const text = questionFor(controls, labels);
    const required = controls.some(c => c.required || c.getAttribute('aria-required') === 'true') ||
      !!controls[0].closest('[aria-required=true]') || /\*\s*$/.test(text);
    questions.push({
      type: kind,
      text,
      required,
      answered: controls.some(isChecked),
      options: controls.map((c, i) => ({ sf: stamp(c), label: labels[i] || ('Option ' + (i + 1)), native: !c.getAttribute('role') })),
      _y: top(controls[0]),
    });
  }

  for (const el of singles) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    let type, options = [], answered;
    if (tag === 'select') {
      type = el.multiple ? 'multiselect' : 'select';
      options = Array.from(el.options)
        .filter(o => o.value !== '' && !o.disabled)
        .map(o => ({ label: norm(o.textContent), value: o.value }));
      answered = el.multiple ? el.selectedOptions.length > 0
        : el.selectedIndex > 0 || (el.selectedIndex === 0 && el.options[0].value !== '' && el.dataset.sfTouched === '1');
    } else if (role === 'listbox') {
      type = 'listbox';
      const opts = Array.from(el.querySelectorAll('[role=option]'));
      options = opts
        .filter(o => norm(o.dataset.value) !== '' || (!('value' in o.dataset) && textOf(o)))
        .map(o => ({ label: textOf(o) || norm(o.dataset.value), value: o.dataset.value || textOf(o) }));
      answered = opts.some(o => o.getAttribute('aria-selected') === 'true' && norm(o.dataset.value) !== '');
    } else if (tag === 'textarea') {
      type = 'textarea'; answered = !!el.value.trim();
    } else if (tag === 'input') {
      type = (el.getAttribute('type') || 'text').toLowerCase(); answered = !!el.value.trim();
    } else {
      type = 'contenteditable'; answered = !!textOf(el);
    }
    if ((type === 'select' || type === 'listbox') && !options.length) continue;

    // A real label (aria/label element) is the question; otherwise look around for it.
    let text = (el.getAttribute('aria-labelledby') && byIds(el.getAttribute('aria-labelledby'))) ||
      (el.labels && el.labels.length && norm(Array.from(el.labels).map(textOf).join(' '))) ||
      norm(el.getAttribute('aria-label'));
    if (!text) {
      const weak = norm(el.getAttribute('title') || el.getAttribute('placeholder'));
      const context = questionFor([el], [weak, ...options.map(o => o.label), norm(el.value)]).slice(0, 300);
      text = norm(context + ' ' + weak) || colHeader(el);
    }
    const q = {
      type,
      text: text.slice(0, 800),
      required: el.required || el.getAttribute('aria-required') === 'true' || !!el.closest('[aria-required=true]') || /\*\s*$/.test(text),
      answered,
      sf: stamp(el),
      options,
      _y: top(el),
    };
    if (type === 'range' || type === 'number') {
      for (const a of ['min', 'max', 'step']) if (el.getAttribute(a) !== null) q[a] = el.getAttribute(a);
    }
    if (el.getAttribute('maxlength')) q.maxlength = +el.getAttribute('maxlength');
    if (el.getAttribute('placeholder')) q.placeholder = norm(el.getAttribute('placeholder'));
    questions.push(q);
  }

  // Drag-and-drop ranking lists: a parent with 2+ draggable/sortable children.
  const rankSel = '[draggable=true], [class*=sortable] > *, [class*=Sortable] > *, ul[class*=rank] > li, ' +
    'ol[class*=rank] > li, [data-sortable] > *, [aria-roledescription~=sortable]';
  const lists = new Set();
  for (const item of document.querySelectorAll(rankSel)) {
    const parent = item.parentElement;
    if (parent && !lists.has(parent) && isShown(item)) lists.add(parent);
  }
  for (const list of lists) {
    const items = Array.from(list.children).filter(c => c.matches(rankSel) && isShown(c));
    // Items holding their own inputs (e.g. a rank dropdown per row) are filled through those.
    if (items.length < 2 || items.some(i => i.querySelector('input,select,textarea'))) continue;
    const labels = items.map(textOf);
    questions.push({
      type: 'rank',
      text: questionFor([list], labels),
      required: false,
      answered: false,
      sf: stamp(list),
      options: items.map((it, i) => ({ sf: stamp(it), label: labels[i] || ('Item ' + (i + 1)) })),
      _y: top(list),
    });
  }

  // Keep the on-page reading order.
  questions.sort((a, b) => a._y - b._y);
  questions.forEach(q => delete q._y);
  return questions;
}
