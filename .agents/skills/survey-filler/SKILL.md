---
name: survey-filler
description: >-
  Use this skill whenever the user asks the agent to open Chrome and fill,
  test, or complete a web survey or form after the user grants access/signs in.
---

# Survey Filler Agent Runbook

Drive Google Chrome directly through [agent_controller.py](../../../agent_controller.py) (needing zero external LLM keys—answer the extracted JSON questions yourself, optionally pairing with TypeSafe Jev) or run [survey_filler.py](../../../survey_filler.py) in autopilot.

## Rules

- Let the user handle logins, access prompts, and security checks manually in Chrome before clicking **`▶ Start Survey`** (or **`▶ Continue`**).
- Only automate forms the user owns or is authorized to automate.

## Agent Workflow ("Open Chrome -> User Gives Access -> Survey Done")

1. **Open Chrome:**
   ```powershell
   python agent_controller.py launch "https://optional-survey-url"
   ```
2. **Wait for User Handoff:**
   ```powershell
   python agent_controller.py wait
   ```
3. **Loop Until `done: true`:**
   - Read screen state:
     ```powershell
     python agent_controller.py state
     ```
   - *(Optional, if `TYPESAFE_API_KEY` is in `.env`)* Let Jev answer structured questions first:
     ```powershell
     python agent_controller.py jev
     ```
   - Answer remaining unanswered questions using these value types:
     - `radio` / `select` / `listbox` / `combobox` / `switch` / `button_group`: `"q2.1"`
     - `checkbox` / `multiselect` / `button_multi`: `["q3.1", "q3.2"]`
     - `rank`: ordered list of **all** option IDs (most preferred first)
     - `bucket`: `{"q5.i1": "q5.1"}` mapping each item ID in `items` to a bucket option ID in `options`
     - `slider` / `range` / `number`: numeric string within `min`..`max`
     - `text` / `textarea` / `email` / `date` / `autocomplete` / `contenteditable`: valid string
   - Apply answers (re-applying if `unanswered_questions` reveals conditional follow-up inputs):
     ```powershell
     python agent_controller.py apply --answers-file answers.json
     ```
   - Advance or submit:
     ```powershell
     python agent_controller.py click next
     python agent_controller.py click submit
     ```
     *(Or click any custom button `sf` ID from `state.buttons`, e.g. `python agent_controller.py click e42`.)*
