import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

# load credentials from a local .env file (never committed to git)
load_dotenv()


DRY_RUN_ENV = "RAINCHECK_DRY_RUN"


def is_dry_run():
    """
    True when RAINCHECK_DRY_RUN is set to anything except an "off" value.
    Fails safe: a typo like "ture" suppresses email instead of sending it.
    """
    value = os.environ.get(DRY_RUN_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def send_email(subject, body):
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
        print(body)
        return

    sender_email = os.environ["SENDER_EMAIL"]
    sender_password = os.environ["SENDER_PASSWORD"]

    # default to emailing myself if no separate receiver is set
    receiver_email = os.environ.get("RECEIVER_EMAIL", sender_email)

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = subject

    # send the body as HTML so images and links render in the email client
    msg.attach(MIMEText(body, 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            # encrypt the connection before logging in
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
            print("Email sent!")
    except Exception as e:
        print(f"Error: unable to send email ({e})")
