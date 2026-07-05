"""
Daily Email Digest
Reads Gmail newsletters, summarizes via Claude API, sends digest to Elizabeth,
then moves processed emails to the Newsletters label.
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

RECIPIENT_EMAIL  = "elizabeth@wit-whittle.com"
SENDER_NAME      = "Daily Digest"
LOOKBACK_DAYS    = 3
MAX_EMAILS       = 50
STATE_FILE       = "last_run.txt"
NEWSLETTER_LABEL = "Newsletters"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

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
    raise ValueError(f"Label '{label_name}' not found in Gmail. Please create it first.")


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
    query = f"after:{after_date_str} -in:sent -in:drafts"
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
        body    = get_message_body(msg_data)

        if len(body) > 4000:
            body = body[:4000] + "\n\n[truncated]"

        emails.append({
            "id":      m["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "from":    headers.get("From", "(unknown sender)"),
            "date":    headers.get("Date", ""),
            "body":    body,
        })

    return emails


def move_to_newsletters(service, message_ids, label_id):
    for msg_id in message_ids:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={
                "addLabelIds":    [label_id],
                "removeLabelIds": ["INBOX"],
            }
        ).execute()
    print(f"📁 Moved {len(message_ids)} emails to Newsletters label")


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

Below is a list of emails received since the last digest. Your job is to:

1. Filter to newsletters, digests, blog updates, product announcements, and industry news.
   Skip: transactional emails, direct personal messages, calendar invites, spam, and any banking or financial institution emails.

2. Analyze and group topics. Flag any topic or theme that appears in 2 or more different sources.

3. Organize the digest into three sections:

   🔥 Top Stories — Items highly relevant to Elizabeth's primary interests (AI / Tech / No-code tools / Higher education),
   OR items appearing in multiple sources. List the 5-8 most important with:
   - Topic headline
   - Why it matters in 1-2 sentences
   - Which source(s) covered it (sender name and email)
   - A link if one was included in the email

   📡 Cross-Source Signals — Topics mentioned by 2+ newsletters. Brief note on what's being said and why multiple sources care.

   📬 Everything Else — Compact list of other newsletters. For each: sender name, sender email, one-line summary, link if available.

4. Format as clean readable HTML — good font, clear section headers, subtle color, links styled as clickable.
   Keep it scannable. Elizabeth should read the whole thing in under 5 minutes.

5. At the top, note the date range covered.

Constraints:
- Summaries and key points only — no full article text.
- No banking or financial emails anywhere.
- Always show sender name and email for every item.
- If fewer than 3 newsletters found, note that and still produce the digest.

Here are the emails:

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

    service  = get_gmail_service()
    label_id = get_label_id(service, NEWSLETTER_LABEL)
    emails   = fetch_emails(service, after_date_str)

    print(f"📬 Found {len(emails)} emails to analyze")

    html_body = generate_digest(emails, date_range_str)
    send_digest_email(service, html_body, today)

    message_ids = [e["id"] for e in emails]
    if message_ids:
        move_to_newsletters(service, message_ids, label_id)

    save_last_run_date(today)


if __name__ == "__main__":
    main()
