"""
Survey Filler - fills web surveys/forms with answers from a free LLM.

Opens a browser (or attaches to your running Chrome), reads every question on
the current page with in-page JavaScript, asks an LLM to answer them as the
persona you describe, then fills them in (fast JS path, Playwright fallback).

    python survey_filler.py https://example.com/survey
    python survey_filler.py --cdp http://localhost:9222      # use your own Chrome
    python survey_filler.py URL --auto                        # also click "Next"
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
EXTRACT_JS = (HERE / "extract.js").read_text(encoding="utf-8")
APPLY_JS = (HERE / "apply.js").read_text(encoding="utf-8")

# Free (or free-tier) OpenAI-compatible endpoints. Override with --model / --base-url.
PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "google/gemma-4-31b-it:free", "OPENROUTER_API_KEY"),
    # Free models with no signup or key (about 200 requests/hour per IP).
    "kilo": ("https://api.kilo.ai/api/gateway", "kilo-auto/free", None),
    "ollama": ("http://localhost:11434/v1", "llama3.1", None),
    "mock": (None, None, None),  # no LLM: picks the first option, for testing
    # TypeSafe's Jev decision model: answers choice questions only, so free text
    # goes to --text-provider. https://docs.typesafe.ai
    "jev": ("https://api.typesafe.ai/v1", "jev-latest", "TYPESAFE_API_KEY"),
}
CHOICE_TYPES = {"radio", "select", "listbox", "checkbox", "multiselect", "rank"}

DEFAULT_PERSONA = (
    "A thoughtful adult respondent. Gives realistic, internally consistent answers, "
    "moderate opinions, and short natural free-text replies."
)

NEXT_RE = re.compile(r"\b(next|continue|weiter|suivant|siguiente|avanti|pr[oó]ximo|volgende|proceed)\b|^\s*(>+|→|»)\s*$", re.I)
SUBMIT_RE = re.compile(r"\b(submit|finish|done|send|complete|absenden|envoyer|enviar|terminer|fertig)\b", re.I)
# Signs that a submit really went through (thank-you page or confirmation URL).
DONE_TEXT_RE = re.compile(r"\bthank(s| you)\b|response has been recorded|has been (submitted|received)|"
                          r"we(?:'ve| have) received|survey (is )?complete|danke|merci|gracias", re.I)
DONE_URL_RE = re.compile(r"formResponse|thank|complete|confirm|success|finished", re.I)

TEXT_OPS = {"text", "textarea", "email", "number", "tel", "url", "date", "time",
            "datetime-local", "month", "week", "range", "contenteditable"}


# --------------------------------------------------------------------------- config

def load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- LLM

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
            body.pop("response_format")  # some models reject JSON mode
            data = post_json(url, body, self.api_key)
        return parse_json(data["choices"][0]["message"]["content"])


class Jev:
    """TypeSafe Jev: typed decisions instead of generated text.

    radio/select/listbox -> one Choice question (criteria = the options)
    checkbox             -> one Noul (probability it applies) per option
    """

    def __init__(self, model=None, api_key=None, base_url=None):
        url, default_model, key_env = PROVIDERS["jev"]
        self.url = (base_url or url).rstrip("/") + "/systemone"
        self.model = model or default_model
        self.api_key = api_key or os.environ.get(key_env)
        if not self.api_key:
            sys.exit(f"Set {key_env} (key from https://console.typesafe.ai/keys) or pass --api-key.")

    def decide(self, persona, history, questions):
        state = (f"Survey respondent persona:\n{persona}\n\n"
                 + ("Their earlier answers:\n" + "\n".join(history[-60:]) if history else ""))
        jq, keymap = {}, {}  # jev question id -> (question, {criteria key -> option})
        for q in questions:
            if q["type"] in ("checkbox", "multiselect", "rank"):
                # Jev picks one answer per Choice, so multi-answer and ranking
                # questions get one probability per option instead.
                verb = "rank near the top" if q["type"] == "rank" else "select"
                for o in q["options"]:
                    jq[o["id"]] = {"type": "noul",
                                   "instructions": f"Would this respondent {verb} \"{o['label']}\" "
                                                   f"for the survey question: {q['text']}"}
                continue
            criteria, opts = {}, {}
            for o in q["options"]:
                key = o["label"][:200] or o["id"]
                while key in criteria:
                    key += " "
                criteria[key] = f"The respondent's answer is: {o['label']}"
                opts[key] = o
            jq[q["id"]] = {"type": "choice",
                           "instructions": f"Which answer would this respondent give to the survey question: {q['text']}",
                           "criteria": criteria}
            keymap[q["id"]] = opts

        answers = {}
        # Keep requests modest; a single request may mix question types.
        items = list(jq.items())
        for i in range(0, len(items), 50):
            data = post_json(self.url, {"model": self.model, "state": state, "questions": dict(items[i:i + 50])},
                             self.api_key)
            for qid, a in data.get("answers", {}).items():
                answers[qid] = a

        out = {}
        for q in questions:
            if q["type"] in ("checkbox", "multiselect", "rank"):
                probs = {o["id"]: (answers.get(o["id"]) or {}).get("noul", 0) for o in q["options"]}
                if q["type"] == "rank":
                    out[q["id"]] = sorted(probs, key=probs.get, reverse=True)
                    continue
                chosen = [oid for oid, p in probs.items() if p >= 0.5]
                if not chosen and q["required"] and probs:
                    chosen = [max(probs, key=probs.get)]
                out[q["id"]] = chosen
            elif q["id"] in answers:
                o = keymap[q["id"]].get(answers[q["id"]].get("choice"))
                if o:
                    out[q["id"]] = o["id"]
        return out


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

Return ONLY a JSON object: {"answers": {"<question id>": <answer>, ...}}
- radio / select / listbox: one option id string, e.g. "q3.2"
- checkbox / multiselect: a list of option ids that apply, e.g. ["q4.1","q4.3"] (at least one if required)
- rank: a list of ALL option ids in order, most preferred / most important first
- text-like types: a string. date: YYYY-MM-DD, time: HH:MM, month: YYYY-MM,
  number/range: a number within min/max, email/tel/url: a valid, plausible value.
- Keep free-text answers natural and brief (1-3 sentences) unless the question asks for more,
  and respect maxlength.
Omit a question only if it truly cannot be answered."""


def mock_answers(questions):
    out = {}
    for q in questions:
        t = q["type"]
        if q["options"]:
            ids = [o["id"] for o in q["options"]]
            if t == "rank":
                out[q["id"]] = ids[::-1]
            elif t in ("checkbox", "multiselect"):
                out[q["id"]] = ids[:1] if t == "checkbox" else ids[:2]
            else:
                out[q["id"]] = ids[min(1, len(ids) - 1)] if t == "radio" else ids[0]
        elif t in ("number", "range"):
            out[q["id"]] = str(q.get("min") or 3)
        else:
            out[q["id"]] = {"email": "test@example.com", "date": "2000-01-01", "time": "09:30",
                            "tel": "5551234567", "url": "https://example.com",
                            "month": "2000-01"}.get(t, "Test answer")
    return out


def ask(jev, llm, persona, history, questions):
    """Jev (if enabled) decides choice questions; the LLM writes everything else."""
    if not jev:
        return ask_llm(llm, persona, history, questions)
    choice = [q for q in questions if q["type"] in CHOICE_TYPES and q["options"]]
    text = [q for q in questions if q not in choice]
    answers = jev.decide(persona, history, choice) if choice else {}
    if text:
        if llm and llm.ok:
            answers.update(ask_llm(llm, persona, history, text))
        else:
            print(f"  ({len(text)} free-text questions skipped: Jev can't write text; "
                  f"set a --text-provider key such as GROQ_API_KEY)")
    return answers


def ask_llm(llm, persona, history, questions):
    if llm.provider == "mock":
        return mock_answers(questions)
    answers = {}
    for i in range(0, len(questions), 30):  # keep prompts small for free tiers
        chunk = questions[i:i + 30]
        payload = [{k: v for k, v in {
            "id": q["id"], "type": q["type"], "question": q["text"], "required": q["required"] or None,
            "options": [{"id": o["id"], "label": o["label"]} for o in q["options"]] or None,
            "min": q.get("min"), "max": q.get("max"), "maxlength": q.get("maxlength"),
            "placeholder": q.get("placeholder"),
        }.items() if v is not None} for q in chunk]
        user = (f"PERSONA:\n{persona}\n\n"
                + ("EARLIER ANSWERS (stay consistent):\n" + "\n".join(history[-60:]) + "\n\n" if history else "")
                + "QUESTIONS:\n" + json.dumps(payload, ensure_ascii=False))
        data = llm.chat_json(SYSTEM_PROMPT, user)
        answers.update(data.get("answers", data))
    return answers


# --------------------------------------------------------------------------- page

def scan(page):
    """Extract questions from every frame. Returns questions with global ids."""
    questions = []
    for frame in page.frames:
        try:
            found = frame.evaluate(EXTRACT_JS)
        except PWError:
            continue  # cross-origin/detached frames we can't script
        for q in found:
            q["frame"] = frame
            q["id"] = f"q{len(questions) + 1}"
            for j, o in enumerate(q["options"], 1):
                o["id"] = f"{q['id']}.{j}"
            questions.append(q)
    return questions


def plan_actions(q, answer):
    """Turn one LLM answer into [(sf, op, value)] steps."""
    t, opts = q["type"], {o["id"]: o for o in q["options"]}
    by_label = {o["label"].lower(): o for o in q["options"]}

    def pick(a):  # accept an option id or (fallback) its label
        a = str(a).strip()
        return opts.get(a) or by_label.get(a.lower())

    many = answer if isinstance(answer, list) else [answer]
    if t == "multiselect":
        return [(q["sf"], "multiselect", [o for o in map(pick, many) if o])]
    if t == "rank":
        order = []
        for o in map(pick, many):
            if o and o not in order:
                order.append(o)
        order += [o for o in q["options"] if o not in order]  # unranked items keep their place at the end
        return [(q["sf"], "rank", order)]
    if t == "checkbox":
        chosen = {o["id"] for o in map(pick, many) if o}
        return [(o["sf"], "check" if o["id"] in chosen else "uncheck", None) for o in q["options"]]
    if t == "radio":
        o = pick(answer[0] if isinstance(answer, list) else answer)
        return [(o["sf"], "check", None)] if o else []
    if t in ("select", "listbox"):
        o = pick(answer[0] if isinstance(answer, list) else answer)
        return [(q["sf"], t, o)] if o else []
    if isinstance(answer, list):
        answer = ", ".join(map(str, answer))
    return [(q["sf"], "text", str(answer))]


def apply_answers(questions, answers, history):
    by_frame = {}
    labels = {}
    for q in questions:
        if q["id"] not in answers:
            continue
        steps = plan_actions(q, answers[q["id"]])
        for sf, op, val in steps:
            value = (val["value"] if op == "select" else [o["value"] for o in val] if op == "multiselect"
                     else [o["sf"] for o in val] if op == "rank" else val)
            by_frame.setdefault(q["frame"], []).append({"sf": sf, "op": op, "value": value})
        if q["type"] in ("select", "listbox"):
            shown = [steps[0][2]["label"]] if steps else []
        elif q["type"] in ("multiselect", "rank"):
            shown = [" > ".join(o["label"] for o in steps[0][2])] if q["type"] == "rank" else [o["label"] for o in steps[0][2]]
        else:
            checked = {s[0] for s in steps if s[1] == "check"}
            shown = [o["label"] for o in q["options"] if o.get("sf") in checked]
        labels[q["id"]] = ", ".join(shown) if q["options"] else str(answers[q["id"]])
        history.append(f"- {q['text'][:150]} => {labels[q['id']][:200]}")

    for q in questions:
        if q["id"] in labels:
            print(f"  {q['id']} [{q['type']}] {q['text'][:70]!r}\n      -> {labels[q['id']][:100]}")

    filled = failed = 0
    for frame, actions in by_frame.items():
        needs_mouse = ("listbox", "rank")
        fast = [a for a in actions if a["op"] not in needs_mouse]
        slow_ids = set(frame.evaluate(APPLY_JS, fast)) if fast else set()
        slow = [a for a in actions if a["op"] in needs_mouse or a["sf"] in slow_ids]
        filled += len(actions) - len(slow)
        for a in slow:  # real-input fallback
            ok = playwright_apply(frame, a)
            filled += ok
            failed += not ok
    return filled, failed


def playwright_apply(frame, a):
    loc = frame.locator(f'[data-sf="{a["sf"]}"]').first
    try:
        if a["op"] == "text":
            if loc.evaluate("e => e.isContentEditable"):
                loc.click()
                frame.page.keyboard.press("Control+A")
                frame.page.keyboard.type(a["value"])
            else:
                loc.fill(a["value"], timeout=3000)
        elif a["op"] in ("check", "uncheck"):
            want = a["op"] == "check"
            role = loc.get_attribute("role")
            if role:
                if (loc.get_attribute("aria-checked") == "true") != want:
                    loc.click(timeout=3000)
            else:
                loc.set_checked(want, timeout=3000, force=True)
        elif a["op"] in ("select", "multiselect"):
            loc.select_option(value=a["value"], timeout=3000)
        elif a["op"] == "rank":
            return rank_by_dragging(frame, loc, a["value"])
        elif a["op"] == "listbox":
            opt = a["value"]
            loc.click(timeout=3000)
            frame.wait_for_timeout(400)
            target = frame.locator(f'[role=option][data-value={json.dumps(opt["value"])}]:visible')
            if not target.count():
                target = frame.locator("[role=option]:visible").filter(has_text=opt["label"])
            target.first.click(timeout=3000)
            frame.wait_for_timeout(300)
        return True
    except PWError as e:
        print(f"    ! could not fill {a['sf']} ({a['op']}): {str(e).splitlines()[0][:120]}")
        return False


ORDER_JS = "c => Array.from(c.children).filter(e => e.dataset.sf).map(e => e.dataset.sf)"


def drag(frame, src, dst, y_frac):
    """Drag src onto dst at y_frac of its height, using real mouse movement so
    both HTML5 drag-and-drop and mouse/pointer-based sortable libraries react."""
    page = frame.page
    src.scroll_into_view_if_needed()
    if src.get_attribute("draggable") == "true":
        box = dst.bounding_box()
        src.drag_to(dst, target_position={"x": box["width"] / 2, "y": max(2, box["height"] * y_frac)}, timeout=3000)
        return
    s, d = src.bounding_box(), dst.bounding_box()
    page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2)
    page.mouse.down()
    page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2 - 5, steps=3)  # start threshold
    page.mouse.move(d["x"] + d["width"] / 2, d["y"] + max(2, d["height"] * y_frac), steps=20)
    page.wait_for_timeout(150)
    page.mouse.up()


def rank_by_dragging(frame, container, order):
    # Fill positions top-down: the wanted item is always below the slot, so every drag goes up.
    for i, sf in enumerate(order):
        for y_frac in (0.2, 0.5, 0.05):  # drop points differ between libraries
            cur = container.evaluate(ORDER_JS)
            if i >= len(cur) or cur[i] == sf:
                break
            drag(frame, frame.locator(f'[data-sf="{sf}"]'), frame.locator(f'[data-sf="{cur[i]}"]'), y_frac)
            frame.wait_for_timeout(250)
    final = container.evaluate(ORDER_JS)
    if final != order:
        print(f"    ! ranking ended as {final}, wanted {order}")
    return final == order


def find_button(page, pattern):
    for frame in page.frames:
        for role in ("button", "link"):
            try:
                loc = frame.get_by_role(role, name=pattern)
                for i in range(loc.count()):
                    b = loc.nth(i)
                    if b.is_visible() and b.is_enabled() and not re.search(r"back|previous|zurück|retour", b.inner_text() or "", re.I):
                        return b
            except PWError:
                pass
    return None


def settle(page):
    """Wait for client-rendered forms (e.g. Google Forms) to finish drawing."""
    page.wait_for_load_state("domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWError:
        pass  # pages with long-polling never go idle


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


def fill_page(page, llm, persona, history, overwrite, jev=None):
    settle(page)
    questions = scan(page)
    todo = [q for q in questions if overwrite or not q["answered"]]
    print(f"\nPage: {page.title()[:80]!r} - {len(questions)} questions found, {len(todo)} to fill")
    if not todo:
        return questions
    t0 = time.time()
    answers = ask(jev, llm, persona, history, todo)
    t1 = time.time()
    filled, failed = apply_answers(todo, answers, history)
    print(f"  filled {filled} controls ({failed} failed) - LLM {t1 - t0:.1f}s, fill {time.time() - t1:.2f}s")
    return questions


def signature(questions):
    return tuple(q["text"][:60] for q in questions)


# --------------------------------------------------------------------------- main

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description="Fill any web survey with a free LLM.")
    ap.add_argument("url", nargs="?", help="survey URL (omit to navigate manually)")
    keyless_default = "groq" if os.environ.get("GROQ_API_KEY") else "kilo"
    ap.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", keyless_default), choices=list(PROVIDERS))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL"))
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL"), help="any OpenAI-compatible endpoint")
    ap.add_argument("--api-key")
    ap.add_argument("--text-provider", default=os.environ.get("TEXT_PROVIDER", keyless_default),
                    choices=[p for p in PROVIDERS if p != "jev"],
                    help="with --provider jev: the LLM that writes free-text answers")
    ap.add_argument("--text-model", default=os.environ.get("TEXT_MODEL"))
    ap.add_argument("--persona", help="who is answering (text)")
    ap.add_argument("--persona-file", help="file describing who is answering")
    ap.add_argument("--cdp", help="attach to a running Chrome, e.g. http://localhost:9222")
    ap.add_argument("--auto", action="store_true", help="click Next automatically between pages")
    ap.add_argument("--submit", action="store_true", help="with --auto, also click the final Submit")
    ap.add_argument("--overwrite", action="store_true", help="re-answer questions that already have a value")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dump", action="store_true", help="only print the questions found, don't fill")
    args = ap.parse_args()

    persona = args.persona or (Path(args.persona_file).read_text(encoding="utf-8") if args.persona_file else None)
    if not persona and (HERE / "persona.txt").exists():
        persona = (HERE / "persona.txt").read_text(encoding="utf-8")
    persona = persona or DEFAULT_PERSONA
    jev = llm = None
    if args.dump:
        pass
    elif args.provider == "jev":
        jev = Jev(args.model, args.api_key, args.base_url)
        llm = LLM(args.text_provider, args.text_model, required=False)
    else:
        llm = LLM(args.provider, args.model, args.base_url, args.api_key)
    history = []

    with sync_playwright() as p:
        if args.cdp:
            browser = p.chromium.connect_over_cdp(args.cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[-1] if context.pages else context.new_page()
        else:
            context = p.chromium.launch_persistent_context(
                str(HERE / ".browser-profile"), headless=args.headless, no_viewport=True)
            page = context.pages[0] if context.pages else context.new_page()
        if args.url:
            page.goto(args.url)

        def current():
            pages = [pg for pg in context.pages if not pg.is_closed()]
            return pages[-1] if pages else context.new_page()

        if args.dump:
            if not args.url:
                input("Open the survey in the browser, then press Enter...")
            for q in scan(current()):
                q.pop("frame")
                print(json.dumps(q, ensure_ascii=False))
            return

        if not args.url:
            input("Open the survey in the browser, then press Enter to fill it...")

        last_sig = None
        while True:
            page = current()
            try:
                qs = fill_page(page, llm, persona, history, args.overwrite, jev)
            except RuntimeError as e:
                print(f"  ! {e}")
                qs = []

            if args.auto:
                sig = signature(qs)
                nxt = find_button(page, NEXT_RE)
                if nxt and sig != last_sig:
                    last_sig = sig
                    print("  -> clicking Next")
                    nxt.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(1500)
                    continue
                if nxt:
                    print("  Page didn't change after Next (validation error?). Fix it in the browser.")
                elif args.submit and (sub := find_button(page, SUBMIT_RE)):
                    print("  -> clicking Submit")
                    if submit_and_confirm(page, sub):
                        break
                args.auto = False  # hand control back to the user

            try:
                cmd = input("\n[Enter] fill this page again   n = click Next   s = click Submit   q = quit > ").strip().lower()
            except EOFError:
                cmd = "q"
            if cmd == "q":
                break
            if cmd in ("n", "s"):
                btn = find_button(current(), NEXT_RE if cmd == "n" else SUBMIT_RE)
                if btn and cmd == "s":
                    submit_and_confirm(current(), btn)
                elif btn:
                    btn.click()
                    current().wait_for_timeout(1500)
                else:
                    print("  No such button found - click it yourself, then press Enter.")
                    input()

        if not args.cdp:
            context.close()


if __name__ == "__main__":
    main()
