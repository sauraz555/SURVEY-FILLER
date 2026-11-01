# Survey Filler

Fills web surveys and forms with Playwright and a free LLM.

1. **Read the page with JavaScript.** One in-page script (`extract.js`) finds every question in every frame: text, email, number and date fields, textareas, radios, checkboxes, single and multi-selects, matrix/Likert tables, custom ARIA radios and checkboxes, Google-Forms-style dropdowns, and **drag-and-drop ranking lists**. Ranking lists can use HTML5 `draggable` or mouse-driven sortable libraries such as SortableJS or jQuery UI.
2. **Ask the LLM.** All questions on the page go to the LLM in one request, along with your persona and your answers from earlier pages, so the answers stay consistent.
3. **Fill fast with JavaScript.** One in-page call (`apply.js`) fills the whole page. A field that resists it, such as a custom dropdown, falls back to a real Playwright click or keystrokes. Rankings are reordered with real mouse drags, top slot first, and the final order is checked afterwards.

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

[Jev](https://docs.typesafe.ai) from TypeSafe AI returns typed decisions instead of text. That makes it fast and cheap for the "pick an option" questions that make up most surveys:

- radio buttons, dropdowns and custom dropdowns become a Jev **Choice** question, with the options as its criteria;
- each checkbox or multi-select option becomes a Jev **Noul**, a probability that the option applies, and options at 0.5 or above are ticked;
- ranking items each get a Noul, and the list is dragged into order by those probabilities;
- free-text, number and date fields go to `--text-provider`, which is Groq by default, because Jev can't write text.

```bash
python survey_filler.py URL --provider jev --text-provider groq
```

Put `TYPESAFE_API_KEY` (from https://console.typesafe.ai/keys) and `GROQ_API_KEY` in `.env`. Jev is a paid model at about $0.042 per million input tokens, and output is free. If no text-provider key is set, free-text questions are skipped.

## Usage

Describe who is answering in `persona.txt` (see `persona.example.txt`), or pass it with `--persona "..."`.

```bash
python survey_filler.py https://your-survey-url
```

The script fills the page and then waits for a key in the terminal:

- **Enter** re-scans and fills the current page. Use it after you move to a new page yourself.
- **n** clicks Next.
- **s** clicks Submit.
- **q** quits.

It never submits on its own unless you pass `--submit`.

Other modes:

```bash
python survey_filler.py URL --auto            # fill and click Next through every page, stop before Submit
python survey_filler.py URL --auto --submit   # also click the final Submit
python survey_filler.py URL --dump            # only list the questions it found
python survey_filler.py URL --provider mock   # no LLM: picks options mechanically (for testing)
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

`test_survey.html` is a two-page survey that contains every supported control type:

```bash
python survey_filler.py "file:///C:/Users/saura/Downloads/survey%20filler/test_survey.html" --provider mock --auto --submit
```

## Limits

- CAPTCHAs and bot checks are not bypassed. Solve them yourself.
- Custom sliders (not native `<input type=range>`), drag-into-bucket card sorts, and file uploads are not filled.
- Screener questions get answered truthfully from your persona. The tool does not try to game them to qualify.
- Use it only on forms you're allowed to automate, such as your own forms, QA testing, or surveys whose terms permit it.
