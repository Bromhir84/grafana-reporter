import smtplib
import os
from email.message import EmailMessage
from .config import SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM

def send_email(to_email, pdf_path=None, csv_files=None, subject="Grafana Report"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = to_email

    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(pdf_path))

    for csv_file in csv_files or []:
        with open(csv_file, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_file))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
