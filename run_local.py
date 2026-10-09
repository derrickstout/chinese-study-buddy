"""Run the Simplify and Translate pipeline from Terminal, with no Synology Chat.

Full flow (refresh character lists, simplify, translate, save):
    python3 run_local.py article.txt
    python3 run_local.py --clipboard              # take the article from the clipboard
    cat article.txt | python3 run_local.py        # or pipe it in
    python3 run_local.py article.txt --copy       # also copy the Chinese to the clipboard

One section at a time:
    python3 run_local.py article.txt --simplify-only
    python3 run_local.py --translate-only [story] # a story in "Simplified English" (default: newest)
    python3 run_local.py --refresh                # only update the character lists from the newest Pleco export

Uses the same config.json, prompts, and folders as the chat bot (it imports chat_bot.py).
LM Studio's server must be running; nothing here needs your home network.
"""
import argparse, subprocess, sys, urllib.error
from pathlib import Path

import chat_bot
import make_prompt


def check_server(cfg):
    try:
        chat_bot.llm_request(cfg, "/models", timeout=5)
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Can't reach the model server at {cfg['llm_url']}: {e}\n"
                 "Start LM Studio's server first.")


def get_article(args):
    if args.clipboard:
        try:
            text = subprocess.run(["pbpaste"], capture_output=True, check=True).stdout.decode("utf-8")
        except (OSError, subprocess.CalledProcessError):
            sys.exit("Could not read the clipboard.")
    elif args.article:
        p = Path(args.article).expanduser()
        if not p.exists():
            sys.exit(f"File not found: {args.article}")
        text = p.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        sys.exit("Give an article file, use --clipboard, or pipe text in. Run with --help for examples.")
    text = text.strip()
    if not text:
        sys.exit("The article is empty.")
    return text


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        print("\nCopied the Chinese to the clipboard.")
    except (OSError, subprocess.CalledProcessError):
        print("\nCould not copy to the clipboard.")


def main():
    ap = argparse.ArgumentParser(description="Run the pipeline from Terminal.")
    ap.add_argument("article", nargs="?", help="text file with the article")
    ap.add_argument("--clipboard", action="store_true", help="read the article from the clipboard")
    ap.add_argument("--copy", action="store_true", help="copy the Chinese to the clipboard when done")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--simplify-only", action="store_true", help="simplify and save, no translation")
    mode.add_argument("--translate-only", nargs="?", const="", metavar="STORY",
                      help="translate a saved story (default: the newest in Simplified English)")
    mode.add_argument("--refresh", action="store_true", help="only update the character lists")
    args = ap.parse_args()

    cfg = chat_bot.load_config()
    # The pipeline code sends its messages through send_chat; here they just print.
    chat_bot.send_chat = lambda cfg, user_id, text: print("\n" + text)

    if args.refresh:
        chat_bot.refresh_characters(cfg, 0)
        return 0

    check_server(cfg)

    if args.simplify_only:
        simplified = chat_bot.simplify(cfg, get_article(args))
        path = chat_bot.save_simplified(simplified)
        print("\n" + simplified)
        print(f"\nSaved as {path}")
        return 0

    if args.translate_only is not None:
        story = make_prompt.find_story(args.translate_only or None).resolve()
        print(f"Translating {story.name}")
        chat_bot.refresh_characters(cfg, 0)
        chinese, notes = chat_bot.translate(cfg, story)
        out = chat_bot.save_translation(cfg, chinese, story)
        print("\n" + chinese)
        print(f"\nSaved as {out}")
        if notes:
            print(f"Model notes: {notes}")
        if args.copy:
            copy_to_clipboard(chinese)
        return 0

    article = get_article(args)
    print(f"Article: {len(article):,} characters. Starting the full flow.")
    result = chat_bot.run_job(cfg, 0, article)
    if result["ok"] and args.copy:
        folder = Path(cfg.get("chinese_texts_dir") or chat_bot.BASE / "Chinese Study Texts").expanduser()
        copy_to_clipboard((folder / result["file"]).read_text(encoding="utf-8").strip())
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:                      # model or prompt problems: a plain message, no traceback
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
