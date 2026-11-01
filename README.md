# Survey Filler

Fills web surveys and forms with Playwright and a free LLM (and/or Jev).

1. **Read the page with JavaScript.** One in-page script (`extract.js`) finds every survey question across frames and open Shadow DOM: text, email, number and date fields, textareas, contenteditable boxes, radios, checkboxes, single and multi-selects, matrix/Likert tables, custom ARIA radios/checkboxes/switches, inline and portal-rendered comboboxes/dropdowns, native `<input type=range>` and custom ARIA `[role=slider]` controls, button-group/NPS/star rating scales, **drag-and-drop ranking lists**, and **drag-into-bucket card sorts**.
2. **Ask Jev and/or the LLM.** Structured choice, scale, ranking, and bucket-sorting questions can be decided by TypeSafe's **Jev** model (or any LLM), while open-ended text fields go to `--text-provider` (Groq or keyless Kilo by default). Earlier answers and any inline validation errors are included so replies stay consistent and self-correct.
3. **Fill fast with JavaScript + Playwright.** Simple inputs fill in one in-page call (`apply.js`); custom widgets (portal comboboxes, ARIA sliders, button scales, rankings, and card sorts) use real Playwright pointer and keyboard actions. After filling, the page is re-scanned automatically to answer any conditional follow-up fields that appeared, and `--auto` advances past Welcome/Start screens as well as Next buttons.

## Setup

```bash
pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

**No key needed to start.** Without a Groq key, the tool uses Kilo Code's free models, which allow about 200 requests an hour.

For faster answers, get a free Groq key:

1. Go to https://console.groq.com/keys and sign in with Google or GitHub. It's free and needs no card.
2. Click **Create API Key**, give it any name, and copy the key. It starts with `gsk_` and is shown only once.
3. Save it by running `powershell -ExecutionPolicy Bypass -File .\setup_keys.ps1` in this folder and pasting the key at the Groq prompt. Alternatively, copy `.env.example` to `.env` and paste the key after `GROQ_API_KEY=`.

| Provider | Free key | Flag |
|---|---|---|
| Kilo Code (no key) | not needed | `--provider kilo` (default without a Groq key) |
| Groq | https://console.groq.com/keys | `--provider groq` (default with a key) |
| Google Gemini | https://aistudio.google.com/apikey | `--provider gemini` |
| OpenRouter (`:free` models) | https://openrouter.ai/keys | `--provider openrouter` |
| Ollama (local, no key) | https://ollama.com, then `ollama pull llama3.1` | `--provider ollama` |

Any other OpenAI-compatible endpoint also works with `--base-url URL --model NAME --api-key KEY`.

### Jev (decision model) + free LLM

[Jev](https://docs.typesafe.ai) from TypeSafe AI returns typed decisions instead of text. That makes it fast and cheap for the structured questions that make up most surveys:

- radio buttons, selects, inline/portal dropdowns (`listbox`/`combobox`), switches, and button/star scales become a Jev **Choice** question;
- native `<input type=range>` and custom ARIA `[role=slider]` controls become quantized Jev **Choice** steps across `min`..`max`;
- drag-into-bucket card sorts ask one Jev **Choice** per card across the target buckets;
- each checkbox, multi-select, or multi-toggle button option becomes a Jev **Noul**, a probability that the option applies, and options at 0.5 or above are ticked;
- ranking items each get a Noul, and the list is dragged into order by those probabilities;
- free-text, number and date fields go to `--text-provider`, which defaults to Groq (if `GROQ_API_KEY` is set) or keyless Kilo.

```bash
python survey_filler.py URL --provider jev --text-provider groq
```

Put `TYPESAFE_API_KEY` (from https://console.typesafe.ai/keys) and optionally `GROQ_API_KEY` in `.env`. Jev is a paid model at about $0.042 per million input tokens, and output is free.

## Usage

Describe who is answering in `persona.txt` (see `persona.example.txt`), or pass it with `--persona "..."`.

```bash
python survey_filler.py
# or double-click start_survey_filler.bat, or pass a URL directly:
python survey_filler.py https://your-survey-url
```

1. **Google Chrome opens** (saving your session in `.browser-profile/`).
2. **You give access:** open the survey, sign in, or complete any security check, then click **`▶ Start Survey`** in the top-right pill inside Chrome (or press **Enter** in the terminal).
3. **Survey is done:** Survey Filler automatically answers every page (via Jev + free LLM when `TYPESAFE_API_KEY` is set, or Groq/Kilo), clicks Start/Next across all pages, pauses with a **`▶ Resume`** button if a sign-in or security check appears mid-survey, and clicks **Submit**.

Optional flags:

```bash
python survey_filler.py URL --no-submit        # stop right before clicking final Submit
python survey_filler.py URL --manual           # pause after every page instead of full auto-run
python survey_filler.py URL --auto             # start filling immediately without waiting for Start click
python survey_filler.py URL --dump             # only list the questions it found
python survey_filler.py URL --provider mock    # no LLM: picks options mechanically (for testing)
```

### Drive It Directly from an AI Agent (Antigravity / Coding Agents)

The project includes a workspace skill ([`.agents/skills/survey-filler/SKILL.md`](./.agents/skills/survey-filler/SKILL.md)), [`AGENTS.md`](./AGENTS.md), and [`agent_controller.py`](./agent_controller.py) so an AI coding agent can open Chrome, wait for you to grant access, and complete the survey directly over JSON (needing **zero** external LLM API keys, or pairing with Jev):

```bash
python agent_controller.py launch https://your-survey-url   # opens Chrome with [▶ Start Survey] pill
python agent_controller.py wait                             # waits until you click [▶ Start Survey] in Chrome
python agent_controller.py state                            # returns questions, buttons & completion status as JSON
python agent_controller.py jev                              # (optional) Jev answers structured questions
python agent_controller.py apply --answers "{\"q1\":\"Hi\"}" # applies agent-chosen answers
python agent_controller.py click next                       # or 'submit' / custom button sf id
```

### Fill a survey already open in your Chrome

Start Chrome with remote debugging. Chrome needs a separate profile directory for this:

```bash
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=%USERPROFILE%\chrome-debug
```

Open the survey in that window, then run:

```bash
python survey_filler.py --cdp http://localhost:9222
```

Without `--cdp`, the script uses its own Chromium window with a saved profile in `.browser-profile/`, so logins persist between runs.

## Test page

`test_survey.html` is a multi-page survey (Welcome screen + two pages) that contains every supported control type:

```bash
python survey_filler.py "file:///C:/Users/saura/Downloads/survey%20filler/test_survey.html" --provider mock --auto --submit
```

## Limits

- CAPTCHAs and bot checks are not bypassed. Solve them yourself in Chrome before clicking `▶ Start Survey` / `▶ Continue`.
- File uploads (`<input type="file">`) and canvas-only widgets without DOM/ARIA semantics are not filled.
- Screener questions get answered truthfully from your persona. The tool does not try to game them to qualify.
- Use it only on forms you're allowed to automate, such as your own forms, QA testing, or surveys whose terms permit it.
