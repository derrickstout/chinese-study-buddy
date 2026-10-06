"""Check the model's Chinese translation against the Translate prompt guidelines.

Run from the ChineseStudy folder, after copying the model's reply:
    python3 check_text.py                 # reads the clipboard (macOS)
    python3 check_text.py reply.txt       # or reads a text file

It compares every Chinese character in the reply with allowed_chars.json and reports
how many unique characters were used, how many are Mastered / Learning / Unknown,
which characters were added beyond your lists, and whether the new-character limit held.
The whole text is checked, apart from an optional trailing "NOTES:" section.
"""
import json, re, subprocess, sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
ALLOWED = BASE / "allowed_chars.json"
MAX_NEW = 10          # limit on different characters outside Mastered and Learning


def is_han(ch):
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0x20000 <= o <= 0x2A6DF or 0xF900 <= o <= 0xFAFF)


def read_input():
    if len(sys.argv) > 1:
        try:
            return Path(sys.argv[1]).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            sys.exit(f"Could not read {sys.argv[1]}: {e}")
    try:
        return subprocess.run(["pbpaste"], capture_output=True, check=True).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        sys.exit("Could not read the clipboard. Pass a file instead: python3 check_text.py reply.txt")


def main():
    if not ALLOWED.exists():
        sys.exit("allowed_chars.json not found next to this script. Run pleco_import.py first.")
    a = json.loads(ALLOWED.read_text(encoding="utf-8"))
    mastered, learning = set(a["mastered"]), set(a["learning"])
    pool = {u["char"] for u in a["unknown"]}

    raw = read_input()
    warnings = []
    # Everything is checked, except a trailing "NOTES:" section if the model wrote one.
    parts = re.split(r"^NOTES:", raw, maxsplit=1, flags=re.M)
    text = parts[0]
    notes = parts[1] if len(parts) > 1 else None

    counts = Counter(ch for ch in text if is_han(ch))
    if not counts:
        sys.exit("No Chinese characters found in the text.")

    def status(ch):
        return "mastered" if ch in mastered else "learning" if ch in learning else "unknown"

    groups = {s: [c for c in counts if status(c) == s] for s in ("mastered", "learning", "unknown")}
    occ = {s: sum(counts[c] for c in groups[s]) for s in groups}
    total_occ = sum(counts.values())
    in_pool = [c for c in groups["unknown"] if c in pool]
    outside = [c for c in groups["unknown"] if c not in pool]

    latin = re.findall(r"[A-Za-z]+", text)
    if latin:
        warnings.append("Letters found inside the translation: " + ", ".join(latin[:5])
                        + (" ..." if len(latin) > 5 else ""))

    print(f"Checked {total_occ} Chinese characters in the reply.\n")
    print(f"Total unique characters used: {len(counts)}")
    print(f"  Mastered: {len(groups['mastered'])}")
    print(f"  Learning: {len(groups['learning'])}")
    print(f"  Unknown:  {len(groups['unknown'])}  "
          f"({len(in_pool)} from the preferred list, {len(outside)} outside it)")
    pct = lambda s: round(100 * occ[s] / total_occ)
    print(f"\nBy occurrence: {pct('mastered')}% Mastered, {pct('learning')}% Learning, "
          f"{pct('unknown')}% Unknown")

    print("\nAdded characters (not in your Mastered or Learning lists):")
    if groups["unknown"]:
        for c in sorted(groups["unknown"], key=lambda c: (-counts[c], c)):
            where = "preferred list" if c in pool else "outside the list"
            print(f"  {c}  x{counts[c]}  ({where})")
    else:
        print("  none")

    if notes:
        print("\nModel's notes: " + " ".join(notes.split()))
    for w in warnings:
        print("\nWarning: " + w)

    ok = len(groups["unknown"]) <= MAX_NEW
    print(f"\nNew-character limit ({MAX_NEW}): {len(groups['unknown'])} used, "
          + ("PASS" if ok else "FAIL, over the limit"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
