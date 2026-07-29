import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv

# load credentials from a local .env file (never committed to git)
load_dotenv()


def send_email(subject, body):
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
