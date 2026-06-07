import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage


def send_email(subject, body):
    sender_email = "nicholassukhdeo1@gmail.com"
    sender_password = "xhhj cuou lglf wpso"

    #sending email to myself rn
    receiver_email = "nicholassukhdeo1@gmail.com" 


    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = subject


    # but you need to use the ".attach()"
    # method to add ur body text into here

    # make the email body in html so you can print
    # images in the body.. instead of 'plain' text
    msg.attach(MIMEText(body, 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            #this method ".starttls() encrypts our msg"
            server.starttls()
            # log into acc where you're sending ur msg from
            server.login(sender_email, sender_password)
            text = msg.as_string()

            server.sendmail(sender_email, receiver_email, text)
            print("Email sent!")
    except:
        print("Error: unable to send email")