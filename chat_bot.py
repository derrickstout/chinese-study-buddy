"""Chinese Study Buddy: receives Synology Chat messages, simplifies the text with a local model,
translates it into Chinese, saves both, and replies.

    python3 chat_bot.py              # start the bot
    python3 chat_bot.py --test-llm   # check the connection to LM Studio's local server

Settings live in config.json, next to this script. Standard library only.
"""
import hmac, json, re, shutil, socket, ssl, subprocess, sys, threading, time, traceback
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
PROMPTS_DIR = BASE / "prompts"
SIMPLIFY_PROMPT = PROMPTS_DIR / "simplify.txt"
TRANSLATE_PROMPT_FILE = PROMPTS_DIR / "translate_prompt.txt"   # replaced by each new job
SIMPLIFIED_DIR = BASE / "Simplified English"
MAKE_PROMPT = BASE / "make_prompt.py"
MAKE_PROMPT_OUTPUT = BASE / "translate_prompt.txt"           # where make_prompt.py writes it
ALLOWED_FILE = BASE / "allowed_chars.json"
JOB_LOCK = threading.Lock()            # one job at a time; others wait their turn
STATE_LOCK = threading.Lock()
STATE = {"current": None, "waiting": 0, "last": None}   # what /status reports


def load_config():
    if not CONFIG.exists():
        sys.exit("config.json not found next to this script.")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


LAST_GOOD_CONFIG = {}                  # the last config.json that parsed cleanly
LOADED_MODEL = {"name": None}          # the model this bot last asked LM Studio to use


def current_config():
    """Re-read config.json every time it is needed, so edits (including llm_model) take effect
    without restarting the bot. If the file is missing or half-saved, keep the last good copy."""
    global LAST_GOOD_CONFIG
    try:
        LAST_GOOD_CONFIG = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  could not re-read config.json ({e}); using the previous settings")
    return LAST_GOOD_CONFIG


def find_lms(cfg):
    """LM Studio's command-line tool, or None. Set "lms_path" in config.json if it isn't found."""
    for candidate in (cfg.get("lms_path"), shutil.which("lms"), Path.home() / ".lmstudio" / "bin" / "lms"):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def ensure_model_switched(cfg):
    """If llm_model differs from the model used last (or this is the first job since the bot
    started), unload everything from LM Studio so the new model has the memory to load.
    Call this only while holding JOB_LOCK, so no request is mid-generation."""
    wanted = cfg.get("llm_model")
    if not wanted or LOADED_MODEL["name"] == wanted:
        return
    previous = LOADED_MODEL["name"] or "whatever was loaded before"
    lms = find_lms(cfg)
    if not lms:
        print("  model changed but the 'lms' tool was not found, so LM Studio's old model was not "
              'unloaded. Set "lms_path" in config.json, or unload it in LM Studio yourself.')
    else:
        print(f"  switching model: {previous} -> {wanted}; unloading LM Studio's loaded models first")
        try:
            r = subprocess.run([lms, "unload", "--all"], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                print(f"  lms unload failed: {(r.stderr or r.stdout).strip()[:200]}")
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"  lms unload failed: {e}")
    LOADED_MODEL["name"] = wanted


# ---------- Synology Chat: sending replies ----------

def send_chat(cfg, user_id, text):
    """Post a message to one user through the bot's incoming webhook."""
    payload = json.dumps({"text": text, "user_ids": [user_id]})
    data = urllib.parse.urlencode({"payload": payload}).encode()
    req = urllib.request.Request(cfg["incoming_url"], data=data, method="POST")
    ctx = None if cfg.get("verify_ssl", True) else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            print(f"  reply sent: {r.read().decode('utf-8', 'replace')[:80]}")
    except (urllib.error.URLError, OSError) as e:
        print(f"  could not send the reply: {e}")


# ---------- LM Studio's local server ----------

def llm_request(cfg, path, body=None, timeout=300):
    url = cfg["llm_url"].rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def llm_chat(cfg, prompt, text=None, max_tokens=None):
    """Send a prompt to the model and return its reply as plain text.

    With text, the prompt is the system message and the text is the user message.
    Without it, the prompt is sent as a single user message."""
    if not cfg.get("llm_model"):
        raise RuntimeError('No model set. Add "llm_model" to config.json.')
    suffix = cfg.get("llm_user_suffix", "")
    if text is None:
        messages = [{"role": "user", "content": (prompt + "\n" + suffix).strip()}]
    else:
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": (text + "\n" + suffix).strip()}]
    body = {
        "model": cfg["llm_model"],
        "temperature": cfg.get("llm_temperature", 0.3),
        "max_tokens": max_tokens or cfg.get("llm_max_tokens", 4096),
        "ttl": cfg.get("llm_ttl", 600),            # unload the model after this many idle seconds
        "messages": messages,
    }
    body.update(cfg.get("llm_extra", {}))          # any extra request settings, e.g. to switch off thinking
    started = time.time()
    try:
        r = llm_request(cfg, "/chat/completions", body, timeout=cfg.get("llm_timeout", 900))
    except urllib.error.HTTPError as e:
        raise RuntimeError("The model server returned an error: "
                           + e.read().decode("utf-8", "replace")[:300])
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Could not reach the model server: {e}")
    used = (r.get("usage") or {}).get("completion_tokens")
    if used:                                       # handy for comparing models
        secs = max(time.time() - started, 0.1)
        print(f"  model wrote {used:,} tokens in {secs:.0f}s ({used / secs:.1f} tokens/s)")
    choice = r["choices"][0]
    text = re.sub(r"<think>.*?</think>", "", choice["message"].get("content") or "", flags=re.S).strip()
    if not text:
        finish = choice.get("finish_reason")
        if finish == "length":
            raise RuntimeError(
                f"The model hit the llm_max_tokens limit ({body['max_tokens']}) before writing an answer. "
                "This is separate from context length. A thinking model can use thousands of tokens "
                "reasoning first: raise llm_max_tokens, switch off thinking, or use a non-thinking model.")
        raise RuntimeError(f"The model returned no text (finish reason: {finish}).")
    return text


def test_llm(cfg):
    try:
        models = llm_request(cfg, "/models", timeout=10)
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Could not reach {cfg['llm_url']}: {e}\nIs the LM Studio server running?")
    print("Models the server lists:")
    for m in models.get("data", []):
        print("  " + m["id"])
    model = cfg.get("llm_model")
    if not model:
        sys.exit('\nSet "llm_model" in config.json to one of the names above, then run this again.')
    print(f"\nAsking {model} a test question (the first call can be slow while the model loads)...")
    try:
        question = ("Reply with the single word: ready\n" + cfg.get("llm_user_suffix", "")).strip()
        body = {"model": model, "temperature": 0, "max_tokens": 300,
                "ttl": cfg.get("llm_ttl", 600),
                "messages": [{"role": "user", "content": question}]}
        body.update(cfg.get("llm_extra", {}))
        r = llm_request(cfg, "/chat/completions", body)
        choice = r["choices"][0]
        msg = choice["message"]
        text = (msg.get("content") or "").strip()
        print("Model replied:", text or "(nothing)")
        print("Finish reason:", choice.get("finish_reason"))
        print("Tokens generated:", (r.get("usage") or {}).get("completion_tokens", "unknown"),
              "(a one-word answer with thinking off is usually under 10)")
        if msg.get("reasoning_content"):
            print(f"The model also produced {len(msg['reasoning_content'])} characters of hidden reasoning.")
        if not text:
            print("The reply was empty. This is what the server sent back:")
            print(json.dumps(msg, ensure_ascii=False, indent=1)[:600])
    except (urllib.error.URLError, OSError, KeyError) as e:
        sys.exit(f"The request failed: {e}")


# ---------- The pipeline ----------

def simplify(cfg, article):
    if not SIMPLIFY_PROMPT.exists():
        raise RuntimeError("prompts/simplify.txt not found.")
    return llm_chat(cfg, SIMPLIFY_PROMPT.read_text(encoding="utf-8").strip(), article)


def save_simplified(text):
    SIMPLIFIED_DIR.mkdir(exist_ok=True)
    slug = "_".join(re.findall(r"[A-Za-z0-9]+", text)[:6])[:50] or "story"
    stem = f"{time.strftime('%Y-%m-%d_%H%M')}_{slug}"
    path, n = SIMPLIFIED_DIR / f"{stem}.txt", 2
    while path.exists():                           # never overwrite an earlier story
        path, n = SIMPLIFIED_DIR / f"{stem}_{n}.txt", n + 1
    path.write_text(text + "\n", encoding="utf-8")
    return path


def newest_pleco_export(folder):
    """Newest Pleco flashcard export in the folder, as (path, export timestamp), or None."""
    if not folder.is_dir():
        return None
    files = [p for p in folder.iterdir() if p.suffix.lower() in (".xml", ".txt")]
    for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            head = f.read_bytes()[:2000].decode("utf-8", "replace")
        except OSError:
            continue
        if "<plecoflash" in head:
            m = re.search(r'<plecoflash[^>]*\bcreated="(\d+)"', head)
            return f, int(m.group(1)) if m else int(f.stat().st_mtime)
    return None


def refresh_characters(cfg, user_id):
    """If a newer Pleco export has been saved, rebuild the character files before the job."""
    found = newest_pleco_export(BASE / cfg.get("pleco_exports_dir", "Pleco Exports"))
    if not found:
        print("  no Pleco export found; using the existing character lists")
        return
    export, created = found
    have = 0
    if ALLOWED_FILE.exists():
        try:
            have = json.loads(ALLOWED_FILE.read_text(encoding="utf-8")).get("exported", 0)
        except ValueError:
            pass
    if created <= have:
        print("  character lists are up to date")
        return
    script = BASE / cfg.get("pleco_import_script", "pleco_import.py")
    out_dir = BASE / cfg.get("snapshot_dir", "data")
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(script), str(export), str(out_dir)],
                       capture_output=True, text=True, cwd=BASE)
    if r.returncode != 0:
        reason = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
        if "ParseError" in reason:
            reason = "the file looks incomplete, perhaps still syncing from iCloud"
        print(f"  Pleco import failed: {reason}")
        send_chat(cfg, user_id, f"I couldn't update your character lists from {export.name} "
                                f"({reason}). Using the previous lists.")
        return
    counts = json.loads(ALLOWED_FILE.read_text(encoding="utf-8")).get("counts", {})
    when = time.strftime("%m/%d/%Y", time.localtime(created))
    print(f"  character lists updated from {export.name}")
    send_chat(cfg, user_id, f"Updated your character lists from the Pleco export of {when}: "
                            f"{counts.get('mastered', '?')} mastered, {counts.get('learning', '?')} learning.")


def build_translate_prompt(story_path):
    """Run make_prompt.py on the saved story. The simplified text is pasted into the prompt."""
    if not MAKE_PROMPT.exists():
        raise RuntimeError("make_prompt.py not found next to chat_bot.py.")
    r = subprocess.run([sys.executable, str(MAKE_PROMPT), str(story_path), "--inline", "--no-copy"],
                       capture_output=True, text=True, cwd=BASE)
    if r.returncode != 0:
        raise RuntimeError("Could not build the Translate prompt: " + (r.stderr or r.stdout).strip()[:300])
    print("  " + r.stdout.strip().replace("\n", "\n  "))      # make_prompt's size summary
    PROMPTS_DIR.mkdir(exist_ok=True)
    if MAKE_PROMPT_OUTPUT.exists():                # move it into the prompts folder, replacing the old one
        MAKE_PROMPT_OUTPUT.replace(TRANSLATE_PROMPT_FILE)
    return TRANSLATE_PROMPT_FILE.read_text(encoding="utf-8")


def split_translation(reply):
    """Separate the Chinese from an optional NOTES section, and drop any <zh> tags."""
    parts = re.split(r"^\s*NOTES:", reply, maxsplit=1, flags=re.M)
    chinese = re.sub(r"</?zh>", "", parts[0]).strip()
    notes = parts[1].strip() if len(parts) > 1 else ""
    if notes.strip(" .\"'").lower() == "none":
        notes = ""
    return chinese, notes


def translate(cfg, story_path):
    reply = llm_chat(cfg, build_translate_prompt(story_path), max_tokens=cfg.get("translate_max_tokens"))
    chinese, notes = split_translation(reply)
    if not chinese:
        raise RuntimeError("The model's reply had no translation in it.")
    return chinese, notes


def save_translation(cfg, text, story_path):
    folder = Path(cfg.get("chinese_texts_dir") or BASE / "Chinese Study Texts").expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    path, n = folder / f"{story_path.stem}.txt", 2
    while path.exists():
        path, n = folder / f"{story_path.stem}_{n}.txt", n + 1
    path.write_text(text + "\n", encoding="utf-8")
    return path


def fmt_duration(seconds):
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def set_step(step):
    with STATE_LOCK:
        if STATE["current"]:
            STATE["current"].update(step=step, step_started=time.time())


def say(cfg, user_id, text):
    """A progress message. Switch these off with "status_messages": false in config.json."""
    if cfg.get("status_messages", True):
        send_chat(cfg, user_id, text)


def status_text(cfg):
    with STATE_LOCK:
        cur = dict(STATE["current"]) if STATE["current"] else None
        last, waiting = STATE["last"], STATE["waiting"]
    now = time.time()
    lines = []
    if cur:
        lines.append(f"Working: {cur['step']} ({fmt_duration(now - cur['step_started'])} in this step, "
                     f"{fmt_duration(now - cur['started'])} in total).")
    else:
        lines.append("Idle.")
    if waiting:
        lines.append(f"{waiting} job{'s' if waiting > 1 else ''} waiting.")
    if last:
        when = time.strftime("%I:%M %p", time.localtime(last["finished"]))
        if last["ok"]:
            lines.append(f"Last job: finished at {when}, took {fmt_duration(last['seconds'])}, "
                         f"saved as {last['file']}.")
        else:
            lines.append(f"Last job: failed at {when}: {last['error']}")
    elif not cur:
        lines.append("No jobs yet since the bot started.")
    lines.append(f"Model: {cfg.get('llm_model') or '(not set)'}.")
    return "\n".join(lines)


def run_job(cfg, user_id, article):
    """Returns a short summary for /status: {"ok", "file", "error"}."""
    job_started = time.time()
    set_step("checking the model")
    ensure_model_switched(cfg)                     # unload the old model first if llm_model changed
    set_step("checking for a new Pleco export")
    refresh_characters(cfg, user_id)               # pick up a newer Pleco export, if there is one

    set_step("simplifying")
    started = time.time()
    simplified = simplify(cfg, article)
    story_path = save_simplified(simplified)
    print(f"  simplified in {time.time() - started:.0f}s, saved {story_path.name}")
    send_chat(cfg, user_id, simplified)            # the English on its own, easy to copy
    say(cfg, user_id, f"Simplified in {fmt_duration(time.time() - started)}. Translating now...")

    set_step("translating")
    step = time.time()
    try:
        chinese, notes = translate(cfg, story_path)
    except Exception as e:
        traceback.print_exc()
        send_chat(cfg, user_id, f"The simplified story was saved as {story_path.name}, "
                                f"but the translation failed: {e}")
        return {"ok": False, "file": story_path.name, "error": f"translation failed: {e}"}
    out = save_translation(cfg, chinese, story_path)
    print(f"  translated in {time.time() - step:.0f}s, saved {out.name}")
    send_chat(cfg, user_id, chinese)               # the Chinese on its own, ready to copy into Pleco
    note = f"Saved as {out.name}"
    if cfg.get("status_messages", True):
        note = f"Done in {fmt_duration(time.time() - job_started)}. " + note
    if notes:
        note += f"\nModel notes: {notes}"
    send_chat(cfg, user_id, note)
    return {"ok": True, "file": out.name, "error": ""}


def handle_message(cfg, user_id, text):
    """Runs in its own thread for each message."""
    ack = f"Got it. Received {len(text):,} characters."
    if not cfg.get("allowed_user_ids"):
        ack += (f" (Setup mode: add {user_id} to allowed_user_ids in config.json "
                "to lock this bot to you.)")
    if JOB_LOCK.acquire(blocking=False):
        send_chat(cfg, user_id, ack + " Simplifying now.")
    else:
        with STATE_LOCK:
            STATE["waiting"] += 1
        send_chat(cfg, user_id, ack + " Another job is running, so yours is queued.")
        JOB_LOCK.acquire()
        with STATE_LOCK:
            STATE["waiting"] -= 1
        send_chat(cfg, user_id, "Starting your job now.")
    started = time.time()
    with STATE_LOCK:
        STATE["current"] = {"started": started, "step": "starting", "step_started": started}
    result = {"ok": False, "file": "", "error": "stopped unexpectedly"}
    try:
        cfg = current_config()                     # fresh settings, read after any wait in the queue
        result = run_job(cfg, user_id, text)
    except Exception as e:                         # report problems to chat instead of failing silently
        traceback.print_exc()
        send_chat(cfg, user_id, f"Sorry, something went wrong: {e}")
        result = {"ok": False, "file": "", "error": str(e)}
    finally:
        with STATE_LOCK:
            STATE["last"] = dict(result, finished=time.time(), seconds=time.time() - started)
            STATE["current"] = None
        JOB_LOCK.release()


# ---------- Synology Chat: receiving messages ----------

def make_handler(startup_cfg):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, text="ok"):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                      # lets you test reachability from a browser
            self._reply(200, "Chinese Study Buddy is running.")

        def do_POST(self):
            cfg = current_config()             # fresh settings for every request
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            try:
                if raw.lstrip().startswith("{"):
                    data = json.loads(raw)
                else:
                    data = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                user_id = int(data.get("user_id"))
            except (ValueError, TypeError):
                return self._reply(400, "bad request")
            token, text = str(data.get("token", "")), str(data.get("text", ""))

            if not hmac.compare_digest(token, str(cfg["chat_token"])):
                print("Rejected a request with the wrong token.")
                return self._reply(403, "forbidden")
            allowed = cfg.get("allowed_user_ids") or []
            if allowed and user_id not in allowed:
                print(f"Ignored a message from user_id {user_id} (not in allowed_user_ids).")
                return self._reply(200)

            if text.strip().lower() in ("/status", "status"):
                print(f"[{time.strftime('%H:%M:%S')}] status request from user {user_id}")
                self._reply(200)
                threading.Thread(target=lambda: send_chat(cfg, user_id, status_text(cfg)), daemon=True).start()
                return

            print(f"[{time.strftime('%H:%M:%S')}] message from user {user_id}, {len(text):,} characters")
            self._reply(200)                   # answer Chat straight away; work happens in a thread
            threading.Thread(target=handle_message, args=(cfg, user_id, text), daemon=True).start()

        def log_message(self, *args):
            pass
    return Handler


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))       # no traffic is sent; just picks the local address
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "<your Mac's IP address>"


def main():
    global LAST_GOOD_CONFIG
    cfg = LAST_GOOD_CONFIG = load_config()
    if "--test-llm" in sys.argv:
        return test_llm(cfg)
    if str(cfg["chat_token"]).startswith("PASTE") or str(cfg["incoming_url"]).startswith("PASTE"):
        sys.exit("Fill in chat_token and incoming_url in config.json first.")
    port = int(cfg.get("port", 8765))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cfg))
    print("Chinese Study Buddy is running. Press Ctrl+C to stop.")
    print(f"Outgoing URL for the Synology Chat bot: http://{lan_ip()}:{port}/chat")
    print(f"Model: {cfg.get('llm_model') or '(not set)'}")
    if not SIMPLIFY_PROMPT.exists():
        print("Warning: prompts/simplify.txt is missing, so jobs will fail.")
    exports = BASE / cfg.get("pleco_exports_dir", "Pleco Exports")
    print(f"Pleco exports folder: {exports}" + ("" if exports.is_dir() else "  (not found)"))
    for name in ("make_prompt.py", "allowed_chars.json"):
        if not (BASE / name).exists():
            print(f"Warning: {name} is missing, so translation will fail.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()