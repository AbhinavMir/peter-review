#!/usr/bin/env python3
"""Peter Review: turn a GitHub PR into a vertical parkour explainer video.

Pipeline: PR diff (gh) -> script (Claude Haiku) -> voice (Fish Audio) -> captions + background (ffmpeg).
"""

import argparse
import concurrent.futures
import json
import os
import random
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import anthropic
import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
BACKGROUND = ROOT / "assets" / "background.mp4"
SMALL_PR_LINES = 40  # PRs with this many changed lines or fewer get a line-by-line explanation
MAX_DIFF_CHARS = 300_000
WIDTH, HEIGHT = 720, 1280
GAP_SECONDS = 0.15

CHARACTERS = {
    "peter": {"name": "Peter", "voice_env": "PETER_VOICE_ID", "side": "left", "color": "white"},
    "brian": {"name": "Brian", "voice_env": "BRIAN_VOICE_ID", "side": "right", "color": (150, 220, 255)},
}
CHARACTER_DIR = Path(os.environ.get("CHARACTERS_DIR") or ROOT / "assets" / "characters")
CHARACTER_HEIGHT = 520

SECTIONS = {
    "hook": "",
    "decisions": "THE DECISIONS",
    "code": "THE CODE",
    "cons": "THE CONS",
    "improvements": "WHAT TO IMPROVE",
    "good": "WHAT IS GOOD",
    "outro": "",
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": list(CHARACTERS)},
                    "section": {"type": "string", "enum": list(SECTIONS)},
                    "text": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["speaker", "section", "text", "code"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lines"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Write the script for a short vertical "doom-scrolling" video that explains a GitHub pull request.
It is a conversation between two cartoon characters, Peter and Brian, over parkour footage.
Peter is loud, goofy, and overconfident, but what he says about the code is correct.
Brian is the dry, sarcastic, smart one. He asks questions, pushes back, and calls out the weak points.
Make it funny and fast, with real banter, but keep every technical claim correct.
Explain it so that a junior developer understands. Use plain words. When you must use a technical
term, explain it in a few words the first time.
Never invent facts that the diff does not show.
The change can be a pull request or a single commit. Call it what the input says it is.
Raise a con only when the diff clearly shows it. Do not raise a concern that the description
already answers. Do not call two things a contradiction unless they really conflict.

Rules for each line:
- Spoken text only: no markdown, no code, no emojis, no stage directions.
- One or two short sentences per line. Lines alternate between the speakers most of the time.
- Say identifiers in a speakable way (for example "the parse config function").
- code: the lines from the diff that this line talks about, shown on screen while it plays.
  Copy them exactly from the diff, with the leading "+" or "-" of each diff line. Use 3 to 6
  consecutive lines so the viewer sees a real block of code, and pick the most interesting part.
  Use an empty string when the line is not about specific code.
  In the decisions, code, and cons sections, almost every line must have code.

Go through these sections in this order, and tag each line with its section:
- hook: Brian asks what the PR is, Peter answers with a hook. Two or three lines.
- decisions: the design decisions the author made, and whether they make sense.
- code: how the code works. {code_mode}
- cons: risks, bugs, edge cases, and weak points.
- improvements: concrete changes that would make the PR better.
- good: what is already pretty good.
- outro: one or two lines to end on a joke.

Target length for all lines together: {word_target} words."""


def sh(*cmd, capture=True):
    result = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return result.stdout if capture else ""


def fetch_pr(repo, pr):
    meta = json.loads(sh("gh", "pr", "view", str(pr), "--repo", repo,
                         "--json", "title,body,author,additions,deletions,changedFiles,files"))
    diff = sh("gh", "pr", "diff", str(pr), "--repo", repo)
    return meta, diff


def fetch_commit(repo, sha):
    c = json.loads(sh("gh", "api", f"repos/{repo}/commits/{sha}"))
    title, _, body = c["commit"]["message"].partition("\n")
    meta = {
        "kind": "commit",
        "title": title,
        "body": body.strip(),
        "additions": c["stats"]["additions"],
        "deletions": c["stats"]["deletions"],
        "changedFiles": len(c["files"]),
        "files": [{"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"]} for f in c["files"]],
    }
    diff = sh("gh", "api", "-H", "Accept: application/vnd.github.diff", f"repos/{repo}/commits/{sha}")
    return meta, diff


def write_script(meta, diff):
    changed = meta["additions"] + meta["deletions"]
    small = changed <= SMALL_PR_LINES
    code_mode = ("This PR is small, so go through the changed lines one by one." if small
                 else "Explain the code at a high level, by file or by component. Do not go line by line.")
    word_target = "120 to 180" if small else "180 to 260"

    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS]
    files = "\n".join(f"- {f['path']} (+{f['additions']} -{f['deletions']})" for f in meta["files"])
    user = (
        f"Type: {meta.get('kind', 'pull request')}\n"
        f"Title: {meta['title']}\n"
        f"Changed lines: +{meta['additions']} -{meta['deletions']} in {meta['changedFiles']} files\n"
        f"Files:\n{files}\n\n"
        f"Description:\n{meta['body'] or '(empty)'}\n\n"
        + ("NOTE: the diff below is cut off because it is very large. Say so in the cons.\n\n" if truncated else "")
        + f"Diff:\n{diff}"
    )

    output_config = {"format": {"type": "json_schema", "schema": SCRIPT_SCHEMA}}
    if os.environ.get("ANTHROPIC_EFFORT"):
        output_config["effort"] = os.environ["ANTHROPIC_EFFORT"]
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=os.environ["ANTHROPIC_MODEL"],
        max_tokens=8000,
        system=SYSTEM_PROMPT.format(code_mode=code_mode, word_target=word_target),
        messages=[{"role": "user", "content": user}],
        output_config=output_config,
    )
    if response.stop_reason != "end_turn":
        sys.exit(f"Claude stopped with stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def check_assets():
    missing = [] if os.environ.get("ANTHROPIC_MODEL") else ["ANTHROPIC_MODEL"]
    missing += [c["voice_env"] for c in CHARACTERS.values() if not os.environ.get(c["voice_env"])]
    missing += [str(CHARACTER_DIR / f"{k}.png") for k in CHARACTERS if not (CHARACTER_DIR / f"{k}.png").exists()]
    if missing:
        sys.exit("Missing: " + ", ".join(missing))


def tts(line, out_path):
    headers = {"Authorization": f"Bearer {os.environ['FISH_API_KEY']}"}
    if os.environ.get("FISH_MODEL"):
        headers["model"] = os.environ["FISH_MODEL"]
    body = {"text": line["text"], "format": "wav", "sample_rate": 44100,
            "reference_id": os.environ[CHARACTERS[line["speaker"]]["voice_env"]]}
    if os.environ.get("FISH_SPEED"):
        body["prosody"] = {"speed": float(os.environ["FISH_SPEED"])}
    r = requests.post("https://api.fish.audio/v1/tts", headers=headers, json=body, timeout=120)
    if r.status_code != 200:
        sys.exit(f"Fish Audio error {r.status_code}: {r.text[:500]}")
    out_path.write_bytes(r.content)


def build_audio(lines, workdir):
    paths = [workdir / f"s{i:03d}.wav" for i in range(len(lines))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(tts, lines, paths))

    # Normalize every clip to the same PCM format, then join with short gaps.
    timeline, frames_out, t = [], [], 0.0
    rate = 44100
    for path in paths:
        norm = path.with_suffix(".norm.wav")
        sh("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
           "-ac", "1", "-ar", str(rate), "-sample_fmt", "s16", str(norm))
        with wave.open(str(norm)) as w:
            data = w.readframes(w.getnframes())
            dur = w.getnframes() / rate
        timeline.append((t, t + dur))
        frames_out.append(data)
        silence = b"\x00\x00" * int(GAP_SECONDS * rate)
        frames_out.append(silence)
        t += dur + GAP_SECONDS

    voice = workdir / "voice.wav"
    with wave.open(str(voice), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(frames_out))
    return voice, timeline, t


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Ubuntu (fonts-dejavu-core)
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",    # macOS
]


MONO_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",  # Ubuntu (fonts-dejavu-core)
    "/System/Library/Fonts/Menlo.ttc",                           # macOS
]


def mono_font(size):
    path = os.environ.get("CODE_FONT") or next((f for f in MONO_CANDIDATES if Path(f).exists()), None)
    if not path:
        sys.exit("No code font found. Set CODE_FONT to a monospace .ttf path.")
    return ImageFont.truetype(path, size)


def code_rows(code):
    """Split diff lines into (marker, body) and remove the indent that all bodies share."""
    rows = []
    for raw in code.expandtabs(4).splitlines():
        marker, body = (raw[0], raw[1:]) if raw[:1] in "+- " and raw else (" ", raw)
        rows.append((marker, body.rstrip()))
    while rows and not rows[-1][1].strip():
        rows.pop()
    indent = min((len(b) - len(b.lstrip()) for _, b in rows if b.strip()), default=0)
    return [(m, b[indent:]) for m, b in rows]


def draw_code_card(img, code, top=170):
    """Draw a dark code card with diff colors and soft-wrapped lines. Return the bottom y of the card."""
    draw = ImageDraw.Draw(img)
    font = mono_font(26)
    pad, line_h = 22, 36
    max_w = WIDTH - 60 - 2 * pad
    colors = {"+": (110, 230, 120), "-": (255, 120, 120), " ": (220, 220, 230)}
    rows = []
    for marker, body in code_rows(code):
        text = f"{marker if marker != ' ' else ' '} {body}"
        while draw.textlength(text, font=font) > max_w:
            cut = len(text)
            while cut > 1 and draw.textlength(text[:cut], font=font) > max_w:
                cut -= 1
            space = text.rfind(" ", 4, cut)
            cut = space if space > 4 else cut
            rows.append((marker, text[:cut]))
            text = "    " + text[cut:].lstrip()
        rows.append((marker, text))
    rows = rows[:11]
    bottom = top + 2 * pad + line_h * len(rows)
    draw.rounded_rectangle((30, top, WIDTH - 30, bottom), radius=18, fill=(18, 18, 26, 235),
                           outline=(90, 90, 110, 255), width=2)
    for i, (marker, row) in enumerate(rows):
        draw.text((30 + pad, top + pad + i * line_h), row, font=font, fill=colors[marker])
    return bottom


def caption_font(size):
    path = os.environ.get("CAPTION_FONT") or next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
    if not path:
        sys.exit("No caption font found. Set CAPTION_FONT to a .ttf path.")
    return ImageFont.truetype(path, size)


def character_image(key, cache={}):
    if key not in cache:
        img = Image.open(CHARACTER_DIR / f"{key}.png").convert("RGBA")
        cache[key] = img.resize((round(img.width * CHARACTER_HEIGHT / img.height), CHARACTER_HEIGHT))
    return cache[key]


def wrap(draw, text, font, max_width):
    lines, line = [], ""
    for word in text.split():
        test = f"{line} {word}".strip()
        if line and draw.textlength(test, font=font) > max_width:
            lines.append(line)
            line = word
        else:
            line = test
    lines.append(line)
    return "\n".join(lines)


def frame_png(out_path, speaker=None, text="", section="", code=""):
    """One transparent overlay frame: section label, code card, speaking character, caption."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if section:
        draw.text((WIDTH / 2, 110), section, font=caption_font(44), fill=(255, 229, 0), anchor="mm",
                  stroke_width=5, stroke_fill="black")
    caption_y = HEIGHT * 0.42
    if code.strip():
        caption_y = max(caption_y, draw_code_card(img, code) + 100)
    if speaker:
        char = character_image(speaker)
        x = 0 if CHARACTERS[speaker]["side"] == "left" else WIDTH - char.width
        img.alpha_composite(char, (max(0, x), HEIGHT - char.height))
    if text:
        font = caption_font(66)
        draw.multiline_text((WIDTH / 2, caption_y), wrap(draw, text, font, WIDTH - 80), font=font,
                            fill=CHARACTERS[speaker]["color"], anchor="mm", align="center", spacing=10,
                            stroke_width=7, stroke_fill="black")
    img.save(out_path)


def build_captions(lines, timeline, workdir):
    """Render one overlay PNG per caption chunk and an ffmpeg concat list that times them."""
    entries, t, n = [], 0.0, 0
    for line, (start, end) in zip(lines, timeline):
        section = SECTIONS[line["section"]]
        if start > t:
            gap = workdir / f"g{n:04d}.png"
            frame_png(gap, line["speaker"], "", section, line["code"])
            entries.append((gap, start - t))
        words = line["text"].split()
        chunks = [" ".join(words[i:i + 3]) for i in range(0, len(words), 3)]
        total = sum(len(c) for c in chunks)
        for chunk in chunks:
            png = workdir / f"c{n:04d}.png"
            n += 1
            frame_png(png, line["speaker"], chunk.upper(), section, line["code"])
            entries.append((png, (end - start) * len(chunk) / total))
        t = end
    listing = [f"file '{png}'\nduration {d:.3f}" for png, d in entries]
    listing.append(f"file '{entries[-1][0]}'")  # concat demuxer needs the last file twice
    path = workdir / "captions.txt"
    path.write_text("\n".join(listing) + "\n")
    return path


def render(voice, captions, duration, out_path):
    bg_len = float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "csv=p=0", str(BACKGROUND)))
    start = random.uniform(0, max(0.0, bg_len - duration))
    sh("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
       "-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", str(BACKGROUND),
       "-f", "concat", "-safe", "0", "-i", str(captions),
       "-i", str(voice),
       "-filter_complex", "[1:v]fps=30,format=rgba[c];[0:v][c]overlay=0:0:eof_action=pass[v]",
       "-map", "[v]", "-map", "2:a", "-t", f"{duration:.2f}",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_path))


def comment_body(script, video_url, pr):
    transcript = "\n\n".join(
        f"**{CHARACTERS[l['speaker']]['name']}:** {l['text']}"
        + (f"\n```diff\n{l['code']}\n```" if l["code"].strip() else "")
        for l in script["lines"])
    return (f"## Peter Review: PR #{pr}\n\n"
            f"[Watch the video]({video_url})\n\n"
            f"<details><summary>Transcript</summary>\n\n{transcript}\n\n</details>\n")


def post(repo, pr, video, script):
    tag = "peter-review"
    exists = subprocess.run(["gh", "release", "view", tag, "--repo", repo], capture_output=True).returncode == 0
    if not exists:
        sh("gh", "release", "create", tag, "--repo", repo, "--title", "Peter Review videos",
           "--notes", "Videos made by the Peter Review bot.", "--prerelease")
    sh("gh", "release", "upload", tag, str(video), "--repo", repo, "--clobber")
    url = f"https://github.com/{repo}/releases/download/{tag}/{video.name}"
    sh("gh", "pr", "comment", str(pr), "--repo", repo, "--body", comment_body(script, url, pr))
    return url


def main():
    p = argparse.ArgumentParser(description="Make a parkour explainer video for a GitHub PR.")
    p.add_argument("--repo", required=True, help="owner/name")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--commit", help="commit SHA, for repos that do not use PRs")
    p.add_argument("--out", type=Path, help="output .mp4 path (default: out/pr-<n>.mp4)")
    p.add_argument("--post", action="store_true", help="upload the video and comment on the PR")
    p.add_argument("--script", type=Path, help="reuse a saved script .json instead of calling Claude")
    args = p.parse_args()

    check_assets()
    if args.post and not args.pr:
        sys.exit("--post works only with --pr")
    name = f"pr-{args.pr}" if args.pr else f"commit-{args.commit[:12]}"
    out = args.out or ROOT / "out" / f"{name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.script:
        script = json.loads(args.script.read_text())
    else:
        meta, diff = fetch_pr(args.repo, args.pr) if args.pr else fetch_commit(args.repo, args.commit)
        print(f"{name}: {meta['title']} (+{meta['additions']} -{meta['deletions']})")
        script = write_script(meta, diff)
        out.with_suffix(".json").write_text(json.dumps(script, indent=2))
    lines = script["lines"]
    print(f"Script: {len(lines)} lines")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        voice, timeline, duration = build_audio(lines, work)
        print(f"Voice: {duration:.1f}s")
        captions = build_captions(lines, timeline, work)
        render(voice, captions, duration, out)
    print(f"Video: {out}")

    if args.post:
        print(f"Posted: {post(args.repo, args.pr, out, script)}")


if __name__ == "__main__":
    main()
