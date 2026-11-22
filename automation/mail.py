"""Your inbox: reading over IMAP and sending over SMTP (Gmail by default).

Setup (.env):
  JARVIS_EMAIL_ADDRESS=you@gmail.com
  JARVIS_EMAIL_APP_PASSWORD=abcd efgh ijkl mnop   # a Google "app password", not your normal password
Optional:
  JARVIS_IMAP_HOST=imap.gmail.com
  JARVIS_MAIL_CHECK_MINUTES=10        # background check interval (0 turns it off)
  JARVIS_MAIL_ALERTS=important        # important | all | off
  JARVIS_MAIL_IMPORTANT_HINT=professors, VIT, internships, interviews, bank

Reading never marks mail as read, deletes or moves anything. Mail is only sent when you
say "send it" to a reply Jarvis read out to you, or as a short automatic "not available"
reply while you're away (JARVIS_AUTO_REPLY=on). Email text is summarised by the same language model Jarvis uses (Gemini when online,
local Ollama when offline).
"""
import datetime
import email
import email.header
import email.utils
import html
import imaplib
import json
import os
import re
import smtplib
import subprocess
import threading
from email.message import EmailMessage
from pathlib import Path

MAX_BODY_CHARS = 1500
FETCH_BYTES = 60000  # enough for the text of almost any email, without downloading attachments


class MailError(Exception):
    """A user-facing problem with reading mail."""


def is_configured():
    return bool(os.getenv("JARVIS_EMAIL_ADDRESS", "").strip() and os.getenv("JARVIS_EMAIL_APP_PASSWORD", "").strip())


def _state_path():
    base = os.getenv("LOCALAPPDATA")
    folder = Path(base) / "JarvisAssistant" if base else Path.home() / ".cache" / "jarvis_assistant"
    return folder / "mail_state.json"


def _load_state():
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _decode(value):
    parts = []
    for text, charset in email.header.decode_header(value or ""):
        if isinstance(text, bytes):
            text = text.decode(charset or "utf-8", errors="replace")
        parts.append(text)
    return " ".join("".join(parts).split())


def _body_text(message):
    plain, rich = None, None
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and rich is None:
            rich = text
    if plain is None and rich:
        rich = re.sub(r"(?is)<(script|style).*?</\1>", " ", rich)
        rich = re.sub(r"(?is)<blockquote.*?</blockquote>", " ", rich)
        rich = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", rich)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", rich))
    return " ".join(strip_quoted(plain or "").split())[:MAX_BODY_CHARS]


QUOTE_HEADER = re.compile(
    r"(?:^|\n)\s*(?:On\s.{5,200}?\swrote:|-{2,}\s*Original Message\s*-{2,}|From:\s.+\nSent:\s)",
    re.IGNORECASE | re.DOTALL,
)


def strip_quoted(text):
    """Removes the earlier messages quoted under a reply ("On Thu ... wrote:", "> ..." lines)."""
    match = QUOTE_HEADER.search(text)
    if match and match.start() > 0:
        text = text[:match.start()]
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines).strip()


AUTOMATED_ADDRESS = re.compile(
    r"(?:no-?reply|do-?not-?reply|donotreply|notifications?|alerts?|mailer-daemon|postmaster|bounce|"
    r"newsletter|updates?|news|info|support|marketing|promo)[^@]*@",
    re.IGNORECASE,
)


def parse_message(uid, raw_bytes):
    message = email.message_from_bytes(raw_bytes)
    sender_name, sender_address = email.utils.parseaddr(_decode(message.get("From")))
    _, reply_to = email.utils.parseaddr(_decode(message.get("Reply-To")))
    try:
        date = email.utils.parsedate_to_datetime(message.get("Date")).astimezone()
    except (TypeError, ValueError):
        date = None
    auto_submitted = (message.get("Auto-Submitted") or "no").strip().lower()
    precedence = (message.get("Precedence") or "").strip().lower()
    automated = bool(
        AUTOMATED_ADDRESS.search(sender_address or "")
        or message.get("List-Unsubscribe") or message.get("List-Id")
        or auto_submitted != "no" or precedence in {"bulk", "list", "junk"}
    )
    return {
        "uid": int(uid),
        "from": sender_name or sender_address,
        "address": sender_address,
        "reply_to": reply_to or sender_address,
        "subject": _decode(message.get("Subject")) or "(no subject)",
        "date": date.strftime("%d %b %I:%M %p") if date else "",
        "body": _body_text(message),
        "message_id": (message.get("Message-ID") or "").strip(),
        "references": " ".join((message.get("References") or "").split()),
        "automated": automated,
    }


def _connect():
    if not is_configured():
        raise MailError(
            "Mail isn't set up yet. Add JARVIS_EMAIL_ADDRESS and JARVIS_EMAIL_APP_PASSWORD to your .env file."
        )
    host = os.getenv("JARVIS_IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com"
    try:
        connection = imaplib.IMAP4_SSL(host, timeout=20)
        connection.login(os.environ["JARVIS_EMAIL_ADDRESS"].strip(), os.environ["JARVIS_EMAIL_APP_PASSWORD"].replace(" ", ""))
    except imaplib.IMAP4.error as exc:
        raise MailError(
            "Your mail server rejected the login. Check JARVIS_EMAIL_ADDRESS and that the app password is correct."
        ) from exc
    except OSError as exc:
        raise MailError(f"I couldn't reach your mail server: {exc}") from exc
    connection.select("INBOX", readonly=True)  # read-only: nothing gets marked as read
    return connection


def _imap_quote(text):
    return '"' + str(text).replace("\\", "").replace('"', "") + '"'


def fetch_messages(unread_only=True, limit=5, after_uid=None, days=None, from_filter=None):
    """Returns the newest messages (newest first) as dicts, without changing anything in the mailbox.

    days=1 means since yesterday's start, days=0 means today only."""
    connection = _connect()
    try:
        criteria = []
        if after_uid:
            criteria.append(f"UID {int(after_uid) + 1}:*")
        if unread_only:
            criteria.append("UNSEEN")
        if from_filter:
            criteria.append(f"FROM {_imap_quote(from_filter)}")
        if days is not None or not criteria:
            back = 7 if days is None else max(0, int(days))
            since = (datetime.date.today() - datetime.timedelta(days=back)).strftime("%d-%b-%Y")
            criteria.append(f"SINCE {since}")
        status, data = connection.uid("search", None, *criteria)
        if status != "OK":
            raise MailError("The mail server couldn't search your inbox.")
        uids = [int(uid) for uid in (data[0] or b"").split()]
        if after_uid:
            uids = [uid for uid in uids if uid > int(after_uid)]  # "N:*" always returns the last message
        messages = []
        for uid in sorted(uids, reverse=True)[:limit]:
            status, parts = connection.uid("fetch", str(uid), f"(BODY.PEEK[]<0.{FETCH_BYTES}>)")
            raw = next((item[1] for item in parts or [] if isinstance(item, tuple)), None)
            if status == "OK" and raw:
                messages.append(parse_message(uid, raw))
        return messages
    finally:
        try:
            connection.logout()
        except Exception:
            pass


def latest_uid():
    connection = _connect()
    try:
        status, data = connection.uid("search", None, "ALL")
        uids = [int(uid) for uid in (data[0] or b"").split()] if status == "OK" else []
        return max(uids) if uids else 0
    finally:
        try:
            connection.logout()
        except Exception:
            pass


def own_address():
    return os.getenv("JARVIS_EMAIL_ADDRESS", "").strip()


def send_email(to, subject, body, in_reply_to=None, references=None, automatic=False):
    """Sends an email from your account over SMTP (same app password as reading)."""
    if not is_configured():
        raise MailError("Mail isn't set up yet. Add JARVIS_EMAIL_ADDRESS and JARVIS_EMAIL_APP_PASSWORD to .env.")
    message = EmailMessage()
    message["From"] = own_address()
    message["To"] = to
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    if automatic:  # tells other mail systems not to auto-reply back (prevents reply loops)
        message["Auto-Submitted"] = "auto-replied"
        message["X-Auto-Response-Suppress"] = "All"
    message.set_content(body)
    host = os.getenv("JARVIS_SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
    try:
        with smtplib.SMTP_SSL(host, 465, timeout=30) as server:
            server.login(own_address(), os.environ["JARVIS_EMAIL_APP_PASSWORD"].replace(" ", ""))
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("Gmail rejected the login for sending. Check the app password in .env.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"I couldn't send the email: {exc}") from exc


def reply_subject(subject):
    return subject if re.match(r"^\s*re:", subject or "", re.IGNORECASE) else f"Re: {subject}"


TITLES = {"prof", "prof.", "professor", "dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "sir", "madam"}


def first_name(display_name):
    """'Rahul Verma' -> 'Rahul'; 'Prof. Rao' -> 'Prof. Rao'; an address -> 'there'."""
    if not display_name or "@" in display_name:
        return "there"
    name = re.sub(r"[^\w\s.'-]", " ", display_name).strip()
    if not name:
        return "there"
    words = name.split()
    if words[0].lower() in TITLES and len(words) > 1:
        return f"{words[0].title()} {words[-1].title()}"
    return words[0].title()


def can_auto_reply(message, state, now=None):
    """Safety rules for automatic replies: real people only, never yourself, once a day per sender."""
    now = now or datetime.datetime.now()
    address = (message.get("reply_to") or message.get("address") or "").lower()
    if not address or message.get("automated") or address == own_address().lower():
        return False
    replied = state.get("auto_replied", {})
    last = replied.get(address)
    if last and now - datetime.datetime.fromisoformat(last) < datetime.timedelta(hours=24):
        return False
    today = now.date().isoformat()
    return sum(1 for when in replied.values() if when.startswith(today)) < 20


def away_message(message, user_name):
    template = os.getenv("JARVIS_AWAY_MESSAGE", "").strip() or (
        "Hi {name},\n\nThanks for your email. {user} isn't available at the moment and will get back to you "
        "as soon as possible.\n\n(This is an automatic reply from Jarvis, {user}'s assistant.)"
    )
    return template.replace("\\n", "\n").format(name=first_name(message.get("from")), user=user_name)


def draft_reply(message, user_name, instructions=None):
    """Writes a reply from the user to one email."""
    from brain import llm

    instruction = (
        f"Write the body of {user_name}'s reply to the email below. Plain text, no subject line, short and "
        f"natural, signed '{user_name}'. "
        + (f"What {user_name} wants to say: {instructions}. " if instructions else
           f"If it needs a decision only {user_name} can make (a time, a yes or no, money), don't invent one: "
           "acknowledge it and say he will confirm soon. ")
        + f"\n\nFrom: {message['from']} <{message['address']}>\nSubject: {message['subject']}\n\n{message['body']}"
    )
    try:
        return llm.generate_text(instruction, max_tokens=500)
    except RuntimeError as exc:
        raise MailError(str(exc)) from exc


def _describe(messages):
    return "\n\n".join(
        f"[{index}] From: {item['from']} <{item['address']}>\nDate: {item['date']}\nSubject: {item['subject']}\n"
        f"Text: {item['body']}"
        for index, item in enumerate(messages, start=1)
    )


def summarize(messages, user_name="Sarthak"):
    """Spoken-style summary of messages, most important first."""
    from brain import llm

    if not messages:
        return "You have no new emails."
    hint = os.getenv("JARVIS_MAIL_IMPORTANT_HINT", "").strip()
    instruction = (
        f"These are {user_name}'s newest emails. Summarise them for him to hear out loud: for each, who it is "
        "from and what it is about in one short sentence, most important first. Say clearly if one needs action "
        "or a reply, and mention any deadline. Group obvious promotions/newsletters into one short line. "
        "No markdown, no lists of symbols."
        + (f" He considers these important: {hint}." if hint else "")
        + "\n\n" + _describe(messages)
    )
    try:
        return llm.generate_text(instruction, max_tokens=700)
    except RuntimeError:
        lines = [f"{item['from']}: {item['subject']}" for item in messages]
        return f"You have {len(messages)} new emails. " + ". ".join(lines) + "."


def triage(messages):
    """Asks the model which new messages are important. Returns a list of {uid, important, summary}."""
    from brain import llm

    hint = os.getenv("JARVIS_MAIL_IMPORTANT_HINT", "").strip()
    instruction = (
        "Decide which of these new emails are important for a university student (for example: professors, "
        "university notices, exams, internships, interviews, job offers, security alerts such as a new sign-in or a "
        "password change, people writing personally). Promotions, newsletters, social notifications and routine "
        "bank transaction alerts (debited/credited/payment receipts) are not important."
        + (f" The user also considers these important: {hint}." if hint else "")
        + " Return only JSON: a list with one object per email, in order: "
        '[{"n": 1, "important": true, "summary": "Prof. Rao: lab exam moved to Friday 10 AM"}]. '
        "Keep each summary under 15 words.\n\n" + _describe(messages)
    )
    content, ok = llm.complete([{"role": "user", "content": instruction}], "You sort emails. Reply with JSON only.",
                               max_tokens=600, json_mode=True)
    results = []
    if ok:
        match = re.search(r"\[.*\]", content, re.DOTALL)
        try:
            data = json.loads(match.group(0)) if match else []
        except ValueError:
            data = []
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                message = messages[int(entry.get("n", 0)) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            results.append({
                "uid": message["uid"],
                "important": bool(entry.get("important")),
                "summary": str(entry.get("summary") or f"{message['from']}: {message['subject']}"),
            })
    if not results:  # model unavailable: treat everything as worth a short mention
        results = [{"uid": m["uid"], "important": True, "summary": f"{m['from']}: {m['subject']}"} for m in messages]
    return results


def show_windows_notification(title, text):
    """A Windows notification balloon using only built-in components."""
    if os.name != "nt":
        print(f"[Mail] {title}: {text}")
        return
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.BalloonTipTitle = $env:JARVIS_NOTE_TITLE; $n.BalloonTipText = $env:JARVIS_NOTE_TEXT; "
        "$n.Visible = $true; $n.ShowBalloonTip(10000); Start-Sleep -Seconds 10; $n.Dispose()"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        env={**os.environ, "JARVIS_NOTE_TITLE": title[:60], "JARVIS_NOTE_TEXT": text[:250]},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class MailWatcher:
    """Checks for new mail in the background and announces the important ones."""

    def __init__(self, announce):
        self.announce = announce  # callback(text): shows and speaks a message in Jarvis
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        minutes = float(os.getenv("JARVIS_MAIL_CHECK_MINUTES", "10") or 0)
        mode = os.getenv("JARVIS_MAIL_ALERTS", "important").strip().lower()
        if not is_configured() or minutes <= 0 or mode == "off":
            return False
        self.thread = threading.Thread(target=self._run, args=(minutes * 60, mode), daemon=True)
        self.thread.start()
        print(f"[Mail] Checking your inbox every {minutes:g} minutes ({mode} alerts).")
        return True

    def stop(self):
        self.stop_event.set()

    def check_once(self, mode="important"):
        state = _load_state()
        last = state.get("last_uid")
        if last is None:  # first run: start from now instead of announcing the whole inbox
            state["last_uid"] = latest_uid()
            _save_state(state)
            return []
        messages = fetch_messages(unread_only=True, limit=10, after_uid=last)
        if not messages:
            return []
        state["last_uid"] = max(item["uid"] for item in messages)
        _save_state(state)
        oldest_first = list(reversed(messages))
        by_uid = {item["uid"]: item for item in oldest_first}
        decisions = triage(oldest_first)
        alerts = [entry["summary"] for entry in decisions if mode == "all" or entry["important"]]
        if not alerts:
            return alerts

        from automation import presence, tools

        user = tools.user_name()
        important = [by_uid[entry["uid"]] for entry in decisions if entry["important"] and entry["uid"] in by_uid]
        heading = "Important email" if len(alerts) == 1 else f"{len(alerts)} important emails"
        show_windows_notification(heading, " | ".join(alerts))
        spoken = f"{heading}: " + ". ".join(alerts) + "."

        if presence.is_away():
            if os.getenv("JARVIS_AUTO_REPLY", "on").strip().lower() not in {"off", "0", "false", "no"}:
                replied = []
                for message in important:
                    if not can_auto_reply(message, state):
                        continue
                    try:
                        send_email(message["reply_to"], reply_subject(message["subject"]), away_message(message, user),
                                   message["message_id"], message["references"], automatic=True)
                    except MailError as exc:
                        print(f"[Mail] Auto-reply to {message['address']} failed: {exc}")
                        continue
                    state.setdefault("auto_replied", {})[message["reply_to"].lower()] = (
                        datetime.datetime.now().isoformat(timespec="seconds"))
                    replied.append(message["from"])
                _save_state(state)
                if replied:
                    spoken += " You were away, so I told " + ", ".join(replied) + " that you'll get back to them."
            self.announce(spoken)
            return alerts

        personal = next((message for message in important if not message["automated"]), None)
        if personal and os.getenv("JARVIS_DRAFT_REPLIES", "on").strip().lower() not in {"off", "0", "false", "no"}:
            try:
                body = draft_reply(personal, user)
                tools.set_pending_email_reply(personal, body)
                spoken += (f" {personal['from']} wrote: {personal['body'][:400]} I've drafted a reply: {body} "
                           "Say 'send it' to send it, or tell me what to change.")
            except MailError as exc:
                print(f"[Mail] Couldn't draft a reply: {exc}")
        self.announce(spoken)
        return alerts

    def _run(self, interval, mode):
        while not self.stop_event.is_set():
            try:
                self.check_once(mode)
            except MailError as exc:
                print(f"[Mail] {exc}")
            except Exception as exc:  # never crash Jarvis over mail
                print(f"[Mail] Check failed: {exc}")
            self.stop_event.wait(interval)
