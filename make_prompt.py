"""Build the "Translate" prompt from allowed_chars.json and a story file.

Run from the ChineseStudy folder:
    python3 make_prompt.py                                  # newest .txt in "Simplified English"
    python3 make_prompt.py The_Fox_and_the_Grapes_Simplified.txt
    python3 make_prompt.py story.txt --inline               # paste the story text into the prompt

The prompt is saved as translate_prompt.txt and copied to the clipboard (macOS).
"""
import argparse, json, subprocess, sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ALLOWED = BASE / "allowed_chars.json"
STORY_DIR = BASE / "Simplified English"
OUT = BASE / "translate_prompt.txt"

TEMPLATE = """You are translating an English story into Traditional Chinese (with the exception of proper nouns, acronyms, and quotes) for a learner
who knows only some characters. Translation quality matters most: keep the
meaning, the events, and the tone of the story. Follow the character
guidance as closely as you can without hurting the meaning.

CHARACTERS (all Traditional)
Mastered (use freely): {{MASTERED}}
Learning (use freely, these need practice): {{LEARNING}}
Preferred new characters: {{UNKNOWN_POOL}}
Not restricted: digits, punctuation, spaces, emoji.

GUIDANCE
1. DO NOT TRANSLATE proper nouns, acronyms, and quotes. Keep these in English.
2. Write in Traditional characters, not simplified.
3. Prefer Mastered and Learning characters.
4. If an important idea cannot be written with them (for example an animal
   or object in the story), use a new character. Choose from the preferred
   list first. If it has nothing suitable, you may use any other character.
5. Use at most 50 different new characters in total, meaning characters that
   are not in the Mastered or Learning lists. Fewer is better. There is no
   minimum. Where it reads naturally, reuse a new character so the learner
   sees it more than once.
6. Keep the grammar simple and natural.

OUTPUT FORMAT
{The full translation, adhering to the rules above}
NOTES: anything you simplified or changed because of the new-character
limit, or "none" IN ENGLISH

STORY
{{STORY}}
"""


def is_han(ch):
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def find_story(arg):
    if arg:
        p = Path(arg).expanduser()
        if not p.exists():
            p = STORY_DIR / arg
        if not p.exists():
            sys.exit(f"Story file not found: {arg}\n(also looked in {STORY_DIR})")
        return p
    files = sorted(STORY_DIR.glob("*.txt"), key=lambda f: f.stat().st_mtime) if STORY_DIR.exists() else []
    if not files:
        sys.exit(f"No .txt files found in {STORY_DIR}")
    return files[-1]


def main():
    ap = argparse.ArgumentParser(description="Build the Translate prompt.")
    ap.add_argument("story", nargs="?", help="story file name or path (default: newest in Simplified English)")
    ap.add_argument("--inline", action="store_true", help="paste the story text into the prompt")
    ap.add_argument("--no-copy", action="store_true", help="don't copy the prompt to the clipboard")
    args = ap.parse_args()

    if not ALLOWED.exists():
        sys.exit("allowed_chars.json not found next to this script. Run pleco_import.py first.")
    a = json.loads(ALLOWED.read_text(encoding="utf-8"))

    story = find_story(args.story)
    text = story.read_text(encoding="utf-8").strip()
    if args.inline:
        story_block = "Translate this story:\n\n" + text
    else:
        story_block = ("Read the English story from this file and translate all of it:\n"
                       + str(story.resolve()))

    prompt = (TEMPLATE
              .replace("{{MASTERED}}", a["mastered"])
              .replace("{{LEARNING}}", a["learning"])
              .replace("{{UNKNOWN_POOL}}", "".join(u["char"] for u in a["unknown"]))
              .replace("{{STORY}}", story_block))
    OUT.write_text(prompt, encoding="utf-8")

    copied = False
    if not args.no_copy:
        try:
            subprocess.run(["pbcopy"], input=prompt.encode("utf-8"), check=True)
            copied = True
        except (OSError, subprocess.CalledProcessError):
            pass

    # Rough size estimate: ~1.5 tokens per Chinese character, ~4 characters per token otherwise.
    han = sum(is_han(c) for c in prompt)
    est = int(han * 1.5 + (len(prompt) - han) / 4)
    story_tokens = 0 if args.inline else int(len(text) / 4)
    need = est + story_tokens + 1500                      # 1,500 left for the reply
    ctx = next((c for c in (4096, 8192, 16384, 32768) if c >= need), 65536)

    when = datetime.fromtimestamp(a["exported"]).strftime("%m/%d/%Y") if a.get("exported") else "unknown date"
    print(f"Prompt saved to {OUT.name}" + (" and copied to the clipboard" if copied else ""))
    print(f"Story: {story.name}" + (" (pasted into the prompt)" if args.inline else " (the prompt gives its full path for the model to read)"))
    print(f"Characters from the Pleco export of {when}: {len(a['mastered'])} mastered, "
          f"{len(a['learning'])} learning, {len(a['unknown'])} new candidates")
    print(f"Estimated size: about {est:,} tokens for the prompt, about {need:,} with the story and reply. "
          f"Set the context length to at least {ctx:,}.")


if __name__ == "__main__":
    main()
