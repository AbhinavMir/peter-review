# Peter Review

A GitHub Action. When a user comments `explain this Peter` on a pull request, the action makes a vertical video and posts it on the PR. In the video, Peter and Brian talk through the PR over parkour footage, with captions and code on screen.

The video covers:

- the design decisions
- the code (line by line when the PR changes 40 lines or fewer)
- the cons
- what to improve
- what is already good

This README is for coding agents. A human can follow it too.

## How it works

1. `gh` gets the PR title, description, file list, and diff.
2. Claude writes a dialogue between Peter and Brian. Each line has a section tag and an optional code snippet from the diff.
3. Fish Audio speaks each line with the voice of the speaker.
4. Pillow draws the captions, the code cards, and the speaker image. ffmpeg puts them on `assets/background.mp4` with the voice.
5. The action uploads the video to a release named `peter-review` in the repo. Then it comments on the PR with the video link and the transcript.

A run uses one Claude call and one Fish Audio call per dialogue line (about 20 lines).

## Install in a repo

Do these steps in the repo that will use the bot (the target repo).

1. Get an Anthropic API key and a Fish Audio API key from the user. Do not write the keys to a file.

2. Add the keys as repo secrets:

   ```
   gh secret set ANTHROPIC_API_KEY --repo OWNER/REPO
   gh secret set FISH_API_KEY --repo OWNER/REPO
   ```

   Each command asks for the value. Let the user paste it.

3. Optional: set the Claude model. The default is `claude-haiku-5-5`, which costs the least.

   ```
   gh variable set PETER_ANTHROPIC_MODEL --repo OWNER/REPO --body claude-sonnet-5-5
   ```

4. Copy `examples/peter-review.yml` from this repo to `.github/workflows/peter-review.yml` in the target repo. Commit it to the default branch. GitHub runs `issue_comment` workflows only from the default branch.

5. Test: open a PR in the target repo and comment `explain this Peter`. The bot adds an 👀 reaction to the comment. The comment with the video comes after the render.

## Who can start the bot

Only users with the `OWNER`, `MEMBER`, or `COLLABORATOR` association on the repo. Other users cannot spend the keys. The check is in the `if:` line of the workflow. The trigger text is not case sensitive.

## Inputs

All inputs except the two keys are optional. Add them under `with:` in the workflow.

| Input | Default | What it does |
|---|---|---|
| `anthropic-api-key` | (required) | Anthropic API key. |
| `fish-api-key` | (required) | Fish Audio API key. |
| `anthropic-model` | `claude-haiku-5-5` | Claude model that writes the script. |
| `anthropic-effort` | `low` | Effort level. Set to `""` for models that do not support effort, for example `claude-haiku-4-5`. |
| `fish-model` | `s2.1-pro-free` | Fish Audio TTS model. The free model works without paid API credit. |
| `fish-speed` | `1.15` | Speech speed, 0.5 to 2.0. |
| `peter-voice-id` | Peter voice | Fish Audio voice model ID for Peter. |
| `brian-voice-id` | Brian voice | Fish Audio voice model ID for Brian. |
| `characters-dir` | images in this action | Directory in the target repo with `peter.png` and `brian.png`. The job must run `actions/checkout` first. |
| `github-token` | `github.token` | Token that uploads the video and posts the comment. |

The workflow needs these permissions: `contents: write` (release upload), `pull-requests: write` (comment), and `issues: write` (reaction).

## Files

| File | What it is |
|---|---|
| `action.yml` | The composite action. |
| `peter.py` | The full pipeline. It also runs locally. |
| `examples/peter-review.yml` | The workflow for target repos. |
| `assets/background.mp4` | 4 minutes of parkour footage, 720x1280. Each video starts at a random point. |
| `assets/characters/` | `peter.png` and `brian.png`, transparent PNGs. |

## Run locally

Requirements: Python 3.10 or later, ffmpeg, and `gh` with a login.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cat > .env <<'EOF'
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-haiku-5-5
ANTHROPIC_EFFORT=low
FISH_API_KEY=...
FISH_MODEL=s2.1-pro-free
FISH_SPEED=1.15
PETER_VOICE_ID=d75c270eaee14c8aa1e9e980cc37cf1b
BRIAN_VOICE_ID=e88928ec5022418cad0606fcfc809b8a
EOF
set -a; source .env; set +a
.venv/bin/python peter.py --repo OWNER/REPO --pr 123
```

The video goes to `out/pr-123.mp4` and the script goes to `out/pr-123.json`. Other flags:

- `--commit SHA` in place of `--pr` explains one commit, for repos that do not use PRs (for example `--repo torvalds/linux --commit 51628610f918`). `--post` does not work with `--commit`.
- `--script out/pr-123.json` uses a saved script and does not call Claude.
- `--post` uploads the video and comments on the PR.

## Errors

| Message | Fix |
|---|---|
| `Missing: PETER_VOICE_ID, ...` | Set the named variables, or add the named image files. |
| `Fish Audio error 402` | The Fish account has no API credit for that model. Use `fish-model: s2.1-pro-free`, or add credit. |
| `Claude stopped with stop_reason=...` | The script was cut off or refused. Run again, or use a different model. |
| HTTP 400 that names `effort` | The model does not support effort. Set `anthropic-effort: ""`. |
