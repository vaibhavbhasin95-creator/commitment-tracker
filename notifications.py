"""
Real nudge delivery (0.6.0). One function per channel, all returning
(status, detail) where status is 'sent', 'failed' or 'skipped' — they never
raise, so a broken channel can't break the request or scheduler that called it.

  email  — SMTP (Gmail with an app password, Outlook/Microsoft 365, SendGrid,
           Resend, or any SMTP server). Sent to the task owner's email.
  slack  — a bot token (xoxb-...) sends the owner a direct message, found by
           their email (scopes: chat:write, users:read.email); or an incoming
           webhook URL posts in one channel, naming the owner.
  teams  — a Teams "Workflows" (Power Automate) webhook URL. The payload is an
           Adaptive Card that the standard "post to a channel" workflow shows
           as-is, plus recipient_email / text fields a custom workflow can use
           to message the owner directly in chat.
  custom — POSTs a JSON payload describing the reminder to any URL.

Only the standard library is used (smtplib, urllib) — no new dependencies.
"""

import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

HTTP_TIMEOUT = 15
REASON_TEXT = {
    "test": "Test reminder",
    "day_before": "Due tomorrow",
    "on_due": "Due today",
    "overdue": "Overdue",
}
EMAIL_REQUIRED = ("smtp_host", "smtp_port", "from_email")


def compose(commitment, reason):
    """Subject line and plain-text body for one reminder."""
    headline = REASON_TEXT.get(reason, "Reminder")
    due = commitment.get("due_date") or "no due date"
    subject = f"{headline}: {commitment['task_text'][:80]}"
    body = (
        f"Hi {commitment.get('owner_name') or 'there'},\n\n"
        f"{headline} — a commitment from the meeting \"{commitment.get('meeting_title') or 'a recent meeting'}\":\n\n"
        f"  {commitment['task_text']}\n"
        f"  Due: {due}\n"
        f"  Status: {commitment.get('status', 'pending').replace('_', ' ')}\n\n"
        "Sent by Commitment Tracker."
    )
    return subject, body


def _payload(commitment, reason, subject, body):
    return {
        "reason": reason,
        "subject": subject,
        "text": body,
        "recipient_email": commitment.get("owner_email"),
        "recipient_name": commitment.get("owner_name"),
        "task": commitment.get("task_text"),
        "due_date": commitment.get("due_date"),
        "status": commitment.get("status"),
        "meeting": commitment.get("meeting_title"),
    }


def _post_json(url, payload, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return raw


# ---------------------------------------------------------------------------

def send_email(config, to_email, subject, body):
    missing = [f for f in EMAIL_REQUIRED if not str(config.get(f) or "").strip()]
    if missing:
        return "failed", f"email settings incomplete: missing {', '.join(missing)}"
    if not to_email:
        return "skipped", "the task owner has no email — add them on the People page or set it on the task"

    msg = EmailMessage()
    msg["From"] = config["from_email"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    host, port = config["smtp_host"].strip(), int(config["smtp_port"])
    security = config.get("smtp_security") or ("ssl" if port == 465 else "starttls")
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=HTTP_TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT)
        with server:
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if config.get("username"):
                server.login(config["username"], config.get("password") or "")
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return "failed", "the email server rejected the username/password (Gmail needs an app password)"
    except (smtplib.SMTPException, OSError) as e:
        return "failed", f"couldn't send email via {host}:{port}: {e}"
    return "sent", f"emailed {to_email}"


def send_slack(value, commitment, subject, body):
    value = (value or "").strip()
    if value.startswith("xoxb-"):
        to_email = commitment.get("owner_email")
        if not to_email:
            return "skipped", "the task owner has no email, so their Slack account can't be found"
        auth = {"Authorization": f"Bearer {value}"}
        try:
            req = urllib.request.Request(
                "https://slack.com/api/users.lookupByEmail?" + urllib.parse.urlencode({"email": to_email}), headers=auth)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                user = json.loads(resp.read().decode("utf-8"))
            if not user.get("ok"):
                return "failed", f"Slack couldn't find {to_email}: {user.get('error')}"
            sent = json.loads(_post_json("https://slack.com/api/chat.postMessage",
                                         {"channel": user["user"]["id"], "text": f"*{subject}*\n{body}"}, auth))
        except (urllib.error.URLError, OSError, ValueError) as e:
            return "failed", f"couldn't reach Slack: {e}"
        if not sent.get("ok"):
            return "failed", f"Slack refused the message: {sent.get('error')}"
        return "sent", f"Slack direct message to {to_email}"
    if value.startswith("https://hooks.slack.com/"):
        owner = commitment.get("owner_name") or "the owner"
        who = f"{owner} ({commitment['owner_email']})" if commitment.get("owner_email") else owner
        try:
            _post_json(value, {"text": f"*{subject}* — for {who}\n{body}"})
        except (urllib.error.URLError, OSError) as e:
            return "failed", f"Slack webhook error: {e}"
        return "sent", "posted to the Slack channel"
    return "failed", "Slack needs a bot token (xoxb-...) or an incoming webhook URL (https://hooks.slack.com/...)"


def send_teams(url, commitment, reason, subject, body):
    if not (url or "").startswith("https://"):
        return "failed", "Teams needs a Workflows webhook URL (https://...)"
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": subject, "weight": "Bolder", "wrap": True},
                    {"type": "TextBlock", "text": body, "wrap": True},
                ],
            },
        }],
    }
    card.update(_payload(commitment, reason, subject, body))
    try:
        _post_json(url, card)
    except (urllib.error.URLError, OSError) as e:
        return "failed", f"Teams workflow error: {e}"
    return "sent", "sent to the Teams workflow" + (f" for {commitment['owner_email']}" if commitment.get("owner_email") else "")


def send_custom(url, commitment, reason, subject, body):
    if not (url or "").startswith(("https://", "http://")):
        return "failed", "the custom channel needs a URL to POST to"
    try:
        _post_json(url, _payload(commitment, reason, subject, body))
    except (urllib.error.URLError, OSError) as e:
        return "failed", f"custom webhook error: {e}"
    return "sent", "posted to the custom webhook"


def deliver(channel, webhook_or_key, config, commitment, reason):
    """Sends one reminder for one task over one channel. Never raises."""
    try:
        subject, body = compose(commitment, reason)
        if channel == "email":
            return send_email(config, commitment.get("owner_email"), subject, body)
        if channel == "slack":
            return send_slack(webhook_or_key, commitment, subject, body)
        if channel == "teams":
            return send_teams(webhook_or_key, commitment, reason, subject, body)
        if channel == "custom":
            return send_custom(webhook_or_key, commitment, reason, subject, body)
        return "failed", f"unknown channel '{channel}'"
    except Exception as e:  # defensive: a channel bug must not break callers
        return "failed", f"unexpected error: {e}"
