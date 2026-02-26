#!/usr/bin/env python3
"""AI Tennis Lab -- Email Assistant.

Polls IMAP for emails from Raffael, parses commands, and takes action
on the tennis blog (write articles, rework, answer questions).
Replies with results via SMTP.

Usage:
    python3 email_assistant.py          # Run polling loop (every 30s)
    python3 email_assistant.py --test   # Check IMAP once and exit
"""
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.header import decode_header
from email.utils import make_msgid
import os
import sys
import time
import re
import argparse
import subprocess
import traceback
from pathlib import Path

# Import from generate.py (same directory)
sys.path.insert(0, str(Path(__file__).parent))
from generate import (
    load_config,
    call_claude,
    research_topic,
    generate_article,
    generate_image,
    check_quality,
    build_site,
    render_article,
    ARTIKEL_DIR,
    IMAGES_DIR,
    ROOT,
)

# ── Config ──────────────────────────────────────────────────────────
IMAP_HOST = "mail.easyname.eu"
IMAP_PORT = 993
SMTP_HOST = "mail.easyname.eu"
SMTP_PORT = 587
EMAIL_ADDR = "i-am-a-user@nichtagentur.at"
EMAIL_PASS = "i_am_an_AI_password_2026"
ALLOWED_SENDER = "r.leb@cybertime.at"
POLL_INTERVAL = 30  # seconds
AUTO_GENERATE_INTERVAL = 1800  # 30 minutes in seconds


# ── IMAP helpers ────────────────────────────────────────────────────

def imap_connect():
    """Connect to IMAP and select INBOX. Returns imap object."""
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(EMAIL_ADDR, EMAIL_PASS)
    imap.select("INBOX")
    return imap


def fetch_unseen(imap):
    """Return list of unseen message UIDs."""
    status, data = imap.search(None, "UNSEEN")
    if status != "OK" or not data[0]:
        return []
    return data[0].split()


def parse_email(imap, uid):
    """Fetch and parse a single email. Returns dict with from, subject, body, message_id."""
    status, data = imap.fetch(uid, "(RFC822)")
    if status != "OK":
        return None

    msg = email.message_from_bytes(data[0][1])

    # Decode subject
    subject_parts = decode_header(msg["Subject"] or "")
    subject = ""
    for part, charset in subject_parts:
        if isinstance(part, bytes):
            subject += part.decode(charset or "utf-8", errors="replace")
        else:
            subject += part

    # Get sender
    from_addr = msg.get("From", "")
    # Extract just the email address
    match = re.search(r'[\w.+-]+@[\w.-]+', from_addr)
    sender = match.group(0).lower() if match else from_addr.lower()

    # Get body (plain text preferred)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    return {
        "from": sender,
        "subject": subject.strip(),
        "body": body.strip(),
        "message_id": msg.get("Message-ID", ""),
    }


# ── SMTP reply ──────────────────────────────────────────────────────

def send_reply(to_addr, subject, body, in_reply_to=""):
    """Send a reply email via SMTP."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Re: {subject}"
    msg["From"] = EMAIL_ADDR
    msg["To"] = to_addr
    msg["Message-ID"] = make_msgid(domain="nichtagentur.at")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(EMAIL_ADDR, EMAIL_PASS)
        server.sendmail(EMAIL_ADDR, [to_addr], msg.as_string())
    print(f"  Reply sent to {to_addr}")


# ── Command parsing (no AI needed) ─────────────────────────────────

def parse_command(subject, body):
    """Parse email into a command. Returns (command, argument).

    Commands:
      ("new_article", "<topic search term>")
      ("rework", "<topic search term>")
      ("topic_list", None)
      ("status", None)
      ("question", "<full text>")
    """
    text = (subject + " " + body).lower()

    # Topic list
    if "themenliste" in text or "topics" in text or "themen" in text:
        return ("topic_list", None)

    # Status
    if "status" in text and len(text.split()) < 10:
        return ("status", None)

    # New article
    new_patterns = [
        r"neuer?\s+artikel\s+(?:ueber|uber|zum thema|zu)\s+(.+)",
        r"schreib\w*\s+(?:einen?\s+)?artikel\s+(?:ueber|uber|zum thema|zu)\s+(.+)",
        r"new\s+article\s+(?:about|on)\s+(.+)",
    ]
    for pattern in new_patterns:
        match = re.search(pattern, text)
        if match:
            return ("new_article", match.group(1).strip().rstrip(".!"))

    # Rework existing
    rework_patterns = [
        r"(?:ueberarbeite|ueberarbeiten|rework|verbessere|verbessern|aktualisiere)\s+(.+)",
    ]
    for pattern in rework_patterns:
        match = re.search(pattern, text)
        if match:
            return ("rework", match.group(1).strip().rstrip(".!"))

    # Default: treat as a question
    full_text = subject
    if body:
        full_text += "\n" + body
    return ("question", full_text)


# ── Topic matching ──────────────────────────────────────────────────

def find_topic(search_term, topics):
    """Find the best matching topic for a search term. Returns topic dict or None."""
    search_lower = search_term.lower()
    search_words = set(search_lower.split())

    best_match = None
    best_score = 0

    for topic in topics:
        title_lower = topic["title"].lower()
        slug_lower = topic["slug"].replace("-", " ")
        keywords_lower = " ".join(kw.lower() for kw in topic["keywords"])
        all_text = f"{title_lower} {slug_lower} {keywords_lower}"

        # Score by word overlap
        score = 0
        for word in search_words:
            if len(word) < 3:
                continue
            if word in all_text:
                score += 2
            # Partial match
            elif any(word in w or w in word for w in all_text.split()):
                score += 1

        if score > best_score:
            best_score = score
            best_match = topic

    return best_match if best_score > 0 else None


# ── Command handlers ────────────────────────────────────────────────

def handle_new_article(search_term, site, topics):
    """Generate a new article. Returns reply text."""
    topic = find_topic(search_term, topics)
    if not topic:
        return f"Kein passendes Thema gefunden fuer: '{search_term}'\n\nVerfuegbare Themen:\n" + \
               "\n".join(f"  - {t['title']}" for t in topics)

    # Check if already exists
    existing = ARTIKEL_DIR / f"{topic['slug']}.html"
    if existing.exists():
        return (
            f"Artikel existiert bereits: {topic['title']}\n"
            f"URL: {site['base_url']}/artikel/{topic['slug']}.html\n\n"
            f"Sende 'ueberarbeite {topic['title']}' um ihn zu ueberarbeiten."
        )

    print(f"  Generating new article: {topic['title']}")

    # Research
    research_notes = research_topic(topic)
    time.sleep(1)

    # Generate with quality loop
    feedback = ""
    quality_score = 0
    for attempt in range(2):
        print(f"  Writing{'  (retry)' if attempt > 0 else ''}...")
        article_data = generate_article(topic, research_notes=research_notes, feedback=feedback)
        time.sleep(1)

        passed, quality_score, feedback = check_quality(topic, article_data["content_html"])
        if passed:
            break
        time.sleep(1)

    # Image
    print("  Generating image...")
    image_file = generate_image(topic)
    time.sleep(1)

    # Render
    render_article(topic, article_data, image_file, site, topics)

    # Rebuild site
    build_site(site, topics)

    # Git commit + push
    git_push(f"Neuer Artikel: {topic['title']}")

    url = f"{site['base_url']}/artikel/{topic['slug']}.html"
    return (
        f"Neuer Artikel erstellt!\n\n"
        f"Titel: {topic['title']}\n"
        f"Qualitaet: {quality_score}/10\n"
        f"Bild: {'Ja' if image_file else 'Nein'}\n"
        f"URL: {url}\n\n"
        f"Der Artikel ist live nach dem GitHub Pages Deploy (ca. 1-2 Minuten)."
    )


def handle_rework(search_term, email_body, site, topics):
    """Rework an existing article with feedback. Returns reply text."""
    topic = find_topic(search_term, topics)
    if not topic:
        return f"Kein passendes Thema gefunden fuer: '{search_term}'"

    existing = ARTIKEL_DIR / f"{topic['slug']}.html"
    if not existing.exists():
        return (
            f"Artikel '{topic['title']}' existiert noch nicht.\n"
            f"Sende 'neuer artikel ueber {search_term}' um ihn zu erstellen."
        )

    print(f"  Reworking article: {topic['title']}")

    # Use email body as feedback for regeneration
    feedback = email_body if email_body else "Bitte ueberarbeite und verbessere den Artikel."

    # Research fresh
    research_notes = research_topic(topic)
    time.sleep(1)

    # Generate with feedback
    quality_score = 0
    for attempt in range(2):
        print(f"  Rewriting{'  (retry)' if attempt > 0 else ''}...")
        article_data = generate_article(topic, research_notes=research_notes, feedback=feedback)
        time.sleep(1)

        passed, quality_score, new_feedback = check_quality(topic, article_data["content_html"])
        if passed:
            break
        feedback = new_feedback
        time.sleep(1)

    # New image
    print("  Generating new image...")
    image_file = generate_image(topic)
    time.sleep(1)

    # Render (overwrites old file)
    render_article(topic, article_data, image_file, site, topics)
    build_site(site, topics)

    git_push(f"Ueberarbeitet: {topic['title']}")

    url = f"{site['base_url']}/artikel/{topic['slug']}.html"
    return (
        f"Artikel ueberarbeitet!\n\n"
        f"Titel: {topic['title']}\n"
        f"Qualitaet: {quality_score}/10\n"
        f"URL: {url}\n\n"
        f"Aenderungen sind live nach dem GitHub Pages Deploy (ca. 1-2 Minuten)."
    )


def handle_topic_list(topics):
    """Return formatted topic list with status."""
    existing = {p.stem for p in ARTIKEL_DIR.glob("*.html")}

    lines = ["AI Tennis Lab -- Themenliste\n"]
    current_cat = ""
    done = 0
    total = len(topics)

    for t in topics:
        if t["category"] != current_cat:
            current_cat = t["category"]
            lines.append(f"\n== {current_cat} ==")

        status = "[X]" if t["slug"] in existing else "[ ]"
        if t["slug"] in existing:
            done += 1
        lines.append(f"  {status} {t['title']}")

    lines.insert(1, f"Fortschritt: {done}/{total} Artikel fertig\n")
    return "\n".join(lines)


def handle_status(site, topics):
    """Return blog statistics."""
    existing = {p.stem for p in ARTIKEL_DIR.glob("*.html")}
    total = len(topics)
    done = len(existing)

    categories = {}
    for t in topics:
        cat = t["category"]
        if cat not in categories:
            categories[cat] = {"total": 0, "done": 0}
        categories[cat]["total"] += 1
        if t["slug"] in existing:
            categories[cat]["done"] += 1

    lines = [
        "AI Tennis Lab -- Status\n",
        f"Artikel gesamt: {done}/{total}",
        f"Fortschritt: {done*100//total}%\n",
        "Kategorien:",
    ]
    for cat, counts in categories.items():
        lines.append(f"  {cat}: {counts['done']}/{counts['total']}")

    lines.append(f"\nURL: {site['base_url']}")
    return "\n".join(lines)


def handle_question(question_text, site):
    """Forward question to Claude and return answer."""
    print("  Answering question via Claude...")
    system = (
        "Du bist der AI-Assistent des AI Tennis Lab Blogs. "
        "Beantworte Fragen zum Blog, Tennis-Technik oder zur Verwaltung des Blogs. "
        f"Blog-URL: {site['base_url']}"
    )
    answer = call_claude(question_text, system)
    return f"Antwort auf deine Frage:\n\n{answer}"


# ── Git helper ──────────────────────────────────────────────────────

def git_push(commit_msg):
    """Git add, commit, pull --rebase, push."""
    try:
        cwd = str(ROOT)
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=cwd, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=cwd, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=cwd, check=True, capture_output=True
        )
        print(f"  Git push OK: {commit_msg}")
    except subprocess.CalledProcessError as e:
        print(f"  Git error: {e.stderr.decode() if e.stderr else e}")


# ── Main loop ───────────────────────────────────────────────────────

def process_email(mail_data, site, topics):
    """Process a single parsed email. Returns reply text."""
    command, argument = parse_command(mail_data["subject"], mail_data["body"])
    print(f"  Command: {command}, arg: {argument}")

    if command == "topic_list":
        return handle_topic_list(topics)
    elif command == "status":
        return handle_status(site, topics)
    elif command == "new_article":
        return handle_new_article(argument, site, topics)
    elif command == "rework":
        return handle_rework(argument, mail_data["body"], site, topics)
    elif command == "question":
        return handle_question(argument, site)
    else:
        return "Unbekannter Befehl. Versuche: neuer artikel, ueberarbeite, themenliste, status"


def poll_once():
    """Check IMAP once, process any unseen emails from allowed sender."""
    site, topics = load_config()

    try:
        imap = imap_connect()
    except Exception as e:
        print(f"IMAP connection failed: {e}")
        return

    try:
        unseen = fetch_unseen(imap)
        if not unseen:
            return

        print(f"Found {len(unseen)} unseen email(s)")

        for uid in unseen:
            mail_data = parse_email(imap, uid)
            if not mail_data:
                continue

            print(f"\nFrom: {mail_data['from']}")
            print(f"Subject: {mail_data['subject']}")

            # Only process emails from Raffael
            if mail_data["from"] != ALLOWED_SENDER:
                print(f"  Ignoring (not from {ALLOWED_SENDER})")
                continue

            try:
                reply_text = process_email(mail_data, site, topics)
                send_reply(
                    mail_data["from"],
                    mail_data["subject"],
                    reply_text,
                    in_reply_to=mail_data["message_id"],
                )
            except Exception as e:
                error_msg = f"Fehler bei der Verarbeitung:\n\n{traceback.format_exc()}"
                print(f"  ERROR: {e}")
                try:
                    send_reply(
                        mail_data["from"],
                        mail_data["subject"],
                        error_msg,
                        in_reply_to=mail_data["message_id"],
                    )
                except Exception:
                    pass

    finally:
        try:
            imap.logout()
        except Exception:
            pass


def auto_generate_next():
    """Automatically generate the next unwritten article from the topic list."""
    site, topics = load_config()
    existing = {p.stem for p in ARTIKEL_DIR.glob("*.html")}
    remaining = [t for t in topics if t["slug"] not in existing]

    if not remaining:
        print("\n[AUTO] Alle Artikel bereits generiert. Nichts zu tun.")
        return

    topic = remaining[0]
    print(f"\n[AUTO] Generiere naechsten Artikel: {topic['title']}")

    try:
        # Research
        research_notes = research_topic(topic)
        time.sleep(1)

        # Generate with quality loop
        feedback = ""
        quality_score = 0
        for attempt in range(2):
            print(f"  Writing{'  (retry)' if attempt > 0 else ''}...")
            article_data = generate_article(topic, research_notes=research_notes, feedback=feedback)
            time.sleep(1)

            passed, quality_score, feedback = check_quality(topic, article_data["content_html"])
            if passed:
                break
            time.sleep(1)

        # Image
        print("  Generating image...")
        image_file = generate_image(topic)
        time.sleep(1)

        # Render + rebuild
        render_article(topic, article_data, image_file, site, topics)
        build_site(site, topics)

        # Git
        git_push(f"Neuer Artikel (auto): {topic['title']}")

        remaining_count = len(remaining) - 1
        print(f"[AUTO] Fertig: {topic['title']} (Qualitaet: {quality_score}/10, noch {remaining_count} offen)")

        # Notify Raffael
        url = f"{site['base_url']}/artikel/{topic['slug']}.html"
        send_reply(
            ALLOWED_SENDER,
            f"Neuer Artikel: {topic['title']}",
            f"Automatisch generiert!\n\n"
            f"Titel: {topic['title']}\n"
            f"Qualitaet: {quality_score}/10\n"
            f"Bild: {'Ja' if image_file else 'Nein'}\n"
            f"URL: {url}\n"
            f"Noch {remaining_count} Artikel offen.\n\n"
            f"Naechster Artikel in 30 Minuten.",
        )

    except Exception as e:
        print(f"[AUTO] FEHLER: {e}")
        traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description="AI Tennis Lab Email Assistant")
    parser.add_argument("--test", action="store_true", help="Check IMAP once and exit")
    args = parser.parse_args()

    # Ensure output dirs exist
    ARTIKEL_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    if args.test:
        print("=== Email Assistant Test Mode ===")
        print(f"IMAP: {IMAP_HOST}:{IMAP_PORT}")
        print(f"Account: {EMAIL_ADDR}")
        print(f"Allowed sender: {ALLOWED_SENDER}")
        print()

        try:
            imap = imap_connect()
            print("IMAP login: OK")

            unseen = fetch_unseen(imap)
            print(f"Unseen emails: {len(unseen)}")

            # Show unseen from allowed sender
            for uid in unseen:
                mail_data = parse_email(imap, uid)
                if mail_data and mail_data["from"] == ALLOWED_SENDER:
                    cmd, arg = parse_command(mail_data["subject"], mail_data["body"])
                    print(f"  -> '{mail_data['subject']}' => command={cmd}, arg={arg}")

            imap.logout()
            print("\nSMTP test...")
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(EMAIL_ADDR, EMAIL_PASS)
            print("SMTP login: OK")

            print("\nAll tests passed! Ready to run.")
        except Exception as e:
            print(f"TEST FAILED: {e}")
            sys.exit(1)
        return

    # Polling loop
    print("=== AI Tennis Lab Email Assistant ===")
    print(f"Polling {EMAIL_ADDR} every {POLL_INTERVAL}s")
    print(f"Auto-generating next article every {AUTO_GENERATE_INTERVAL // 60} min")
    print(f"Only processing emails from {ALLOWED_SENDER}")
    print("Press Ctrl+C to stop\n")

    # Generate first article immediately on start
    last_auto_generate = time.time() - AUTO_GENERATE_INTERVAL

    while True:
        try:
            poll_once()
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error in poll cycle: {e}")

        # Auto-generate next article every 30 minutes
        if time.time() - last_auto_generate >= AUTO_GENERATE_INTERVAL:
            last_auto_generate = time.time()
            try:
                auto_generate_next()
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:
                print(f"[AUTO] Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
