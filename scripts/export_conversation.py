"""Export the real AI-assistant conversation for the submission, unedited.

    python scripts/export_conversation.py [path/to/transcript.jsonl | conversation-id]

Default input: the newest Cursor agent transcript of this project (or the newest whose name starts with the given
conversation id). Writes
  conversation/transcript.jsonl         the raw transcript, byte for byte
  conversation/CONVERSATION_HISTORY.md  the same messages, readable: every user and assistant text in full and in
                                        order; each tool call as one line with its name and the start of its input
Run it after the last message of the conversation, so nothing is missing."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPTS = Path.home() / ".cursor" / "projects" / "e-AI-Vaani-AI" / "agent-transcripts"
OUT = ROOT / "conversation"
TOOL_INPUT_CHARS = 300


def newest_transcript(conversation_id: str = "") -> Path:
    files = sorted(TRANSCRIPTS.rglob(f"{conversation_id}*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"no transcripts under {TRANSCRIPTS}; pass the path explicitly")
    return files[-1]


def render(lines: list[str]) -> str:
    out = ["# Conversation history with the AI assistant (Cursor agent)", "",
           "Unedited export of `transcript.jsonl` in this folder. Tool calls are shown as one line each "
           f"(name and the first {TOOL_INPUT_CHARS} characters of the input); the raw file has them in full.", ""]
    turn = 1
    for line in lines:
        rec = json.loads(line)
        if rec.get("type") == "turn_ended":
            out += ["---", ""]
            turn += 1
            continue
        role = rec.get("role")
        if role not in ("user", "assistant"):
            continue
        texts, tools = [], []
        for part in rec.get("message", {}).get("content", []):
            if part.get("type") == "text":
                texts.append(part["text"])
            elif part.get("type") == "tool_use":
                arg = json.dumps(part.get("input", {}), ensure_ascii=False)
                tools.append(f"- tool `{part.get('name')}`: `{arg[:TOOL_INPUT_CHARS].replace('`', "'")}"
                             f"{'…' if len(arg) > TOOL_INPUT_CHARS else ''}`")
        if not texts and not tools:
            continue
        out.append(f"### {'User' if role == 'user' else 'Assistant'} (turn {turn})")
        out.append("")
        out += [t.rstrip() + "\n" for t in texts]
        out += tools + ([""] if tools else [])
    return "\n".join(out) + "\n"


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    src = Path(arg) if arg.endswith(".jsonl") else newest_transcript(arg)
    OUT.mkdir(exist_ok=True)
    shutil.copyfile(src, OUT / "transcript.jsonl")
    lines = src.read_text(encoding="utf-8").splitlines()
    (OUT / "CONVERSATION_HISTORY.md").write_text(render(lines), encoding="utf-8")
    print(f"{len(lines)} records from {src} -> {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
