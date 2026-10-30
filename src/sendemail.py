import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

# load credentials from a local .env file (never committed to git)
load_dotenv()


DRY_RUN_ENV = "RAINCHECK_DRY_RUN"
logger = logging.getLogger(__name__)


def is_dry_run():
    """
    True when RAINCHECK_DRY_RUN is set to anything except an "off" value.
    Fails safe: a typo like "ture" suppresses email instead of sending it.
    """
    value = os.environ.get(DRY_RUN_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def send_email(subject, html_body, text_body, *, inline_images=()):
    """Sends a text + HTML email. Returns True if sent (or dry run), False if sending failed."""
    # dry run returns before we touch SMTP or SENDER_PASSWORD, so it works with no credentials
    if is_dry_run():
        recipient = (
            os.environ.get("RECEIVER_EMAIL")
            or os.environ.get("SENDER_EMAIL")
            or "(not configured)"
        )
        print("[DRY RUN] Email not sent (RAINCHECK_DRY_RUN is set).")
        print(f"To: {recipient}")
        print(f"Subject: {subject}")
        print("--- text/plain ---")
        print(text_body)
        print("--- text/html ---")
        print(html_body)
        return True

    sender_email = os.environ["SENDER_EMAIL"]
    sender_password = os.environ["SENDER_PASSWORD"]

    # default to emailing myself if no separate receiver is set
    receiver_email = os.environ.get("RECEIVER_EMAIL", sender_email)

    msg = MIMEMultipart("alternative")
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = subject

    # plain text first, HTML last: the last alternative is the preferred one (RFC 2046).
    # utf-8 is explicit because listing titles are Japanese
    msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    if inline_images:
        # The HTML alternative owns exactly the images its CID references use.
        related = MIMEMultipart('related')
        related.attach(MIMEText(html_body, 'html', 'utf-8'))
        for image in inline_images:
            part = MIMEImage(image.data, _subtype=image.content_type.split('/', 1)[1])
            part.add_header('Content-ID', f'<{image.cid}>')
            part.add_header('Content-Disposition', 'inline')
            related.attach(part)
        msg.attach(related)
    else:
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
        # timeout so a hung mail server can't stall the run forever
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
            # encrypt the connection before logging in
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
            logger.info("Digest email sent")
        return True
    except Exception as exc:
        logger.error("Unable to send email (%s)", type(exc).__name__)
        # the caller must know, so it doesn't record deals that were never emailed
        return False
