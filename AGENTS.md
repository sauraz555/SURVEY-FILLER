# Survey Filler — Agent Guidelines

- **Workflow:** Open Chrome (`python agent_controller.py launch [URL]`), wait for the user to sign in / grant access and click **`▶ Start Survey`** (`python agent_controller.py wait`), then drive the survey to completion (`state` -> `jev`/`apply` -> `click next`/`submit`).
- **Boundaries:** User handles any sign-in or security checks manually in Chrome; never attempt to bypass access or security controls.
- **Skill Reference:** Follow [.agents/skills/survey-filler/SKILL.md](./.agents/skills/survey-filler/SKILL.md) for JSON schemas across all control types.
