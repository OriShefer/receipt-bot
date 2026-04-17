import os, json, base64, tempfile, time
from datetime import datetime
from io import BytesIO
from dotenv import load_dotenv
import requests
from flask import Flask, request
from PIL import Image
import anthropic
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account

load_dotenv()

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
AGENT_ID = os.getenv("AGENT_ID")
ENVIRONMENT_ID = os.getenv("ENVIRONMENT_ID")

# Load Google credentials from environment (paste your JSON key as one line)
SERVICE_ACCOUNT_INFO = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
creds = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
drive_service = build("drive", "v3", credentials=creds)
sheets_service = build("sheets", "v4", credentials=creds)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_telegram_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})


def download_telegram_photo(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
    file_path = r["result"]["file_path"]
    img_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}").content
    return img_bytes


def save_to_drive(img_bytes, filename):
    img = Image.open(BytesIO(img_bytes)).convert("RGB")
    pdf_buffer = BytesIO()
    img.save(pdf_buffer, format="PDF")
    pdf_buffer.seek(0)
    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID]
    }
    media = MediaIoBaseUpload(pdf_buffer, mimetype="application/pdf")
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True
    ).execute()
    # Make file readable by anyone with the link
    drive_service.permissions().create(
        fileId=file.get("id"),
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True
    ).execute()
    return file.get("webViewLink")


def append_to_sheet(data, drive_link):
    now = datetime.now()
    month_name = now.strftime("%B %Y")  # e.g. "April 2025"
    # Try to find existing sheet tab, else create it
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_names = [s["properties"]["title"] for s in spreadsheet["sheets"]]
    if month_name not in sheet_names:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": month_name}}}]}
        ).execute()
        # Add headers
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"{month_name}!A1",
            valueInputOption="RAW",
            body={"values": [["תאריך", "שם עסק", "פריטים", "סכום לפני מע\"מ", "מע\"מ", "סה\"כ", "אמצעי תשלום", "קישור PDF"]]}
        ).execute()
    row = [
        data.get("date", now.strftime("%d/%m/%Y")),
        data.get("business_name", ""),
        data.get("items_summary", ""),
        data.get("subtotal", ""),
        data.get("vat", ""),
        data.get("total", ""),
        data.get("payment_method", ""),
        drive_link
    ]
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID, range=f"{month_name}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]}
    ).execute()


def process_receipt_with_agent(img_bytes, chat_id):
    img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
    # Create a session
    session = anthropic_client.beta.sessions.create(
        agent=AGENT_ID,
        environment_id=ENVIRONMENT_ID,
        title=f"Receipt {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        betas=["managed-agents-2026-04-01"]
    )
    session_id = session.id
    # Send the image + instruction
    anthropic_client.beta.sessions.events.send(
        session_id=session_id,
        events=[{
            "type": "user.message",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": (
                    "זוהי תמונת קבלה. אנא חלץ את כל הפרטים והחזר JSON בדיוק בפורמט הזה (ללא טקסט נוסף):\n"
                    '{"date":"DD/MM/YYYY","business_name":"...","items_summary":"...","subtotal":"...","vat":"...","total":"...","payment_method":"..."}'
                )}
            ]
        }],
        betas=["managed-agents-2026-04-01"]
    )
    # Poll for response
    last_id = None
    agent_text = ""
    for _ in range(60):
        time.sleep(2)
        events = anthropic_client.beta.sessions.events.list(
            session_id=session_id,
            after_id=last_id,
            betas=["managed-agents-2026-04-01"]
        )
        for ev in events.data:
            last_id = ev.id
            if ev.type == "agent.message":
                for block in ev.content:
                    if block.type == "text":
                        agent_text += block.text
            if ev.type == "session.status_idle" and agent_text:
                return agent_text
    return agent_text


@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = request.json
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    photos = msg.get("photo")
    if not photos:
        send_telegram_message(chat_id, "אנא שלח תמונת קבלה 📸")
        return "ok"
    send_telegram_message(chat_id, "⏳ מעבד את הקבלה שלך...")
    try:
        file_id = photos[-1]["file_id"]
        img_bytes = download_telegram_photo(file_id)
        filename = f"קבלה_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        # Save to Drive
        drive_link = save_to_drive(img_bytes, filename)
        # Process with agent
        agent_response = process_receipt_with_agent(img_bytes, chat_id)
        # Parse JSON from agent
        try:
            data = json.loads(agent_response)
        except:
            import re
            match = re.search(r'\{.*\}', agent_response, re.DOTALL)
            data = json.loads(match.group()) if match else {}
        # Save to Sheets
        append_to_sheet(data, drive_link)
        # Build confirmation message
        confirmation = (
            f"✅ הקבלה נשמרה בהצלחה!\n\n"
            f"📅 תאריך: {data.get('date', 'לא זוהה')}\n"
            f"🏪 עסק: {data.get('business_name', 'לא זוהה')}\n"
            f"🛒 פריטים: {data.get('items_summary', 'לא זוהה')}\n"
            f"💰 סכום לפני מע\"מ: ₪{data.get('subtotal', 'לא זוהה')}\n"
            f"🧾 מע\"מ: ₪{data.get('vat', 'לא זוהה')}\n"
            f"💳 סה\"כ: ₪{data.get('total', 'לא זוהה')}\n"
            f"💳 תשלום: {data.get('payment_method', 'לא זוהה')}\n\n"
            f"📂 PDF נשמר בדרייב\n"
            f"📊 פרטים נוספו לגיליון האלקטרוני"
        )
        send_telegram_message(chat_id, confirmation)
    except Exception as e:
        send_telegram_message(chat_id, f"❌ שגיאה בעיבוד הקבלה: {str(e)}")
    return "ok"


if __name__ == "__main__":
    # Set webhook (run once)
    # requests.get(f"{TELEGRAM_API}/setWebhook?url=https://YOUR-APP-URL/webhook/{TELEGRAM_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
