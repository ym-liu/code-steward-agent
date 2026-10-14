import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

SMTP_HOST = "smtp.office365.com"
RECIPIENTS = ["finance-team@example.com"]


def send(attachment):
    msg = EmailMessage()
    msg["Subject"] = f"Monthly sales report - {Path(attachment).stem}"
    msg["From"] = os.environ["REPORT_SMTP_USER"]
    msg["To"] = ", ".join(RECIPIENTS)
    msg.set_content("Hi all,\n\nThe merged monthly sales report is attached.\n\nAmy")
    data = Path(attachment).read_bytes()
    msg.add_attachment(data, maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename=Path(attachment).name)
    with smtplib.SMTP(SMTP_HOST, 587) as server:
        server.starttls()
        server.login(os.environ["REPORT_SMTP_USER"], os.environ["REPORT_SMTP_PASSWORD"])
        server.send_message(msg)


if __name__ == "__main__":
    send(sys.argv[1])
    print("sent")
