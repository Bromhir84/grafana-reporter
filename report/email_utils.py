import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
from ..config import SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM
import logging

logger = logging.getLogger(__name__)

def send_email(pdf_path, csv_files, report_uid, email_to):
    msg = EmailMessage()
    msg["Subject"] = f"Grafana Report - {report_uid} - {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = email_to

    if pdf_path and os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=f"{report_uid}.pdf")

    for csv_file in csv_files:
        if os.path.exists(csv_file):
            with open(csv_file, "rb") as f:
                msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_file))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        if SMTP_USERNAME and SMTP_PASSWORD:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
    logger.info(f"Email sent to {email_to}")
