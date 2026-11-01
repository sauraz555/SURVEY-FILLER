// Fast path: applies all answers for one frame in a single evaluate call.
// actions: [{sf, op: 'text'|'check'|'uncheck'|'select', value}]
// Returns the sf ids that could not be applied, so Python can retry them with
// real Playwright input.
(actions) => {
  const failed = [];
  const fire = (el, ...names) => names.forEach(n => el.dispatchEvent(new Event(n, { bubbles: true })));
  // Use the prototype setter so React/Vue controlled inputs notice the change.
  const setNative = (el, value) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  };
  const checked = el => (el.getAttribute('role') ? el.getAttribute('aria-checked') === 'true' : el.checked);

  for (const a of actions) {
    const el = document.querySelector(`[data-sf="${a.sf}"]`);
    if (!el) { failed.push(a.sf); continue; }
    try {
      if (a.op === 'text') {
        if (el.isContentEditable) {
          el.focus();
          el.textContent = a.value;
          fire(el, 'input');
        } else {
          el.focus();
          setNative(el, String(a.value));
          fire(el, 'input', 'change');
          el.blur();
          if (el.type !== 'range' && String(el.value) !== String(a.value)) { failed.push(a.sf); continue; }
        }
      } else if (a.op === 'check' || a.op === 'uncheck') {
        const want = a.op === 'check';
        if (checked(el) !== want) el.click();
        if (checked(el) !== want) {
          // Hidden input behind a styled label: click the label instead.
          const lbl = el.labels && el.labels[0];
          if (lbl) lbl.click();
        }
        if (checked(el) !== want) failed.push(a.sf);
      } else if (a.op === 'select') {
        setNative(el, a.value);
        el.dataset.sfTouched = '1';
        fire(el, 'input', 'change');
        if (el.value !== a.value) failed.push(a.sf);
      } else if (a.op === 'multiselect') {
        for (const o of el.options) o.selected = a.value.includes(o.value);
        fire(el, 'input', 'change');
        const got = Array.from(el.selectedOptions).map(o => o.value);
        if (got.length !== a.value.length) failed.push(a.sf);
      } else {
        failed.push(a.sf);
      }
    } catch (e) {
      failed.push(a.sf);
    }
  }
  return failed;
}
