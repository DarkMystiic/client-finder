"""Client Finder email outreach: find public emails for exported leads, then mail-merge them from Gmail.

  python tools/outreach.py find  leads.csv            -> leads_emails.csv (adds email + where it was found)
  python tools/outreach.py send  leads_emails.csv     -> outbox.txt only (dry run, nothing sent)
  python tools/outreach.py send  leads_emails.csv --send [--limit 20]

Your details, Gmail app password and the sent / opt-out lists live in ~/.clientfinder (never in this public repo).
Email finding is adapted from Desktop/OutreachLab/app/research.py (robots.txt respected, polite per-host delays).
"""
import argparse
import csv
import json
import random
import re
import smtplib
import sys
import time
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import lxml.html
import requests

HOME = Path.home() / ".clientfinder"
CONFIG = HOME / "config.json"
SENT = HOME / "sent.json"
OPTOUT = HOME / "optout.txt"
DEFAULT_CONFIG = {
    "name": "Jaylen Gotico",
    "from_email": "jaylendgotico@gmail.com",
    "app_password": "",
    "postal_address": "",
    "price": "$150",
    "portfolio": "https://darkmystiic.github.io/portfolio/",
    "daily_limit": 20,
}

UA = "ClientFinder/1.0 (finding public business contact emails; respects robots.txt)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|example\.|sentry|wixpress|domain\.com|yourname|"
                       r"@email\.|godaddy|squarespace|shopify\.com|yelp\.|facebook|instagram|google\.|"
                       r"noreply|no-reply|privacy@|abuse@|webmaster@", re.I)
SOCIAL = re.compile(r"facebook\.com|fb\.com|instagram\.com|linktr\.ee|yelp\.com|tiktok\.com|booksy\.com|"
                    r"vagaro\.com|styleseat\.com|google\.com/maps|maps\.app", re.I)
STOP = {"the", "and", "of", "inc", "llc", "co", "shop", "salon", "barber", "barbershop", "auto", "repair",
        "automotive", "body", "hair", "cuts", "gym", "studio", "center", "service", "services"}

session = requests.Session()
session.headers["User-Agent"] = UA
_last_hit = {}


def polite_get(url, gap=1.5, **kw):
    host = urlparse(url).netloc
    wait = _last_hit.get(host, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()
    kw.setdefault("timeout", 15)
    return session.get(url, **kw)


def domain_of(url):
    host = urlparse(url if "://" in url else "http://" + url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def words(s):
    return [w for w in re.sub(r"[^a-z0-9& ]", " ", s.lower()).split() if w]


def same_business(ours, text):
    """Same rule as the web app's lookup: at least half the name's distinctive words appear in the text."""
    t, all_w = set(words(text)), words(ours)
    need = [w for w in all_w if w not in STOP] or all_w
    return sum(w in t for w in need) >= (len(need) + 1) // 2


def clean_email(e):
    e = e.strip().strip(".").lower()
    return e if EMAIL_RE.fullmatch(e) and not BAD_EMAIL.search(e) else ""


# ---------- find ----------
def robots_ok(url):
    p = urlparse(url)
    rp = robotparser.RobotFileParser()
    try:
        r = polite_get(f"{p.scheme}://{p.netloc}/robots.txt", gap=1.0)
        if r.status_code in (401, 403):
            return False
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        return True  # no robots.txt reachable = no restriction published
    return rp.can_fetch(UA, url)


def fetch(url):
    r = polite_get(url, stream=True)
    if r.status_code >= 400 or "html" not in r.headers.get("Content-Type", "html"):
        r.close()
        return None, r.url
    content = r.raw.read(1_500_000, decode_content=True)
    r.close()
    return lxml.html.fromstring(content, base_url=r.url), r.url


def emails_from_site(url):
    """Homepage + up to 2 same-site contact/about pages. Returns [(email, page)], business-domain emails first."""
    url = url if "://" in url else "https://" + url
    if not robots_ok(url):
        return []
    try:
        doc, final = fetch(url)
    except Exception:
        return []
    if doc is None:
        return []
    site, pages = domain_of(final), [(final, doc)]
    extra = []
    for a in doc.xpath("//a[@href]"):
        href = urljoin(final, a.get("href")).split("#")[0]
        if domain_of(href) == site and re.search(r"contact|about", (a.text_content() + " " + href).lower()) \
                and href not in extra and href != final:
            extra.append(href)
    for href in extra[:2]:
        try:
            if robots_ok(href):
                d, u = fetch(href)
                if d is not None:
                    pages.append((u, d))
        except Exception:
            pass
    found = []
    for page, d in pages:
        hrefs = d.xpath("//a[starts-with(translate(@href,'MAILTO','mailto'),'mailto:')]/@href")
        for bad in d.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        for e in [h[7:].split("?")[0] for h in hrefs] + EMAIL_RE.findall(d.text_content()):
            e = clean_email(e)
            if e and e not in [f[0] for f in found]:
                found.append((e, page))
    return sorted(found, key=lambda f: not f[0].endswith("@" + site))


def emails_from_search(name, city):
    """Web search fallback for businesses with no site: emails in result snippets that also name the business."""
    try:
        from ddgs import DDGS
        hits = DDGS(timeout=12).text(f'"{name}" {city} email', max_results=8, backend="duckduckgo")
    except Exception:
        return []
    found = []
    for h in hits or []:
        text = f"{h.get('title', '')} {h.get('body', '')}"
        if not same_business(name, text):
            continue
        for e in EMAIL_RE.findall(text):
            e = clean_email(e)
            if e and e not in [f[0] for f in found]:
                found.append((e, h.get("href", "web search")))
    return found


def cmd_find(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    out = Path(path).with_name(Path(path).stem + "_emails.csv")
    cols = list(rows[0].keys()) + [c for c in ("email_source",) if c not in rows[0]]
    got = 0
    for i, r in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {r['name']}", end=" ", flush=True)
        r.setdefault("email_source", "")
        if r.get("email"):
            r["email_source"] = r["email_source"] or "Client Finder (OpenStreetMap)"
        else:
            site = (r.get("site") or "").strip()
            found = emails_from_site(site) if site and not SOCIAL.search(site) else []
            if not found:
                found = emails_from_search(r["name"], r.get("city", ""))
                time.sleep(2)  # be gentle with the search engine
            if found:
                r["email"], r["email_source"] = found[0]
        got += bool(r["email"])
        print("->", r["email"] or "-", flush=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\n{got} of {len(rows)} leads have an email. Saved {out}")
    return out


# ---------- send ----------
def load_config():
    HOME.mkdir(exist_ok=True)
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return {**DEFAULT_CONFIG, **json.loads(CONFIG.read_text(encoding="utf-8"))}


def compose(r, cfg):
    name, link = r["name"], r.get("preview", "")
    issue = (r.get("issues") or "").split(";")[0].strip()
    if r.get("site"):
        opener = f"I came across {name} and noticed a few things on your website ({issue.lower() or 'mainly on phones'}) " \
                 "that can cost you customers who find you on their phone.\n\nSo I put together a free preview of a clean, " \
                 "fast, phone-friendly version:"
    else:
        opener = f"I noticed {name} doesn't have a website yet, so I put together a free preview of what yours could look like:"
    body = (f"Hi {name} team,\n\nI'm {cfg['name'].split()[0]}, a local web designer. {opener}\n\n{link}\n\n"
            f"If you like it, I can finish it with your photos, details and booking/contact buttons and get it live for "
            f"{cfg['price']} (one-time, no monthly fees from me). More of my work: {cfg['portfolio']}\n\n"
            f"Happy to answer any questions.\n\n{cfg['name']}\n{cfg['from_email']}\n\n"
            f"--\n{cfg['postal_address']}\nNot interested? Just reply \"no thanks\" and I won't email you again.")
    return f"I made {name} a free website preview", body


def cmd_send(path, really, limit):
    cfg = load_config()
    if not cfg["postal_address"].strip():
        sys.exit(f"Add your postal address to {CONFIG} first (US law requires one in every cold email).")
    sent = json.loads(SENT.read_text(encoding="utf-8")) if SENT.exists() else {}
    optout = {l.strip().lower() for l in OPTOUT.read_text(encoding="utf-8").splitlines()} if OPTOUT.exists() else set()
    today = str(date.today())
    budget = min(limit or cfg["daily_limit"], cfg["daily_limit"]) - sum(v["date"] == today for v in sent.values())

    queue, skipped = [], {}
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        e = (r.get("email") or "").strip().lower()
        why = ("no email" if not e else "opted out" if e in optout or domain_of(e.split("@")[-1]) in optout
               else "already emailed" if e in sent else "not new in Client Finder" if r.get("status", "new") != "new"
               else "website looks fine" if r.get("site") and not r.get("issues") else "")
        if why:
            skipped[why] = skipped.get(why, 0) + 1
        elif e not in [q[0] for q in queue]:
            queue.append((e, r))
    queue = queue[:max(budget, 0)]

    outbox = Path(path).with_name("outbox.txt")
    with open(outbox, "w", encoding="utf-8") as f:
        for e, r in queue:
            subj, body = compose(r, cfg)
            f.write(f"TO: {e}   (found: {r.get('email_source', '')})\nSUBJECT: {subj}\n\n{body}\n\n{'=' * 70}\n\n")
    print(f"{len(queue)} emails ready (today's limit {cfg['daily_limit']}). Skipped: {skipped or 'none'}")
    print(f"Read every one in {outbox}")
    if not really:
        print("Dry run: nothing sent. Run again with --send to send them.")
        return
    if not cfg["app_password"].strip():
        sys.exit(f"Add a Gmail app password to {CONFIG} first (myaccount.google.com/apppasswords).")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(cfg["from_email"], cfg["app_password"].replace(" ", ""))
        for i, (e, r) in enumerate(queue):
            subj, body = compose(r, cfg)
            m = EmailMessage()
            m["From"] = formataddr((cfg["name"], cfg["from_email"]))
            m["To"], m["Subject"], m["Message-ID"] = e, subj, make_msgid()
            m.set_content(body)
            smtp.send_message(m)
            sent[e] = {"name": r["name"], "date": today}
            SENT.write_text(json.dumps(sent, indent=2), encoding="utf-8")
            print(f"Sent {i + 1}/{len(queue)}: {r['name']} <{e}>", flush=True)
            if i + 1 < len(queue):
                time.sleep(random.uniform(45, 90))  # spaced out like a person, keeps Gmail from flagging the account
    print(f"Done. Mark these as Contacted in Client Finder. Opt-outs go in {OPTOUT}, one email or domain per line.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["find", "send"])
    ap.add_argument("csv")
    ap.add_argument("--send", action="store_true", help="actually send (default is a dry run)")
    ap.add_argument("--limit", type=int, default=0, help="send at most this many now")
    a = ap.parse_args()
    cmd_find(a.csv) if a.command == "find" else cmd_send(a.csv, a.send, a.limit)
