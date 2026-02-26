#!/usr/bin/env python3
"""AI Tennis Lab -- Automated German tennis technique blog generator."""
import json
import os
import sys
import argparse
import datetime
import time
import re
import smtplib
from email.mime.text import MIMEText
import requests
import base64
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

# Paths
ROOT = Path(__file__).parent
TOPICS_FILE = ROOT / "topics.json"
TEMPLATES_DIR = ROOT / "templates"
DOCS_DIR = ROOT / "docs"
ARTIKEL_DIR = DOCS_DIR / "artikel"
IMAGES_DIR = DOCS_DIR / "images"


def load_config():
    """Load topics.json and return site config + topics."""
    with open(TOPICS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["site"], data["topics"]


def call_claude(prompt, system=""):
    """Call Claude Haiku API for text generation."""
    api_key = os.environ.get("CLAUDE_API_KEY_1")
    if not api_key:
        print("FEHLER: CLAUDE_API_KEY_1 nicht gesetzt")
        sys.exit(1)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def research_topic(topic):
    """Gather verified facts about a topic before writing."""
    prompt = f"""Recherchiere zum Tennisthema: "{topic['title']}"
Kategorie: {topic['category']}, Niveau: {topic['difficulty']}

Liefere kurze, faktenbasierte Notizen zu:
1. Biomechanische Grundlagen (kinematische Kette, Gelenkwinkel)
2. DTB/ITF-Methodik und offizielle Empfehlungen
3. Haeufige Mythen oder Missverstaendnisse
4. 5 zentrale Fakten die im Artikel vorkommen muessen
5. Typische Fehlerbilder und deren Korrektur

Antworte kompakt in Stichpunkten, max 300 Woerter."""

    print("  Recherche...")
    return call_claude(prompt, "Du bist ein Tennisexperte und Sportwissenschaftler.")


def generate_image(topic):
    """Generate a hero image using Gemini image generation."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(f"  WARNUNG: GEMINI_API_KEY nicht gesetzt, ueberspringe Bild fuer {topic['slug']}")
        return None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        image_prompt = (
            f"Professional tennis photography: {topic['title']}. "
            f"Dynamic action shot on a modern tennis court, dramatic lighting, "
            f"sports magazine quality, 16:9 aspect ratio, photorealistic. "
            f"Category: {topic['category']}."
        )

        response = client.models.generate_images(
            model="imagen-4.0-fast-generate-001",
            prompt=image_prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="16:9",
            ),
        )

        if response.generated_images:
            img_data = response.generated_images[0].image.image_bytes
            img_path = IMAGES_DIR / f"{topic['slug']}.jpg"
            with open(img_path, "wb") as f:
                f.write(img_data)
            print(f"  Bild generiert: {img_path.name}")
            return f"{topic['slug']}.jpg"

        print(f"  WARNUNG: Kein Bild in Antwort fuer {topic['slug']}")
        return None

    except Exception as e:
        print(f"  WARNUNG: Bildgenerierung fehlgeschlagen fuer {topic['slug']}: {e}")
        return None


def generate_article(topic, research_notes="", feedback=""):
    """Generate a full article for a topic using Claude."""
    system = (
        "Du bist ein erfahrener Tennisexperte und Sportanalyst. "
        "Schreibe fuer Tennistrainer. Nutze korrekte biomechanische Fachbegriffe. "
        "Strukturiere mit H2/H3 Ueberschriften. "
        "Referenziere DTB- und ITF-Methodik wo passend."
    )

    research_block = ""
    if research_notes:
        research_block = f"\n\nRecherche-Ergebnisse (nutze diese Fakten im Artikel):\n{research_notes}\n"

    feedback_block = ""
    if feedback:
        feedback_block = f"\n\nBitte beruecksichtige folgendes Feedback zur Verbesserung:\n{feedback}\n"

    prompt = f"""Schreibe einen ausfuehrlichen Artikel zum Thema: "{topic['title']}"

Kategorie: {topic['category']}
Niveau: {topic['difficulty']}
Keywords: {', '.join(topic['keywords'])}
{research_block}{feedback_block}

Anforderungen:
- 1200-1500 Woerter
- Verwende HTML-Tags: <h2>, <h3>, <p>, <ul>, <li>, <ol>, <strong>, <em>
- Beginne NICHT mit <h1> (wird vom Template gesetzt)
- Struktur: Einfuehrung, Technikbeschreibung (biomechanisch), Fehlerbilder, Uebungen (mit Wiederholungen), Sicherheitstipps
- Verwende biomechanische Fachbegriffe (kinematische Kette, Pronation, etc.)
- Fuer Uebungen: konkrete Wiederholungszahlen und Progressionen angeben

Antworte NUR mit dem HTML-Inhalt, kein Markdown, keine Erklaerungen.

Gib am Ende in einer separaten Zeile folgendes JSON zurueck (nach dem HTML):
|||META|||
{{"meta_description": "kurze Beschreibung unter 155 Zeichen", "howto_steps": [{{"name": "Schritt 1 Titel", "text": "Beschreibung"}}, {{"name": "Schritt 2 Titel", "text": "Beschreibung"}}, {{"name": "Schritt 3 Titel", "text": "Beschreibung"}}]}}"""

    raw = call_claude(prompt, system)

    # Split content and meta
    if "|||META|||" in raw:
        content_html, meta_raw = raw.split("|||META|||", 1)
        try:
            meta = json.loads(meta_raw.strip())
        except json.JSONDecodeError:
            meta = {"meta_description": topic["title"], "howto_steps": []}
    else:
        content_html = raw
        meta = {"meta_description": topic["title"], "howto_steps": []}

    # Strip markdown code fences if present
    content_html = content_html.strip()
    if content_html.startswith("```html"):
        content_html = content_html[7:]
    if content_html.startswith("```"):
        content_html = content_html[3:]
    if content_html.endswith("```"):
        content_html = content_html[:-3]
    content_html = content_html.strip()

    return {
        "content_html": content_html,
        "meta_description": meta.get("meta_description", topic["title"]),
        "howto_steps": meta.get("howto_steps", []),
    }


def get_related_articles(topic, all_topics, existing_slugs):
    """Find up to 3 related articles (same category, already generated)."""
    related = []
    for t in all_topics:
        if t["slug"] != topic["slug"] and t["slug"] in existing_slugs:
            if t["category"] == topic["category"]:
                related.append({"slug": t["slug"], "title": t["title"]})
            if len(related) >= 3:
                break
    # Fill with other categories if needed
    if len(related) < 3:
        for t in all_topics:
            if t["slug"] != topic["slug"] and t["slug"] in existing_slugs:
                if t["category"] != topic["category"]:
                    related.append({"slug": t["slug"], "title": t["title"]})
                if len(related) >= 3:
                    break
    return related


def pick_next_topics(topics, count=1):
    """Return next ungenerated topics."""
    existing = {p.stem for p in ARTIKEL_DIR.glob("*.html")}
    remaining = [t for t in topics if t["slug"] not in existing]
    return remaining[:count]


def build_site(site, topics):
    """Rebuild index, about, sitemap, robots using templates."""
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    year = datetime.datetime.now().year
    base_ctx = {"site": site, "year": year}

    # Collect generated articles for index
    existing_slugs = {p.stem for p in ARTIKEL_DIR.glob("*.html")}
    generated = []
    for t in topics:
        if t["slug"] in existing_slugs:
            img_file = f"{t['slug']}.jpg"
            has_image = (IMAGES_DIR / img_file).exists()
            generated.append({
                **t,
                "image": img_file if has_image else None,
                "date": _get_file_date(ARTIKEL_DIR / f"{t['slug']}.html"),
            })

    # Group by category (ordered)
    from collections import OrderedDict
    categories = OrderedDict()
    for a in generated:
        cat = a["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(a)

    # Index
    tpl = env.get_template("index.html")
    html = tpl.render(**base_ctx, categories=categories, total_articles=len(generated), total_categories=len(categories))
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    print("  index.html aktualisiert")

    # About
    tpl = env.get_template("about.html")
    html = tpl.render(**base_ctx)
    (DOCS_DIR / "ueber-uns.html").write_text(html, encoding="utf-8")
    print("  ueber-uns.html aktualisiert")

    # Sitemap
    urls = [
        {"loc": site["base_url"] + "/", "lastmod": datetime.date.today().isoformat()},
        {"loc": site["base_url"] + "/ueber-uns.html", "lastmod": datetime.date.today().isoformat()},
    ]
    for a in generated:
        urls.append({
            "loc": f"{site['base_url']}/artikel/{a['slug']}.html",
            "lastmod": a["date"],
        })

    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n'
    sitemap += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for u in urls:
        sitemap += f'  <url>\n    <loc>{u["loc"]}</loc>\n    <lastmod>{u["lastmod"]}</lastmod>\n  </url>\n'
    sitemap += '</urlset>\n'
    (DOCS_DIR / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    print("  sitemap.xml aktualisiert")

    # Robots.txt
    robots = f"User-agent: *\nAllow: /\n\nSitemap: {site['base_url']}/sitemap.xml\n"
    (DOCS_DIR / "robots.txt").write_text(robots, encoding="utf-8")
    print("  robots.txt aktualisiert")


def _get_file_date(path):
    """Get modification date of a file as ISO string."""
    if path.exists():
        ts = path.stat().st_mtime
        return datetime.date.fromtimestamp(ts).isoformat()
    return datetime.date.today().isoformat()


def render_article(topic, article_data, image_file, site, topics):
    """Render article HTML and save to docs/artikel/."""
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    existing_slugs = {p.stem for p in ARTIKEL_DIR.glob("*.html")}
    existing_slugs.add(topic["slug"])
    related = get_related_articles(topic, topics, existing_slugs)

    article = {
        "slug": topic["slug"],
        "title": topic["title"],
        "category": topic["category"],
        "difficulty": topic["difficulty"],
        "keywords": topic["keywords"],
        "date": datetime.date.today().strftime("%d.%m.%Y"),
        "date_iso": datetime.date.today().isoformat(),
        "content_html": article_data["content_html"],
        "meta_description": article_data["meta_description"],
        "howto_steps": article_data["howto_steps"],
        "image": image_file,
        "related": related,
    }

    tpl = env.get_template("article.html")
    html = tpl.render(site=site, article=article, year=datetime.datetime.now().year)
    out_path = ARTIKEL_DIR / f"{topic['slug']}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"  Gespeichert: {out_path.name}")
    return out_path


def check_quality(topic, content_html):
    """Score article quality 1-10 using an independent Claude call."""
    prompt = f"""Bewerte folgenden Tennis-Fachartikel zum Thema "{topic['title']}".

Bewerte auf einer Skala von 1-10 in diesen Kategorien:
- Fachliche Korrektheit (biomechanische Begriffe, DTB/ITF-konform)
- Lesbarkeit (Struktur, Verstaendlichkeit fuer Trainer)
- Vollstaendigkeit (Technik, Fehlerbilder, Uebungen, Sicherheit)
- E-E-A-T (Expertise, Experience, Authority, Trust)

Artikel-HTML:
{content_html[:3000]}

Antworte NUR in diesem Format:
KORREKTHEIT: <Zahl>
LESBARKEIT: <Zahl>
VOLLSTAENDIGKEIT: <Zahl>
EEAT: <Zahl>
GESAMT: <Zahl>
FEEDBACK: <1-2 Saetze konkretes Verbesserungsfeedback>"""

    print("  Qualitaetspruefung...")
    raw = call_claude(prompt, "Du bist ein strenger Redakteur fuer Sportfachmedien.")

    # Parse score
    score = 7  # default
    feedback = ""
    for line in raw.strip().split("\n"):
        if line.startswith("GESAMT:"):
            try:
                score = int(line.split(":")[1].strip().split("/")[0].strip())
            except (ValueError, IndexError):
                pass
        if line.startswith("FEEDBACK:"):
            feedback = line.split(":", 1)[1].strip()

    passed = score >= 7
    print(f"  Qualitaet: {score}/10 {'OK' if passed else 'MANGELHAFT'}")
    return passed, score, feedback


def check_urls(html_path, base_url):
    """Check all URLs in a rendered HTML file for broken links."""
    content = html_path.read_text(encoding="utf-8")
    urls = re.findall(r'(?:href|src)=["\']([^"\']+)["\']', content)

    broken = []
    for url in urls:
        # Skip anchors and javascript
        if url.startswith("#") or url.startswith("javascript:") or url.startswith("data:"):
            continue

        # Internal URL
        if not url.startswith("http"):
            # Resolve relative to docs/
            if url.startswith("/"):
                local_path = DOCS_DIR / url.lstrip("/")
            else:
                local_path = html_path.parent / url
            if not local_path.exists():
                broken.append(f"LOKAL: {url}")
        else:
            # External URL - HEAD request
            try:
                r = requests.head(url, timeout=5, allow_redirects=True)
                if r.status_code >= 400:
                    broken.append(f"HTTP {r.status_code}: {url}")
            except requests.RequestException as e:
                broken.append(f"FEHLER: {url} ({e})")

    if broken:
        print(f"  {len(broken)} fehlerhafte URLs gefunden:")
        for b in broken:
            print(f"    - {b}")
    else:
        print("  Alle URLs OK")

    return broken


def send_email(subject, body):
    """Send email notification via SMTP STARTTLS."""
    smtp_host = "mail.easyname.eu"
    smtp_port = 587
    from_addr = "i-am-a-user@nichtagentur.at"
    to_addr = "r.leb@cybertime.at"
    password = os.environ.get("EMAIL_PASSWORD", "i_am_an_AI_password_2026")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        print(f"  E-Mail gesendet an {to_addr}")
    except Exception as e:
        print(f"  WARNUNG: E-Mail-Versand fehlgeschlagen: {e}")


def main():
    parser = argparse.ArgumentParser(description="AI Tennis Lab Generator")
    parser.add_argument("--count", type=int, default=1, help="Anzahl neuer Artikel (Standard: 1)")
    parser.add_argument("--all", action="store_true", help="Alle fehlenden Artikel generieren")
    args = parser.parse_args()

    # Ensure output dirs exist
    ARTIKEL_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    site, topics = load_config()

    # Determine how many to generate
    if args.all:
        to_generate = pick_next_topics(topics, count=len(topics))
    else:
        to_generate = pick_next_topics(topics, count=args.count)

    if not to_generate:
        print("Alle Artikel bereits generiert. Nichts zu tun.")
        build_site(site, topics)
        return

    print(f"\n=== AI Tennis Lab Generator ===")
    print(f"Generiere {len(to_generate)} Artikel...\n")

    results = []  # Collect results for email summary

    for i, topic in enumerate(to_generate, 1):
        print(f"[{i}/{len(to_generate)}] {topic['title']}")

        # Step 1: Research
        research_notes = research_topic(topic)
        time.sleep(1)

        # Step 2: Generate article with research context + quality loop
        feedback = ""
        quality_score = 0
        for attempt in range(3):  # max 3 attempts (1 initial + 2 retries)
            print(f"  Generiere Text{' (Versuch ' + str(attempt + 1) + ')' if attempt > 0 else ''}...")
            article_data = generate_article(topic, research_notes=research_notes, feedback=feedback)
            time.sleep(1)

            # Quality check
            passed, quality_score, feedback = check_quality(topic, article_data["content_html"])
            if passed:
                break
            if attempt < 2:
                print(f"  Regeneriere mit Feedback...")
            time.sleep(1)

        # Step 3: Generate image
        print("  Generiere Bild...")
        image_file = generate_image(topic)
        time.sleep(1)

        # Render article page
        out_path = render_article(topic, article_data, image_file, site, topics)

        # Step 4: Check URLs
        broken_urls = check_urls(out_path, site["base_url"])

        # Collect result
        results.append({
            "title": topic["title"],
            "slug": topic["slug"],
            "score": quality_score,
            "broken_urls": broken_urls,
        })

    # Rebuild site (index, sitemap, etc.)
    print("\nAktualisiere Seite...")
    build_site(site, topics)

    # Step 5: Send email summary
    if results:
        email_lines = ["AI Tennis Lab -- Pipeline-Bericht\n"]
        for r in results:
            url = f"{site['base_url']}/artikel/{r['slug']}.html"
            email_lines.append(f"Artikel: {r['title']}")
            email_lines.append(f"  Qualitaet: {r['score']}/10")
            email_lines.append(f"  URL: {url}")
            if r["broken_urls"]:
                email_lines.append(f"  Fehlerhafte Links: {len(r['broken_urls'])}")
                for b in r["broken_urls"]:
                    email_lines.append(f"    - {b}")
            email_lines.append("")

        send_email(
            f"Tennis Blog: {len(results)} neue Artikel generiert",
            "\n".join(email_lines),
        )

    print(f"\nFertig! {len(to_generate)} Artikel generiert.\n")


if __name__ == "__main__":
    main()
