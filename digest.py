"""
Daily Email Digest
Fetches emails matching Tools or To Read criteria, summarizes via Claude API,
sends digest to Elizabeth, then files each email into the correct Gmail label.
"""

import os
import base64
import json
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── Configuration ────────────────────────────────────────────────────────────

RECIPIENT_EMAIL = "elizabeth@wit-whittle.com"
SENDER_NAME     = "Daily Digest"
LOOKBACK_DAYS   = 3
MAX_EMAILS      = 50
STATE_FILE      = "last_run.txt"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

# ── Split inbox definitions ───────────────────────────────────────────────────

TOOLS_SENDERS = [
    "contact@mail.replit.com",
    "newsletter@mail.bubble.io",
    "make.com",
    "send.relay.app",
    "support@mail.anthropic.com",
    "sam@twimlai.com",
    "hello@cognitoforms.com",
    "teamzoom@e.zoom.us",
    "changelog@neon.tech",
    "support@rapidapi.com",
    "mail@sendfoxmail.com",
    "chartgen@chartgen.ai",
    "otterly.ai",
    "octomind.dev",
    "xano.com",
    "marie@tally.so",
    "hello@gamma.app",
    "ivan@mail.notion.so",
    "maestroai@substack.com",
    "no-reply@github.com",
    "hello@news.railway.app",
    "noreply@lovable.dev",
    "team@e.mylens.ai",
    "no-reply@email.claude.com",
    "hello@apify.com",
]

TO_READ_SENDERS = [
    "newsletter@createwith.com",
    "tldrnewsletter.com",
    "digest.producthunt.com",
    "lenny+how-i-ai@substack.com",
    "lenny@substack.com",
    "earnestsweat+field-notes@substack.com",
    "earnestsweat@substack.com",
]

TO_READ_SUBJECTS = ["agentic"]

# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_label_id(service, label_name):
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    raise ValueError(f"Label '{label_name}' not found in Gmail.")


def build_gmail_query(after_date_str):
    tools_from = " OR ".join(f"from:{s}" for s in TOOLS_SENDERS)
    to_read_from = " OR ".join(f"from:{s}" for s in TO_READ_SENDERS)
    to_read_subjects = " OR ".join(f"subject:{s}" for s in TO_READ_SUBJECTS)
    combined = f"({tools_from}) OR ({to_read_from}) OR ({to_read_subjects})"
    return f"after:{after_date_str} -in:sent -in:drafts ({combined})"


def classify_email(from_addr, subject):
    from_lower = from_addr.lower()
    for sender in TOOLS_SENDERS:
        if sender.lower() in from_lower:
            return "tools"
    subject_lower = subject.lower()
    for kw in TO_READ_SUBJECTS:
        if kw.lower() in subject_lower:
            return "to_read"
    return "to_read"


def get_message_body(msg_data):
    payload = msg_data.get("payload", {})

    def extract_parts(parts):
        text, html = "", ""
        for part in parts:
            mime = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data", "")
            sub_parts = part.get("parts", [])
            if sub_parts:
                t, h = extract_parts(sub_parts)
                text += t
                html += h
            elif mime == "text/plain" and body_data:
                text += base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            elif mime == "text/html" and body_data:
                html += base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        return text, html

    if "parts" in payload:
        plain, html = extract_parts(payload["parts"])
        return plain or html
    else:
        body_data = payload.get("body", {}).get("data", "")
        if body_data:
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
    return ""


def fetch_emails(service, after_date_str):
    query = build_gmail_query(after_date_str)
    results = service.users().messages().list(
        userId="me", q=query, maxResults=MAX_EMAILS
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for m in messages:
        msg_data = service.users().messages().get(
            userId="me", id=m["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
        from_addr = headers.get("From", "")
        subject   = headers.get("Subject", "(no subject)")
        body      = get_message_body(msg_data)

        if len(body) > 4000:
            body = body[:4000] + "\n\n[truncated]"

        emails.append({
            "id":       m["id"],
            "subject":  subject,
            "from":     from_addr,
            "date":     headers.get("Date", ""),
            "body":     body,
            "category": classify_email(from_addr, subject),
        })

    return emails


def file_emails(service, emails, tools_label_id, newsletters_label_id):
    tools_count = 0
    newsletters_count = 0

    for email in emails:
        label_id = tools_label_id if email["category"] == "tools" else newsletters_label_id
        service.users().messages().modify(
            userId="me",
            id=email["id"],
            body={
                "addLabelIds":    [label_id],
                "removeLabelIds": ["INBOX"],
            }
        ).execute()
        if email["category"] == "tools":
            tools_count += 1
        else:
            newsletters_count += 1

    print(f"📁 Filed {tools_count} emails to Tools, {newsletters_count} to Newsletters")


# ── Date state helpers ────────────────────────────────────────────────────────

def get_last_run_date():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            raw = f.read().strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return (datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS))


def save_last_run_date(date_obj):
    with open(STATE_FILE, "w") as f:
        f.write(date_obj.strftime("%Y-%m-%d"))


# ── Claude summarization ──────────────────────────────────────────────────────

DIGEST_PROMPT = """You are creating a daily email digest for Elizabeth (elizabeth@wit-whittle.com).

You will receive two types of emails:
- "to_read" emails: editorial newsletters and curated content (TLDR, Product Hunt, Lenny's Newsletter, etc.)
- "tools" emails: vendor updates from software companies Elizabeth uses (product announcements, changelogs, feature releases)

Produce a digest with FOUR sections in this exact order. Each email appears in exactly ONE section — no duplicates across sections.

---

## 📰 Today's Biggest Stories
What's actually happening in the world right now, based purely on coverage volume and significance — NOT filtered by Elizabeth's interests.

- Identify the 3-5 topics most covered across the "to_read" emails
- For each: one headline, 1-2 sentences on why it matters broadly, which sources covered it (name + email), and a link if available
- This section is interest-agnostic — it reflects what the world is talking about
- Only use "to_read" emails for this section
- Do NOT repeat these items in any later section

---

## 🔥 Relevant to You
Stories from the "to_read" emails that are directly relevant to Elizabeth's interests: AI, tech, no-code tools, and higher education.

- Only include items NOT already in Today's Biggest Stories
- For each: topic headline, why it matters to Elizabeth in 1-2 sentences, source (name + email), link if available
- Limit to 5 items maximum
- If an item would appear here AND in Today's Biggest Stories, it stays in Today's Biggest Stories only

---

## 🔧 Vendor Updates
Product news, feature releases, changelogs, and announcements from the "tools" emails.

- These are vendor-generated updates, not independent editorial coverage
- For each: vendor name, what changed or launched, in one sentence, link if available
- Do NOT editorialize or inflate the significance of vendor announcements
- List all tools emails here — do not move them to other sections

---

## 📬 Everything Else
Any "to_read" emails not covered in Today's Biggest Stories or Relevant to You.

- One line per item: sender name, sender email, topic, link if available
- Keep it compact

---

FORMAT RULES:
- Clean, readable HTML with good typography
- Subtle color for section headers
- Links clearly styled as clickable
- Date range at the very top (e.g. "Covering emails from July 20–24, 2026")
- Total reading time under 5 minutes
- Always show sender name and email for every item
- If a section has no items, omit it entirely rather than showing an empty section

Here are the emails (each includes a "category" field of either "tools" or "to_read"):

{emails_json}
"""


def generate_digest(emails, date_range_str):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    emails_for_claude = [{k: v for k, v in e.items() if k != "id"} for e in emails]
    emails_json = json.dumps(emails_for_claude, indent=2)
    prompt = DIGEST_PROMPT.format(emails_json=emails_json)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ── Send email ────────────────────────────────────────────────────────────────

def send_digest_email(service, html_body, today):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Daily Digest — {today.strftime('%A, %B %-d, %Y')}"
    msg["From"]    = f"{SENDER_NAME} <{RECIPIENT_EMAIL}>"
    msg["To"]      = RECIPIENT_EMAIL

    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    print(f"✅ Digest sent to {RECIPIENT_EMAIL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today          = datetime.now(timezone.utc).date()
    last_run       = get_last_run_date()
    after_date_str = last_run.strftime("%Y/%m/%d")
    date_range_str = f"{last_run.strftime('%B %-d')} – {today.strftime('%B %-d, %Y')}"

    print(f"📅 Searching for emails after {after_date_str}")

    service              = get_gmail_service()
    tools_label_id       = get_label_id(service, "Tools")
    newsletters_label_id = get_label_id(service, "Newsletters")
    emails               = fetch_emails(service, after_date_str)

    print(f"📬 Found {len(emails)} emails to process ({sum(1 for e in emails if e['category'] == 'tools')} tools, {sum(1 for e in emails if e['category'] == 'to_read')} to_read)")

    if emails:
        html_body = generate_digest(emails, date_range_str)
        send_digest_email(service, html_body, today)
        file_emails(service, emails, tools_label_id, newsletters_label_id)
    else:
        print("No matching emails found — skipping digest.")

    save_last_run_date(today)


if __name__ == "__main__":
    main()
