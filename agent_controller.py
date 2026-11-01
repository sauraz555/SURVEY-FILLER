"""
Agent Controller for Survey Filler — lets an AI Agent (e.g. Antigravity) open
Google Chrome, wait for your access handoff, inspect on-screen questions &
buttons as JSON, apply answers directly (with or without Jev), and submit.

    python agent_controller.py launch [URL]
    python agent_controller.py wait
    python agent_controller.py state
    python agent_controller.py jev
    python agent_controller.py apply --answers "{\"q1\": \"Alice\"}"
    python agent_controller.py click <next|submit|sf_id>
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

import survey_filler as sf

HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 9222


def cdp_alive(cdp_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def find_chrome_binary() -> str:
    for c in (
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if c and Path(c).exists():
            return str(c)
    raise RuntimeError("Could not find Google Chrome executable. Set CHROME_PATH or install Chrome.")


def ensure_detached_chrome(port: int = DEFAULT_PORT) -> str:
    cdp_url = f"http://127.0.0.1:{port}"
    if cdp_alive(cdp_url):
        return cdp_url
    profile_dir = HERE / ".browser-profile-cdp"
    profile_dir.mkdir(exist_ok=True)
    cmd = [
        find_chrome_binary(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    for _ in range(40):
        if cdp_alive(cdp_url):
            return cdp_url
        time.sleep(0.25)
    raise RuntimeError(f"Chrome started but CDP did not respond on {cdp_url}.")


def active_tab(browser):
    for ctx in reversed(browser.contexts):
        pages = [pg for pg in ctx.pages if not pg.is_closed() and not pg.url.startswith("devtools://")]
        if pages:
            return ctx, pages[-1]
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return ctx, ctx.new_page()


def strip_frames(items):
    return [{k: v for k, v in x.items() if k != "frame"} for x in items]


def inspect_page(page):
    sf.settle(page)
    try:
        body_text = page.inner_text("body", timeout=2000)
    except PWError:
        body_text = ""
    done = bool(sf.DONE_TEXT_RE.search(body_text) or sf.DONE_URL_RE.search(page.url or ""))
    questions = sf.scan(page)
    buttons = sf.extract_nav_buttons(page, questions)
    return {
        "ok": True,
        "title": page.title(),
        "url": page.url,
        "done": done,
        "questions": strip_frames(questions),
        "buttons": strip_frames(buttons),
    }


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_launch(args):
    cdp_url = ensure_detached_chrome(args.port)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        _, page = active_tab(browser)
        if args.url:
            page.goto(args.url)
        elif page.url in ("about:blank", "chrome://newtab/"):
            page.set_content(sf.WELCOME_HTML)
        page.bring_to_front()
        sf.update_hud(page, "Agent Ready — give access, then click Start", "▶ Start Survey", "ready")
        emit({"ok": True, "cdp": cdp_url, "url": page.url, "title": page.title()})


def cmd_wait(args):
    cdp_url = f"http://127.0.0.1:{args.port}"
    if not cdp_alive(cdp_url):
        sys.exit(json.dumps({"ok": False, "error": f"Chrome CDP not running on {cdp_url}. Run 'launch' first."}))
    deadline = time.time() + args.timeout
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        ctx, _ = active_tab(browser)
        for pg in ctx.pages:
            try:
                pg.evaluate("() => { delete window.__sfGo; }")
            except PWError:
                pass
        while time.time() < deadline:
            pages = [pg for pg in ctx.pages if not pg.is_closed() and not pg.url.startswith("devtools://")]
            if not pages:
                emit({"ok": False, "error": "Chrome window closed."})
                return
            for pg in pages:
                sf.update_hud(pg, args.status, args.label, args.tone)
                try:
                    sig = pg.evaluate("() => { const v = window.__sfGo; delete window.__sfGo; return v || null; }")
                    if sig:
                        pg.bring_to_front()
                        emit({"signal": sig, **inspect_page(pg)})
                        return
                except PWError:
                    pass
            pages[-1].wait_for_timeout(250)
        emit({"ok": False, "error": "Timed out waiting for user click in Chrome."})


def cmd_state(args):
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        _, page = active_tab(browser)
        emit(inspect_page(page))


def cmd_apply(args):
    raw = Path(args.answers_file).read_text(encoding="utf-8") if args.answers_file else args.answers
    answers = json.loads(raw)
    if "answers" in answers and isinstance(answers["answers"], dict):
        answers = answers["answers"]

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        _, page = active_tab(browser)
        sf.update_hud(page, "Agent filling page...", None, "busy")
        questions = sf.scan(page)
        history = []
        with contextlib.redirect_stdout(sys.stderr):
            filled, failed = sf.apply_answers(questions, answers, history)
        page.wait_for_timeout(400)
        after = inspect_page(page)
        emit({
            "ok": True,
            "filled": filled,
            "failed": failed,
            "applied_log": history,
            "unanswered_questions": [q for q in after["questions"] if not q["answered"]],
            "buttons": after["buttons"],
        })


def cmd_jev(args):
    sf.load_dotenv(HERE / ".env")
    persona = args.persona or (
        (HERE / "persona.txt").read_text(encoding="utf-8") if (HERE / "persona.txt").exists() else sf.DEFAULT_PERSONA
    )
    jev = sf.Jev()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        _, page = active_tab(browser)
        sf.update_hud(page, "Jev deciding structured questions...", None, "busy")
        questions = sf.scan(page)
        todo = [q for q in questions if args.overwrite or not q["answered"]]
        choice_qs = [q for q in todo if sf.can_jev(q)]
        filled = failed = 0
        history = []
        if choice_qs:
            ans = jev.decide(persona, history, choice_qs)
            with contextlib.redirect_stdout(sys.stderr):
                filled, failed = sf.apply_answers(choice_qs, ans, history)
            page.wait_for_timeout(400)
            questions = sf.scan(page)
        emit({
            "ok": True,
            "jev_filled": filled,
            "jev_failed": failed,
            "applied_log": history,
            "remaining_questions_for_agent": strip_frames([q for q in questions if not q["answered"]]),
            "buttons": strip_frames(sf.extract_nav_buttons(page, questions)),
        })


def cmd_click(args):
    target = args.target.strip()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        _, page = active_tab(browser)
        low = target.lower()
        confirmed = False
        if low == "next":
            btn = sf.find_button(page, sf.NEXT_RE)
            if not btn:
                emit({"ok": False, "error": "No standard Next/Start button found; pass a button sf id from state.buttons."})
                return
            sf.update_hud(page, "Advancing...", None, "busy")
            btn.click()
            page.wait_for_timeout(1500)
        elif low == "submit":
            btn = sf.find_button(page, sf.SUBMIT_RE)
            if not btn:
                emit({"ok": False, "error": "No standard Submit button found; pass a button sf id from state.buttons."})
                return
            sf.update_hud(page, "Submitting...", None, "busy")
            with contextlib.redirect_stdout(sys.stderr):
                confirmed = sf.submit_and_confirm(page, btn)
        else:
            clicked = False
            for frame in page.frames:
                loc = frame.locator(f'[data-sf="{target}"]').first
                if loc.count():
                    loc.click(timeout=3000)
                    clicked = True
                    break
            if not clicked:
                emit({"ok": False, "error": f"No element with data-sf={target!r} found."})
                return
            page.wait_for_timeout(1500)

        after = inspect_page(page)
        after["submitted_confirmed"] = confirmed or after["done"]
        if after["submitted_confirmed"]:
            sf.update_hud(page, "Survey Completed!", "▶ Fill Another", "done")
        emit(after)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="JSON controller for AI Agents driving Survey Filler in Google Chrome.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_launch = sub.add_parser("launch")
    p_launch.add_argument("url", nargs="?")
    p_launch.set_defaults(func=cmd_launch)

    p_wait = sub.add_parser("wait")
    p_wait.add_argument("--status", default="Agent Ready — give access, then click Start")
    p_wait.add_argument("--label", default="▶ Start Survey")
    p_wait.add_argument("--tone", default="ready", choices=["ready", "busy", "pause", "done"])
    p_wait.add_argument("--timeout", type=int, default=600)
    p_wait.set_defaults(func=cmd_wait)

    p_state = sub.add_parser("state")
    p_state.set_defaults(func=cmd_state)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--answers")
    p_apply.add_argument("--answers-file")
    p_apply.set_defaults(func=cmd_apply)

    p_jev = sub.add_parser("jev")
    p_jev.add_argument("--persona")
    p_jev.add_argument("--overwrite", action="store_true")
    p_jev.set_defaults(func=cmd_jev)

    p_click = sub.add_parser("click")
    p_click.add_argument("target")
    p_click.set_defaults(func=cmd_click)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
