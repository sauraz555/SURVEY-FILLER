"""
Survey Filler - opens Google Chrome, waits for you to grant access (sign in /
open the survey), and completes the entire survey automatically using Jev + LLM.

    python survey_filler.py                           # open Chrome -> click Start -> done
    python survey_filler.py https://example.com/form  # open URL -> click Start -> done
    python survey_filler.py --cdp http://localhost:9222
"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
EXTRACT_JS = (HERE / "extract.js").read_text(encoding="utf-8")
APPLY_JS = (HERE / "apply.js").read_text(encoding="utf-8")

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "google/gemma-4-31b-it:free", "OPENROUTER_API_KEY"),
    "kilo": ("https://api.kilo.ai/api/gateway", "kilo-auto/free", None),
    "ollama": ("http://localhost:11434/v1", "llama3.1", None),
    "mock": (None, None, None),
    "jev": ("https://api.typesafe.ai/v1", "jev-latest", "TYPESAFE_API_KEY"),
}

SINGLE_CHOICE_TYPES = {"radio", "select", "listbox", "combobox", "switch", "button_group"}
MULTI_CHOICE_TYPES = {"checkbox", "multiselect", "button_multi"}
SCALE_TYPES = {"slider", "range"}
JEV_TYPES = SINGLE_CHOICE_TYPES | MULTI_CHOICE_TYPES | SCALE_TYPES | {"rank", "bucket"}

DEFAULT_PERSONA = (
    "A thoughtful adult respondent. Gives realistic, internally consistent answers, "
    "moderate opinions, and short natural free-text replies."
)

NEXT_RE = re.compile(
    r"\b(next|continue|proceed|weiter|suivant|siguiente|avanti|pr[oó]ximo|volgende|"
    r"start(\s+survey)?|begin(\s+survey)?|get\s+started|take\s+survey|let'?s\s+go|"
    r"agree\s*(?:&|and)\s*continue)\b|^\s*(ok|okay|>+|→|»)\s*$",
    re.I,
)
SUBMIT_RE = re.compile(r"\b(submit|finish|done|send|complete|absenden|envoyer|enviar|terminer|fertig)\b", re.I)
AVOID_BTN_RE = re.compile(r"\b(back|previous|zurück|retour|cancel|close|skip|reset|clear|reject|decline|cookie)\b", re.I)
DONE_TEXT_RE = re.compile(
    r"\bthank(s| you)\b|response has been recorded|has been (submitted|received)|"
    r"we(?:'ve| have) received|survey (is )?complete|danke|merci|gracias",
    re.I,
)
DONE_URL_RE = re.compile(r"formResponse|thank|complete|confirm|success|finished", re.I)

WELCOME_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Survey Filler — Ready</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 620px; margin: 80px auto; padding: 28px;
         background: #0f172a; color: #e2e8f0; border-radius: 14px; line-height: 1.6; }
  h1 { margin-top: 0; color: #38bdf8; font-size: 24px; }
  code { background: #1e293b; padding: 2px 7px; border-radius: 5px; color: #7dd3fc; }
</style></head><body>
  <h1>Survey Filler is Ready</h1>
  <ol>
    <li>Paste or open your survey URL in Chrome and sign in / grant access if needed.</li>
    <li>Click <code>&#9654; Start Survey</code> in the top-right pill (or press <code>Enter</code> in the terminal).</li>
  </ol>
</body></html>"""

HUD_JS = """([statusText, btnText, tone]) => {
  if (!document.documentElement) return;
  let host = document.getElementById('__sf_hud');
  if (!host) {
    host = document.createElement('div');
    host.id = '__sf_hud';
    host.style.cssText = 'position:fixed;top:14px;right:14px;z-index:2147483647;pointer-events:auto;';
    const root = host.attachShadow({ mode: 'closed' });
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 14px;border-radius:999px;font:600 13px/1.2 system-ui,sans-serif;color:#f8fafc;background:#0f172a;border:1px solid #334155;box-shadow:0 8px 24px rgba(0,0,0,.35);';
    const dot = document.createElement('span');
    dot.style.cssText = 'width:9px;height:9px;border-radius:50%;display:inline-block;flex-shrink:0;';
    const msg = document.createElement('span');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.style.cssText = 'border:0;border-radius:999px;padding:6px 13px;font:600 12px/1 system-ui,sans-serif;background:#22c55e;color:#052e16;cursor:pointer;';
    btn.onclick = e => { e.preventDefault(); e.stopPropagation(); window.__sfGo = 'go'; };
    wrap.append(dot, msg, btn);
    root.appendChild(wrap);
    host.__sfRefs = { dot, msg, btn };
    document.documentElement.appendChild(host);
  }
  const { dot, msg, btn } = host.__sfRefs;
  dot.style.background = ({ ready: '#22c55e', busy: '#38bdf8', pause: '#f59e0b', done: '#4ade80' })[tone] || '#38bdf8';
  msg.textContent = statusText || '';
  btn.textContent = btnText || '';
  btn.style.display = btnText ? 'inline-block' : 'none';
}"""

POPUP_OPTS_JS = """() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const placeholder = /^(select|choose|pick|--+|none|please\\s+(select|choose))\\b/i;
  const seen = new Set(), out = [];
  for (const o of document.querySelectorAll('[role=option], [role=menuitemradio], [role=menuitem], [data-radix-collection-item], [cmdk-item]')) {
    if (o.closest('select') || o.getAttribute('aria-disabled') === 'true') continue;
    const s = getComputedStyle(o), r = o.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0 || r.width <= 0 || r.height <= 0) continue;
    const label = norm(o.innerText || o.textContent || o.dataset.value);
    if (!label || placeholder.test(label) || seen.has(label)) continue;
    seen.add(label);
    out.push({ label, value: o.dataset.value ?? label });
  }
  return out;
}"""

NAV_BTNS_JS = """(usedSfs) => {
  const used = new Set(usedSfs || []);
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  window.__sfN = window.__sfN || 0;
  const stamp = el => el.dataset.sf || (el.dataset.sf = 'e' + (++window.__sfN));
  const ignore = '#__sf_hud, #onetrust-consent-sdk, [id*="cookie" i], [class*="cookie" i], [class*="gdpr" i]';
  const out = [];
  for (const el of document.querySelectorAll('button, [role=button], input[type=submit], input[type=button], a[href], [role=link]')) {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.closest(ignore)) continue;
    if (el.dataset.sf && used.has(el.dataset.sf)) continue;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden' || +s.opacity === 0 || r.width <= 0 || r.height <= 0) continue;
    const label = norm(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title'));
    const maxLen = el.tagName === 'A' && !/btn|button/i.test(el.className) ? 40 : 80;
    if (!label || label.length > maxLen) continue;
    out.push({ sf: stamp(el), label, isSubmitType: (el.getAttribute('type') || '').toLowerCase() === 'submit' });
  }
  return out;
}"""


# --------------------------------------------------------------------------- config & handoff

def load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class StdinWatcher:
    def __init__(self):
        self.q = queue.Queue()
        self._started = False

    def start(self):
        if not self._started and sys.stdin and sys.stdin.isatty():
            self._started = True
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            line = sys.stdin.readline()
            if not line:
                self.q.put("eof")
                break
            self.q.put(line.strip().lower())

    def poll(self):
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None

    def drain(self):
        while self.poll() is not None:
            pass


def update_hud(page, status_text, btn_text=None, tone="busy"):
    try:
        if not page.is_closed():
            page.evaluate(HUD_JS, [status_text, btn_text, tone])
    except PWError:
        pass


def wait_for_access(context, stdin_watcher, status_text, btn_text, terminal_prompt, tone="ready"):
    print(terminal_prompt, flush=True)
    stdin_watcher.start()
    stdin_watcher.drain()
    for pg in context.pages:
        try:
            if not pg.is_closed():
                pg.evaluate("() => { delete window.__sfGo; }")
        except PWError:
            pass

    while True:
        pages = [pg for pg in context.pages if not pg.is_closed()]
        if not pages:
            return "q", None
        for pg in pages:
            update_hud(pg, status_text, btn_text, tone)
            try:
                sig = pg.evaluate("() => { const v = window.__sfGo; delete window.__sfGo; return v || null; }")
                if sig:
                    pg.bring_to_front()
                    return sig, pg
            except PWError:
                pass
        cmd = stdin_watcher.poll()
        if cmd is not None:
            return ("q" if cmd in ("q", "eof") else (cmd or "go")), pages[-1]
        pages[-1].wait_for_timeout(200)


# --------------------------------------------------------------------------- LLM & Jev

class HTTPFail(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def post_json(url, body, api_key=None, retries=4):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < retries:
                wait = 2 ** attempt * 3
                print(f"  API {e.code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise HTTPFail(e.code, f"API error {e.code} from {url}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(3)
                continue
            raise HTTPFail(0, f"Cannot reach {url}: {e}") from None


class LLM:
    def __init__(self, provider, model=None, base_url=None, api_key=None, required=True):
        if provider not in PROVIDERS:
            sys.exit(f"Unknown provider {provider!r}. Choose from: {', '.join(PROVIDERS)}")
        url, default_model, key_env = PROVIDERS[provider]
        self.provider = provider
        self.base_url = (base_url or url or "").rstrip("/")
        self.model = model or default_model
        self.api_key = api_key or (os.environ.get(key_env) if key_env else None)
        if provider == "kilo":
            self.api_key = self.api_key or os.environ.get("KILO_API_KEY") or "anonymous"
        self.ok = not (key_env and not self.api_key)
        if not self.ok and required:
            sys.exit(f"Set {key_env} (in .env or the environment) or pass --api-key. See README.md.")

    def chat_json(self, system, user):
        body = {
            "model": self.model,
            "temperature": 0.7,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        url = f"{self.base_url}/chat/completions"
        try:
            data = post_json(url, body, self.api_key)
        except HTTPFail as e:
            if e.code != 400:
                raise
            body.pop("response_format")
            data = post_json(url, body, self.api_key)
        return parse_json(data["choices"][0]["message"]["content"])


def scale_candidates(q):
    try:
        lo = float(q.get("min") if q.get("min") is not None else 0)
        hi = float(q.get("max") if q.get("max") is not None else 100)
        step = float(q.get("step") or 1) or 1.0
    except ValueError:
        lo, hi, step = 0.0, 100.0, 1.0
    if hi <= lo:
        hi = lo + 10.0
    fmt = lambda v: str(int(round(v))) if float(step).is_integer() and float(lo).is_integer() else f"{v:.4g}"
    total = int(round((hi - lo) / step))
    if 1 <= total <= 12:
        return [fmt(lo + i * step) for i in range(total + 1)]
    vals = []
    for i in range(11):
        snapped = min(hi, max(lo, lo + round(((hi - lo) * (i / 10.0)) / step) * step))
        if not vals or snapped != vals[-1]:
            vals.append(snapped)
    return [fmt(v) for v in vals]


class Jev:
    def __init__(self, model=None, api_key=None, base_url=None):
        url, default_model, key_env = PROVIDERS["jev"]
        self.url = (base_url or url).rstrip("/") + "/systemone"
        self.model = model or default_model
        self.api_key = api_key or os.environ.get(key_env)
        if not self.api_key:
            sys.exit(f"Set {key_env} (key from https://console.typesafe.ai/keys) or pass --api-key.")

    def decide(self, persona, history, questions):
        state = f"Survey respondent persona:\n{persona}\n\n" + ("Their earlier answers:\n" + "\n".join(history[-60:]) if history else "")
        jq, keymap = {}, {}
        for q in questions:
            t = q["type"]
            if t in MULTI_CHOICE_TYPES or t == "rank":
                verb = "rank near the top" if t == "rank" else "select"
                for o in q["options"]:
                    jq[o["id"]] = {"type": "noul", "instructions": f'Would this respondent {verb} "{o["label"]}" for: {q["text"]}'}
            elif t == "bucket":
                criteria, opts = self._build_criteria(q["options"])
                keymap[q["id"]] = opts
                for it in q.get("items", []):
                    jq[it["id"]] = {"type": "choice", "instructions": f'Which category would this respondent place "{it["label"]}" into for: {q["text"]}', "criteria": criteria}
            elif t in SCALE_TYPES:
                vals = scale_candidates(q)
                criteria, opts = self._build_criteria([{"id": v, "label": v} for v in vals])
                keymap[q["id"]] = opts
                jq[q["id"]] = {"type": "choice", "instructions": f"Which value from {vals[0]} to {vals[-1]} would this respondent choose for: {q['text']}", "criteria": criteria}
            else:
                criteria, opts = self._build_criteria(q["options"])
                keymap[q["id"]] = opts
                jq[q["id"]] = {"type": "choice", "instructions": f"Which answer would this respondent give to: {q['text']}", "criteria": criteria}

        answers = {}
        items = list(jq.items())
        for i in range(0, len(items), 50):
            data = post_json(self.url, {"model": self.model, "state": state, "questions": dict(items[i:i + 50])}, self.api_key)
            answers.update(data.get("answers", {}))

        out = {}
        for q in questions:
            t = q["type"]
            if t in MULTI_CHOICE_TYPES or t == "rank":
                probs = {o["id"]: (answers.get(o["id"]) or {}).get("noul", 0) for o in q["options"]}
                if t == "rank":
                    out[q["id"]] = sorted(probs, key=probs.get, reverse=True)
                else:
                    chosen = [oid for oid, p in probs.items() if p >= 0.5]
                    out[q["id"]] = chosen or ([max(probs, key=probs.get)] if q["required"] and probs else [])
            elif t == "bucket":
                opts = keymap.get(q["id"], {})
                mapping = {it["id"]: opts[c]["id"] for it in q.get("items", []) if (c := (answers.get(it["id"]) or {}).get("choice")) in opts}
                if mapping:
                    out[q["id"]] = mapping
            elif q["id"] in answers and (picked := keymap[q["id"]].get(answers[q["id"]].get("choice"))):
                out[q["id"]] = picked["id"]
        return out

    @staticmethod
    def _build_criteria(options):
        criteria, opts = {}, {}
        for o in options:
            key = o["label"][:200] or o["id"]
            while key in criteria:
                key += " "
            criteria[key] = f"The respondent's answer is: {o['label']}"
            opts[key] = o
        return criteria, opts


def parse_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError(f"LLM did not return JSON: {text[:300]}")
        return json.loads(m.group(0))


SYSTEM_PROMPT = """You fill out web surveys and forms on behalf of the respondent described in the persona.
Answer every question the way that person would, consistent with their earlier answers.
If a survey question explicitly instructs selecting a specific answer (e.g. an attention check), follow that instruction.

Return ONLY a JSON object: {"answers": {"<question id>": <answer>, ...}}
- radio / select / listbox / combobox / switch / button_group: one option id string, e.g. "q3.2"
- checkbox / multiselect / button_multi: a list of option ids that apply, e.g. ["q4.1","q4.3"] (at least one if required)
- rank: a list of ALL option ids in order, most preferred first
- bucket: an object mapping EVERY item id in "items" to a category option id in "options", e.g. {"q5.i1": "q5.1"}
- slider / range / number: a number within min/max aligned to step
- text-like types: valid plausible string (date: YYYY-MM-DD, time: HH:MM, month: YYYY-MM). Respect minlength, maxlength, pattern, and error hints."""


def mock_answers(questions):
    out = {}
    for q in questions:
        t = q["type"]
        if t == "bucket" and q.get("items") and q["options"]:
            out[q["id"]] = {it["id"]: q["options"][i % len(q["options"])]["id"] for i, it in enumerate(q["items"])}
        elif q["options"]:
            ids = [o["id"] for o in q["options"]]
            if t == "rank":
                out[q["id"]] = ids[::-1]
            elif t in MULTI_CHOICE_TYPES:
                out[q["id"]] = ids[:1] if t == "checkbox" else ids[:min(2, len(ids))]
            elif t == "switch":
                out[q["id"]] = ids[0]
            else:
                out[q["id"]] = ids[min(1, len(ids) - 1)] if t in ("radio", "button_group") else ids[0]
        elif t in ("number", "range", "slider"):
            vals = scale_candidates(q)
            out[q["id"]] = vals[len(vals) // 2] if t in SCALE_TYPES else str(q.get("min") or 25)
        else:
            out[q["id"]] = {
                "email": "test@example.com", "date": "2000-01-15", "time": "09:30",
                "tel": "5551234567", "url": "https://example.com",
                "month": "2024-05", "week": "2024-W20", "datetime-local": "2024-05-15T09:30",
            }.get(t, "Test answer")
    return out


def can_jev(q):
    return True if q["type"] in SCALE_TYPES else bool(q.get("items") and q.get("options")) if q["type"] == "bucket" else (q["type"] in JEV_TYPES and bool(q.get("options")))


def ask(jev, llm, persona, history, questions):
    if not jev:
        return ask_llm(llm, persona, history, questions)
    choice = [q for q in questions if can_jev(q)]
    text = [q for q in questions if q not in choice]
    answers = jev.decide(persona, history, choice) if choice else {}
    if text:
        if llm and llm.ok:
            answers.update(ask_llm(llm, persona, history, text))
        else:
            print(f"  ({len(text)} free-text questions skipped: set a --text-provider key)")
    return answers


def ask_llm(llm, persona, history, questions):
    if llm.provider == "mock":
        return mock_answers(questions)
    answers = {}
    for i in range(0, len(questions), 30):
        payload = [{k: v for k, v in {
            "id": q["id"], "type": q["type"], "question": q["text"], "required": q["required"] or None,
            "items": [{"id": it["id"], "label": it["label"]} for it in q.get("items", [])] or None,
            "options": [{"id": o["id"], "label": o["label"]} for o in q["options"]] or None,
            "min": q.get("min"), "max": q.get("max"), "step": q.get("step"),
            "minlength": q.get("minlength"), "maxlength": q.get("maxlength"),
            "pattern": q.get("pattern"), "placeholder": q.get("placeholder"), "error": q.get("error"),
        }.items() if v is not None} for q in questions[i:i + 30]]
        user = f"PERSONA:\n{persona}\n\n" + ("EARLIER ANSWERS:\n" + "\n".join(history[-60:]) + "\n\n" if history else "") + "QUESTIONS:\n" + json.dumps(payload, ensure_ascii=False)
        data = llm.chat_json(SYSTEM_PROMPT, user)
        answers.update(data.get("answers", data))
    return answers


# --------------------------------------------------------------------------- page scanning & actions

def expand_combobox(frame, q):
    loc = frame.locator(f'[data-sf="{q["sf"]}"]').first
    try:
        is_input = loc.evaluate("e => e.tagName === 'INPUT'")
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.click(timeout=2000)
        frame.wait_for_timeout(250)
        opts = frame.evaluate(POPUP_OPTS_JS) or (frame.page.main_frame.evaluate(POPUP_OPTS_JS) if frame != frame.page.main_frame else [])
        frame.page.keyboard.press("Escape")
        frame.wait_for_timeout(100)
        if opts:
            q["options"] = opts
            return True
        if is_input:
            q["type"] = "autocomplete"
            return True
    except PWError:
        pass
    return False


def scan(page):
    questions = []
    for frame in page.frames:
        try:
            found = frame.evaluate(EXTRACT_JS)
        except PWError:
            continue
        for q in found:
            if q["type"] == "combobox" and not q["options"] and not expand_combobox(frame, q):
                continue
            q["frame"] = frame
            q["id"] = f"q{len(questions) + 1}"
            for j, o in enumerate(q["options"], 1):
                o["id"] = f"{q['id']}.{j}"
            for k, it in enumerate(q.get("items", []), 1):
                it["id"] = f"{q['id']}.i{k}"
            questions.append(q)
    return questions


def clamp_scale(q, answer):
    try:
        val = float(str(answer).strip())
        lo = float(q.get("min") if q.get("min") is not None else 0)
        hi = float(q.get("max") if q.get("max") is not None else 100)
        step = float(q.get("step") or 1) or 1.0
        val = min(hi, max(lo, lo + round((val - lo) / step) * step))
        s = str(int(round(val))) if float(step).is_integer() and float(lo).is_integer() else f"{val:.4g}"
        return val, lo, hi, step, s
    except ValueError:
        return 50.0, 0.0, 100.0, 1.0, "50"


def plan_actions(q, answer):
    t = q["type"]
    opts = {o["id"]: o for o in q["options"]}
    by_label = {o["label"].lower(): o for o in q["options"]}

    def pick(a):
        if a is None:
            return None
        s = str(a).strip()
        if s in opts:
            return opts[s]
        low = s.lower()
        return by_label.get(low) or next((o for lbl, o in by_label.items() if low and (low in lbl or lbl in low)), None)

    if t == "bucket":
        items_by_id = {it["id"]: it for it in q.get("items", [])}
        items_by_lbl = {it["label"].lower(): it for it in q.get("items", [])}
        pairs = []
        for k, v in (answer.items() if isinstance(answer, dict) else ()):
            it = items_by_id.get(str(k).strip()) or items_by_lbl.get(str(k).strip().lower())
            b = pick(v)
            if it and b:
                pairs.append({"item": it["sf"], "bucket": b["sf"], "item_label": it["label"], "bucket_label": b["label"]})
        assigned = {p["item"] for p in pairs}
        for it in (q.get("items", []) if q["options"] else ()):
            if it["sf"] not in assigned:
                b = q["options"][0]
                pairs.append({"item": it["sf"], "bucket": b["sf"], "item_label": it["label"], "bucket_label": b["label"]})
        return [(q["sf"], "bucket", pairs)] if pairs else []

    many = answer if isinstance(answer, list) else [answer]
    if t == "multiselect":
        return [(q["sf"], "multiselect", [o for o in map(pick, many) if o])]
    if t == "rank":
        order = []
        for o in map(pick, many):
            if o and o not in order:
                order.append(o)
        return [(q["sf"], "rank", order + [o for o in q["options"] if o not in order])]
    if t == "checkbox":
        chosen = {o["id"] for o in map(pick, many) if o}
        return [(o["sf"], "check" if o["id"] in chosen else "uncheck", None) for o in q["options"]]
    if t == "button_multi":
        chosen = {o["id"] for o in map(pick, many) if o}
        return [(o["sf"], "button_click" if o["id"] in chosen else "button_unclick", q["sf"]) for o in q["options"]]
    if t in ("radio", "button_group"):
        o = pick(many[0] if many else None)
        return [(o["sf"], "check" if t == "radio" else "button_click", q.get("sf"))] if o else []
    if t in ("select", "listbox", "combobox", "switch"):
        o = pick(many[0] if many else None)
        return [(q["sf"], t, o)] if o else []
    if t == "slider":
        val, lo, hi, step, shown = clamp_scale(q, many[0] if many else answer)
        return [(q["sf"], "slider", {"value": val, "min": lo, "max": hi, "step": step, "shown": shown})]
    if t == "range":
        return [(q["sf"], "text", clamp_scale(q, many[0] if many else answer)[4])]
    if t == "autocomplete":
        return [(q["sf"], "autocomplete", str(many[0] if isinstance(answer, list) and many else answer))]
    return [(q["sf"], "text", ", ".join(map(str, answer)) if isinstance(answer, list) else str(answer))]


def apply_answers(questions, answers, history):
    by_frame, labels = {}, {}
    for q in questions:
        if q["id"] not in answers:
            continue
        steps = plan_actions(q, answers[q["id"]])
        if not steps:
            continue
        for sf_id, op, val in steps:
            value = (val["value"] if op in ("select", "switch") else [o["value"] for o in val] if op == "multiselect"
                     else [o["sf"] for o in val] if op == "rank" else val)
            by_frame.setdefault(q["frame"], []).append({"sf": sf_id, "op": op, "value": value})

        t = q["type"]
        if t in ("select", "listbox", "combobox", "switch"):
            shown = [steps[0][2]["label"]]
        elif t in ("multiselect", "rank"):
            shown = [" > ".join(o["label"] for o in steps[0][2])] if t == "rank" else [o["label"] for o in steps[0][2]]
        elif t == "bucket":
            shown = [f"{p['item_label']} -> {p['bucket_label']}" for p in steps[0][2]]
        elif t == "slider":
            shown = [steps[0][2]["shown"]]
        elif q["options"]:
            picked = {s[0] for s in steps if s[1] in ("check", "button_click")}
            shown = [o["label"] for o in q["options"] if o.get("sf") in picked]
        else:
            shown = [str(steps[0][2])]
        labels[q["id"]] = ", ".join(shown)
        history.append(f"- {q['text'][:150]} => {labels[q['id']][:200]}")
        print(f"  {q['id']} [{t}] {q['text'][:70]!r}\n      -> {labels[q['id']][:100]}")

    needs_mouse = {"listbox", "combobox", "rank", "bucket", "slider", "button_click", "button_unclick", "autocomplete"}
    filled = failed = 0
    for frame, actions in by_frame.items():
        fast = [a for a in actions if a["op"] not in needs_mouse]
        slow_ids = set(frame.evaluate(APPLY_JS, fast)) if fast else set()
        slow = [a for a in actions if a["op"] in needs_mouse or a["sf"] in slow_ids]
        filled += len(actions) - len(slow)
        for a in slow:
            ok = playwright_apply(frame, a)
            filled += ok
            failed += not ok
    return filled, failed


def select_popup_option(frame, opt):
    for tf in ([frame, frame.page.main_frame] if frame != frame.page.main_frame else [frame]):
        sel = f'[role=option][data-value={json.dumps(opt["value"])}]:visible, [role=menuitemradio][data-value={json.dumps(opt["value"])}]:visible'
        target = tf.locator(sel)
        if not target.count():
            target = tf.locator("[role=option]:visible, [role=menuitemradio]:visible, [role=menuitem]:visible").filter(has_text=opt["label"])
        if target.count():
            target.first.click(timeout=3000)
            frame.wait_for_timeout(200)
            return True
    return False


def apply_slider(frame, loc, info):
    val, lo, hi, step = info["value"], info["min"], info["max"], info["step"]
    frac = 0.0 if hi <= lo else max(0.0, min(1.0, (val - lo) / (hi - lo)))
    loc.scroll_into_view_if_needed(timeout=2000)
    box = loc.evaluate("""el => {
      const r = el.getBoundingClientRect();
      if (r.width >= 48) return { x: r.x, y: r.y, width: r.width, height: r.height };
      for (let p = el.parentElement, d = 0; p && d < 4; d++, p = p.parentElement) {
        const pr = p.getBoundingClientRect();
        if (pr.width >= 80 && pr.height <= 80) return { x: pr.x, y: pr.y, width: pr.width, height: pr.height };
      }
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    }""")
    if box and box["width"] > 4:
        frame.page.mouse.click(box["x"] + max(2.0, min(box["width"] - 2.0, box["width"] * frac)), box["y"] + box["height"] / 2.0)
        frame.wait_for_timeout(120)
    cur_raw = loc.get_attribute("aria-valuenow")
    if cur_raw is not None:
        try:
            diff = int(round((val - float(cur_raw)) / step))
            if 0 < abs(diff) <= 50:
                loc.focus(timeout=1500)
                for _ in range(abs(diff)):
                    frame.page.keyboard.press("ArrowRight" if diff > 0 else "ArrowLeft")
        except ValueError:
            pass
    loc.evaluate("e => { e.dataset.sfTouched = '1'; }")
    return True


def playwright_apply(frame, a):
    loc = frame.locator(f'[data-sf="{a["sf"]}"]').first
    try:
        op = a["op"]
        if op == "text":
            if loc.evaluate("e => e.isContentEditable"):
                loc.click(timeout=3000)
                frame.page.keyboard.press("Control+A")
                frame.page.keyboard.type(a["value"])
            else:
                loc.fill(a["value"], timeout=3000)
                loc.evaluate("e => { e.dataset.sfTouched = '1'; }")
        elif op == "autocomplete":
            loc.click(timeout=3000)
            loc.fill(a["value"], timeout=3000)
            frame.wait_for_timeout(350)
            opt = frame.locator("[role=option]:visible").first
            if opt.count():
                opt.click(timeout=2000)
            loc.evaluate("e => { e.dataset.sfTouched = '1'; }")
        elif op in ("check", "uncheck"):
            want = op == "check"
            if loc.get_attribute("role"):
                if (loc.get_attribute("aria-checked") == "true") != want:
                    loc.click(timeout=3000)
            else:
                loc.set_checked(want, timeout=3000, force=True)
        elif op == "switch":
            if (loc.get_attribute("aria-checked") == "true") != (str(a["value"]) == "true"):
                loc.click(timeout=3000)
            loc.evaluate("e => { e.dataset.sfTouched = '1'; }")
        elif op in ("button_click", "button_unclick"):
            want = op == "button_click"
            pressed = loc.get_attribute("aria-pressed")
            if (want and pressed != "true") or (not want and pressed == "true"):
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=3000)
            if a.get("value"):
                grp = frame.locator(f'[data-sf="{a["value"]}"]').first
                if grp.count():
                    grp.evaluate("e => { e.dataset.sfTouched = '1'; }")
        elif op in ("select", "multiselect"):
            loc.select_option(value=a["value"], timeout=3000)
            loc.evaluate("e => { e.dataset.sfTouched = '1'; }")
        elif op == "rank":
            ok = rank_by_dragging(frame, loc, a["value"])
            if ok:
                loc.evaluate("e => { e.dataset.sfTouched = '1'; }")
            return ok
        elif op == "bucket":
            for pair in a["value"]:
                drag(frame, frame.locator(f'[data-sf="{pair["item"]}"]').first, frame.locator(f'[data-sf="{pair["bucket"]}"]').first, 0.5)
                frame.wait_for_timeout(200)
        elif op == "slider":
            return apply_slider(frame, loc, a["value"])
        elif op in ("listbox", "combobox"):
            opt = a["value"]
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=3000)
            frame.wait_for_timeout(300)
            if op == "combobox" and loc.evaluate("e => e.tagName === 'INPUT'"):
                loc.fill(opt["label"], timeout=2000)
                frame.wait_for_timeout(250)
            if not select_popup_option(frame, opt):
                frame.page.keyboard.press("Escape")
                return False
            loc.evaluate("e => { e.dataset.sfTouched = '1'; }")
        return True
    except PWError as e:
        print(f"    ! could not fill {a['sf']} ({a['op']}): {str(e).splitlines()[0][:120]}")
        return False


ORDER_JS = "c => Array.from(c.children).filter(e => e.dataset.sf).map(e => e.dataset.sf)"


def drag(frame, src, dst, y_frac):
    page = frame.page
    src.scroll_into_view_if_needed()
    dst.scroll_into_view_if_needed()
    if src.get_attribute("draggable") == "true":
        box = dst.bounding_box()
        src.drag_to(dst, target_position={"x": box["width"] / 2, "y": max(2, box["height"] * y_frac)}, timeout=3000)
        return
    s, d = src.bounding_box(), dst.bounding_box()
    page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2)
    page.mouse.down()
    page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2 - 5, steps=3)
    page.mouse.move(d["x"] + d["width"] / 2, d["y"] + max(2, d["height"] * y_frac), steps=20)
    page.wait_for_timeout(150)
    page.mouse.up()


def rank_by_dragging(frame, container, order):
    for i, sf_id in enumerate(order):
        for y_frac in (0.2, 0.5, 0.05):
            cur = container.evaluate(ORDER_JS)
            if i >= len(cur) or cur[i] == sf_id:
                break
            drag(frame, frame.locator(f'[data-sf="{sf_id}"]'), frame.locator(f'[data-sf="{cur[i]}"]'), y_frac)
            frame.wait_for_timeout(250)
    final = container.evaluate(ORDER_JS)
    if final != order:
        print(f"    ! ranking ended as {final}, wanted {order}")
    return final == order


# --------------------------------------------------------------------------- navigation

def extract_nav_buttons(page, questions=()):
    used_sfs = [q["sf"] for q in questions if q.get("sf")] + [
        o["sf"] for q in questions for o in q.get("options", []) if o.get("sf")
    ]
    candidates = []
    for frame in page.frames:
        try:
            found = frame.evaluate(NAV_BTNS_JS, used_sfs)
        except PWError:
            continue
        for b in found:
            if not AVOID_BTN_RE.search(b["label"]):
                b["id"] = f"b{len(candidates) + 1}"
                b["frame"] = frame
                candidates.append(b)
    return candidates


def find_button(page, pattern, questions=()):
    for b in extract_nav_buttons(page, questions):
        if pattern.search(b["label"]):
            return b["frame"].locator(f'[data-sf="{b["sf"]}"]').first
    return None


def agent_pick_button(page, questions, jev, llm, allow_submit=True):
    candidates = extract_nav_buttons(page, questions)
    if not allow_submit:
        candidates = [b for b in candidates if not b["isSubmitType"] and not SUBMIT_RE.search(b["label"])]
    if not candidates:
        return None, False, ""

    by_id = {b["id"]: b for b in candidates}
    chosen, is_submit = None, False
    if jev:
        synth = [{"id": b["id"], "label": b["label"]} for b in candidates] + [{"id": "NONE", "label": "NONE"}]
        criteria, opts = Jev._build_criteria(synth)
        data = post_json(jev.url, {
            "model": jev.model,
            "state": f"Current page title: {page.title()}",
            "questions": {"nav": {"type": "choice", "instructions": "All questions on this screen are answered. Which button moves forward or submits?", "criteria": criteria}},
        }, jev.api_key)
        pick_opt = opts.get(((data.get("answers") or {}).get("nav") or {}).get("choice"))
        if pick_opt and pick_opt["id"] in by_id:
            chosen = by_id[pick_opt["id"]]
            is_submit = chosen["isSubmitType"] or bool(SUBMIT_RE.search(chosen["label"]))
    elif llm and llm.provider != "mock" and llm.ok:
        prompt = json.dumps([{"id": b["id"], "label": b["label"], "submit_type": b["isSubmitType"]} for b in candidates], ensure_ascii=False)
        res = llm.chat_json(
            'Pick the button that advances to the next step or submits the survey, or null if none applies. Return ONLY JSON: {"button": "<id or null>", "is_submit": true|false}',
            f"Page title: {page.title()}\nButtons: {prompt}",
        )
        if res.get("button") in by_id:
            chosen = by_id[res["button"]]
            is_submit = bool(res.get("is_submit")) or chosen["isSubmitType"]
    elif llm and llm.provider == "mock" and allow_submit:
        sub_cands = [b for b in candidates if b["isSubmitType"]]
        if sub_cands:
            chosen, is_submit = sub_cands[0], True

    if not chosen:
        return None, False, ""
    return chosen["frame"].locator(f'[data-sf="{chosen["sf"]}"]').first, is_submit, chosen["label"]


def settle(page):
    page.wait_for_load_state("domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWError:
        pass


def submit_and_confirm(page, button):
    url_before = page.url
    button.click()
    for _ in range(10):
        page.wait_for_timeout(500)
        try:
            text = page.inner_text("body", timeout=2000)
        except PWError:
            continue
        if DONE_TEXT_RE.search(text) or (page.url != url_before and DONE_URL_RE.search(page.url)):
            print("Submitted - confirmation page detected.")
            return True
    print("Clicked Submit, but no confirmation appeared. Check the browser for errors.")
    return False


def q_key(q):
    return q.get("sf") or tuple(o.get("sf") for o in q.get("options", []))


def fill_page(page, llm, persona, history, overwrite, jev=None, retry_errors=False, show_hud=False):
    settle(page)
    if show_hud:
        update_hud(page, "Filling survey page...", None, "busy")
    attempted = set()
    all_questions = []
    for pass_num in range(4):
        questions = scan(page)
        all_questions = questions
        if retry_errors:
            todo = [q for q in questions if q.get("error") or (q["required"] and not q["answered"])]
        else:
            todo = [q for q in questions if ((overwrite if pass_num == 0 else False) or not q["answered"]) and q_key(q) not in attempted]

        if pass_num == 0:
            print(f"\nPage: {page.title()[:80]!r} - {len(questions)} questions found, {len(todo)} to fill")
        elif todo:
            print(f"  +{len(todo)} conditional follow-up question(s) appeared")
        if not todo:
            break
        for q in todo:
            attempted.add(q_key(q))
        t0 = time.time()
        answers = ask(jev, llm, persona, history, todo)
        t1 = time.time()
        filled, failed = apply_answers(todo, answers, history)
        print(f"  filled {filled} controls ({failed} failed) - LLM {t1 - t0:.1f}s, fill {time.time() - t1:.2f}s")
        if retry_errors:
            break
        page.wait_for_timeout(350)
    return all_questions


def page_signature(page, questions):
    try:
        heading = page.evaluate("() => (document.querySelector('h1,h2,[role=heading]')?.innerText || '').slice(0, 80)")
    except PWError:
        heading = ""
    return (page.url, heading, tuple(q["text"][:60] for q in questions))


def launch_chrome(p, profile_dir: Path, headless: bool):
    try:
        return p.chromium.launch_persistent_context(str(profile_dir), channel="chrome", headless=headless, no_viewport=True)
    except PWError:
        return p.chromium.launch_persistent_context(str(profile_dir), headless=headless, no_viewport=True)


# --------------------------------------------------------------------------- main

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description="Open Chrome, wait for your access handoff, and complete the survey.")
    ap.add_argument("url", nargs="?", help="survey URL (omit to open/paste inside Chrome)")
    text_default = "groq" if os.environ.get("GROQ_API_KEY") else "kilo"
    auto_provider = "jev" if os.environ.get("TYPESAFE_API_KEY") else text_default
    ap.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", auto_provider), choices=list(PROVIDERS))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL"))
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL"))
    ap.add_argument("--api-key")
    ap.add_argument("--text-provider", default=os.environ.get("TEXT_PROVIDER", text_default), choices=[p for p in PROVIDERS if p != "jev"])
    ap.add_argument("--text-model", default=os.environ.get("TEXT_MODEL"))
    ap.add_argument("--persona")
    ap.add_argument("--persona-file")
    ap.add_argument("--cdp", help="attach to a running Chrome, e.g. http://localhost:9222")
    ap.add_argument("--auto", action="store_true", help="start immediately without waiting for initial Start click")
    ap.add_argument("--submit", action="store_true", help="click final Submit (enabled by default)")
    ap.add_argument("--no-submit", action="store_true", help="stop right before clicking final Submit")
    ap.add_argument("--manual", action="store_true", help="pause after each page instead of auto-completing")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()

    persona = args.persona or (Path(args.persona_file).read_text(encoding="utf-8") if args.persona_file else None)
    if not persona and (HERE / "persona.txt").exists():
        persona = (HERE / "persona.txt").read_text(encoding="utf-8")
    persona = persona or DEFAULT_PERSONA

    jev = llm = None
    if not args.dump:
        if args.provider == "jev":
            jev = Jev(args.model, args.api_key, args.base_url)
            llm = LLM(args.text_provider, args.text_model, required=False)
        else:
            llm = LLM(args.provider, args.model, args.base_url, args.api_key)

    history = []
    stdin_watcher = StdinWatcher()
    show_hud = not args.headless and not args.dump
    want_auto = not args.manual
    do_submit = (args.submit or want_auto) and not args.no_submit

    with sync_playwright() as p:
        if args.cdp:
            browser = p.chromium.connect_over_cdp(args.cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[-1] if context.pages else context.new_page()
        else:
            context = launch_chrome(p, HERE / ".browser-profile", args.headless)
            page = context.pages[0] if context.pages else context.new_page()

        if args.url:
            page.goto(args.url)
        elif not args.cdp and not args.headless and page.url in ("about:blank", "chrome://newtab/"):
            page.set_content(WELCOME_HTML)

        active_page = [page]

        def current():
            pages = [pg for pg in context.pages if not pg.is_closed()]
            if not pages:
                return None
            return active_page[0] if (active_page[0] in pages and len(pages) == 1) else pages[-1]

        if args.dump:
            if not args.url:
                input("Open the survey in Chrome, then press Enter...")
            pg = current()
            for q in (scan(pg) if pg else []):
                q.pop("frame")
                print(json.dumps(q, ensure_ascii=False))
            return

        if not args.headless and (not args.url or not args.auto):
            cmd, chosen = wait_for_access(
                context, stdin_watcher,
                "Survey Filler Ready — give access, then click Start", "▶ Start Survey",
                "Chrome is open. Sign in / open your survey, then click [▶ Start Survey] in Chrome (or press [Enter], q = quit)...",
                "ready",
            )
            if cmd == "q" or chosen is None:
                if not args.cdp:
                    context.close()
                return
            active_page[0] = chosen

        run_auto = want_auto
        last_sig = retried_sig = None

        while True:
            page = current()
            if page is None:
                break
            try:
                qs = fill_page(page, llm, persona, history, args.overwrite, jev, show_hud=show_hud)
            except RuntimeError as e:
                print(f"  ! {e}")
                qs = []

            if run_auto:
                sig = page_signature(page, qs)
                nxt = find_button(page, NEXT_RE, qs)
                if nxt and sig != last_sig:
                    last_sig = sig
                    print("  -> clicking Next")
                    if show_hud:
                        update_hud(page, "Advancing to next page...", None, "busy")
                    nxt.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(1500)
                    continue
                if nxt and sig != retried_sig:
                    retried_sig = sig
                    print("  -> page stayed put; retrying errored fields once...")
                    fill_page(page, llm, persona, history, overwrite=True, jev=jev, retry_errors=True, show_hud=show_hud)
                    nxt = find_button(page, NEXT_RE, qs)
                    if nxt:
                        nxt.click()
                        page.wait_for_load_state("domcontentloaded")
                        page.wait_for_timeout(1500)
                        nxt_pg = current()
                        if nxt_pg and page_signature(nxt_pg, scan(nxt_pg)) != sig:
                            continue

                sub = find_button(page, SUBMIT_RE, qs) if (not nxt and do_submit) else None
                btn_name = "Submit"
                if not nxt and not sub and sig != last_sig:
                    btn_loc, is_sub, lbl = agent_pick_button(page, qs, jev, llm, allow_submit=do_submit)
                    if btn_loc is not None:
                        if is_sub:
                            sub, btn_name = btn_loc, lbl
                        else:
                            last_sig = sig
                            print(f"  -> agent clicking {lbl!r}")
                            if show_hud:
                                update_hud(page, f"Clicking {lbl[:24]}...", None, "busy")
                            btn_loc.click()
                            page.wait_for_load_state("domcontentloaded")
                            page.wait_for_timeout(1500)
                            continue
                if sub:
                    print(f"  -> clicking {btn_name}")
                    if show_hud:
                        update_hud(page, "Submitting survey...", None, "busy")
                    if submit_and_confirm(page, sub):
                        if args.headless or args.auto:
                            break
                        cmd, chosen = wait_for_access(
                            context, stdin_watcher, "Survey Completed!", "▶ Fill Another",
                            "\nSurvey completed! Open another survey and click [▶ Fill Another], or press q + Enter to exit...",
                            "done",
                        )
                        if cmd == "q" or chosen is None:
                            break
                        active_page[0] = chosen
                        history.clear()
                        last_sig = retried_sig = None
                        continue
                if args.headless:
                    break
                run_auto = False

            cmd, chosen = wait_for_access(
                context, stdin_watcher, "Paused — fix/advance page, then click Continue", "▶ Continue",
                "\nClick [▶ Continue] in Chrome or type: [Enter] continue   n = Next   s = Submit   q = quit > ",
                "pause",
            )
            if cmd == "q" or chosen is None:
                break
            active_page[0] = chosen
            if cmd in ("n", "s"):
                btn = find_button(chosen, NEXT_RE if cmd == "n" else SUBMIT_RE)
                if btn and cmd == "s":
                    submit_and_confirm(chosen, btn)
                elif btn:
                    btn.click()
                    chosen.wait_for_timeout(1500)
            else:
                run_auto = want_auto
                last_sig = None

        if not args.cdp:
            context.close()


if __name__ == "__main__":
    main()
