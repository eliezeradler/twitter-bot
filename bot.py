import os
import time
import json
import asyncio
import random
import re
import difflib
import aiohttp
from aiolimiter import AsyncLimiter
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# הגדרות סביבה
SPACE_NAME = os.environ.get('CHAT_SPACE', '').strip()
if SPACE_NAME and not SPACE_NAME.startswith('spaces/'):
    SPACE_NAME = f"spaces/{SPACE_NAME}"

CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('GOOGLE_REFRESH_TOKEN')

API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
SESSION_STRING = os.environ.get('TELEGRAM_STRING_SESSION')
IS_MANUAL_INIT = os.environ.get('INIT_RUN', 'false') == 'true'

TARGET_CHANNELS_ENV = os.environ.get('TELEGRAM_CHANNELS', '')
TARGET_CHANNELS = [ch.strip() for ch in TARGET_CHANNELS_ENV.split(',') if ch.strip()]

STATE_FILE = 'last_ids.json'

# מגביל קצב קשיח: המתנה של 2.1 שניות בדיוק בין כל קריאה (מדיה -> הודעה -> מדיה הבאה)
chat_api_limiter = AsyncLimiter(1, 2.1)

AD_WORDS = [
    "לפרטים נוספים לחצו", "לרכישה", "להזמנות", "מכירת", "לשליחת קורות חיים",
    "לפרטים והרשמה", "הלינק", "השאירו פרטים", "מספר המקומות מוגבל",
    "אסור לכם לפספס", "לחצו כאן ", "לפרטים נוספים", "יפה תורה עם דרך ארץ",
    "לפרטים מלאים", "לרכישת כרטיסים", "utm_source=", "utm_campaign=", "ללא עלות",
    "לפרטים והזמנות", "לחצו כעת", "אל תפספסו"
]

def is_ad(text):
    if not text: return False
    return any(word in text for word in AD_WORDS)

def clean_text(text):
    if not text: return ""
    text = re.sub(r'(https?://)?(t\.me|telegram\.me|chat\.whatsapp\.com|wa\.me)[^\s]*', '', text)
    
    footer_markers = [
        "לשליחת חומרים", "להצטרפות:", "ערוץ וואטסאפ", "גם בטלגרם", "אוף דה רקורד",
        "ללא צנזורה", "צאפ מגזין בטלגרם - חדשות ועדכונים סביב השעון:", "@ZiratNews",
        "@N12chat", "הכי חם ברשת - ’הערינג’", "דיווחים ראשוניים בערוץ",
        " רשת החדשות של בית שמש", "לעדכוני הפרגוד בטלגרם", "כדי להגיב לכתבה לחצו כאן",
        "לכל העדכונים", "דרך הקישור"
    ]
    
    lines = text.split('\n')
    valid_lines = [l for l in lines if not (any(m in l for m in footer_markers) and len(l) < 80)]
    return '\n'.join(valid_lines).strip()

def is_too_similar(new_text, seen_texts, threshold=0.70):
    if not new_text: return False
    check_text = new_text[:200]
    return any(difflib.SequenceMatcher(None, check_text, s[:200]).ratio() >= threshold for s in seen_texts)

def get_user_credentials():
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/chat.messages"]
    )
    creds.refresh(Request())
    return creds.token

async def execute_request_with_official_backoff(session, method, url, headers, data=None, json_payload=None):
    max_attempts = 6
    
    for attempt in range(max_attempts):
        # המערכת ממתינה כאן אוטומטית 2.1 שניות מהבקשה הקודמת
        async with chat_api_limiter:
            try:
                async with session.request(method, url, headers=headers, data=data, json=json_payload, timeout=120) as res:
                    if res.status == 200:
                        return True, await res.json()
                    
                    error_text = await res.text()
                    if res.status == 429 or "RESOURCE_EXHAUSTED" in error_text:
                        wait_time = min((2 ** attempt) + random.uniform(0.1, 1.0), 32)
                        print(f" > עומס 429. ממתין {wait_time:.2f} שניות (ניסיון {attempt + 1}/{max_attempts})...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        return False, f"HTTP {res.status}: {error_text}"
            except Exception as e:
                wait_time = min((2 ** attempt) + random.uniform(0.1, 1.0), 32)
                print(f" > שגיאת רשת ({e}). ממתין {wait_time:.2f} שניות (ניסיון {attempt + 1}/{max_attempts})...")
                await asyncio.sleep(wait_time)
                
    return False, f"בקשה נכשלה סופית לאחר {max_attempts} ניסיונות."

async def upload_media_to_chat(session, token, file_path, filename):
    content_type = "application/octet-stream"
    if filename.endswith(".mp4"): content_type = "video/mp4"
    elif filename.endswith((".jpg", ".jpeg")): content_type = "image/jpeg"
    elif filename.endswith(".png"): content_type = "image/png"
    elif filename.endswith(".webp"): content_type = "image/webp"
    elif filename.endswith(".mp3"): content_type = "audio/mpeg"
    elif filename.endswith(".pdf"): content_type = "application/pdf"
    
    upload_url = f"https://chat.googleapis.com/upload/v1/{SPACE_NAME}/attachments:upload?filename={filename}&uploadType=media"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}

    print(f"Uploading {filename} to {SPACE_NAME}...")
    
    if os.path.getsize(file_path) / (1024 * 1024) > 200:
        return None, "הקובץ כבד מדי (מעל 200MB)."

    with open(file_path, 'rb') as f:
        file_data = f.read()

    success, res_data = await execute_request_with_official_backoff(session, 'POST', upload_url, headers, data=file_data)
    
    if success:
        return res_data.get('attachmentDataRef', {}).get('attachmentUploadToken'), None
    return None, str(res_data)

async def send_chat_message(session, token, text, attachment_tokens):
    payload = {"text": text}
    if attachment_tokens:
        payload["attachment"] = [{"attachmentDataRef": {"attachmentUploadToken": t}} for t in attachment_tokens]
        
    msg_url = f"https://chat.googleapis.com/v1/{SPACE_NAME}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    success, res_data = await execute_request_with_official_backoff(session, 'POST', msg_url, headers, json_payload=payload)
    return success, res_data

async def main():
    if not TARGET_CHANNELS:
        return

    is_global_initial_run = not os.path.exists(STATE_FILE) or IS_MANUAL_INIT
    if is_global_initial_run:
        print("🚀 זוהתה ריצת אתחול! הבוט יסרוק וישמור היסטוריה מבלי לשלוח הודעות לצ'אט.")

    states = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try: states = json.load(f)
            except: pass

    if "global_seen_texts" not in states:
        states["global_seen_texts"] = []

    token = get_user_credentials() if not is_global_initial_run else None

    async with aiohttp.ClientSession() as aio_session:
        client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
        await client.connect()

        for channel in TARGET_CHANNELS:
            print(f"\n--- Checking channel: {channel} ---")
            try:
                entity = await client.get_entity(channel)
                channel_title = entity.title
                
                last_id = states.get(channel, 0)
                highest_id_processed = last_id
                
                is_channel_initial_run = is_global_initial_run or last_id == 0

                if is_channel_initial_run:
                    messages = await client.get_messages(entity, limit=10)
                else:
                    messages = await client.get_messages(entity, min_id=last_id, limit=5, reverse=True)
                
                if not messages:
                    continue

                for message in messages:
                    raw_text = message.text or ""
                    clean_msg = clean_text(raw_text)

                    if is_channel_initial_run:
                        highest_id_processed = max(highest_id_processed, message.id)
                        if clean_msg and not is_ad(raw_text):
                            states["global_seen_texts"].append(clean_msg)
                        continue

                    if is_ad(raw_text):
                        highest_id_processed = message.id
                        continue
                    
                    if clean_msg and is_too_similar(clean_msg, states["global_seen_texts"], threshold=0.70):
                        highest_id_processed = message.id
                        continue

                    file_path = None
                    if message.media:
                        print("Downloading media via Telethon...")
                        file_path = await client.download_media(message)

                    attachment_tokens = []
                    upload_errors = []
                    
                    if file_path:
                        filename = os.path.basename(file_path)
                        upload_token, upload_error = await upload_media_to_chat(aio_session, token, file_path, filename)
                        
                        if upload_token:
                            attachment_tokens.append(upload_token)
                        elif upload_error:
                            upload_errors.append(upload_error)

                    if not clean_msg and not attachment_tokens and not upload_errors:
                        if file_path:
                            try: os.remove(file_path)
                            except: pass
                        continue

                    formatted_text = f"*{channel_title}*\n\n{clean_msg}" if clean_msg else f"*{channel_title}*\n\n[ללא טקסט]"
                    
                    if upload_errors:
                        formatted_text += f"\n\n*(⚠️ הבוט לא הצליח להעלות קובץ מצורף להודעה זו. שגיאה: {upload_errors[0]})*"
                    
                    success, send_error = await send_chat_message(aio_session, token, formatted_text, attachment_tokens)
                    if success:
                        highest_id_processed = message.id
                        if clean_msg:
                            states["global_seen_texts"].append(clean_msg)
                    else:
                        print(f"Message failed: {send_error}")

                    if file_path:
                        try: os.remove(file_path)
                        except: pass

                states[channel] = highest_id_processed

            except Exception as e:
                print(f"Error processing channel {channel}: {e}")

            states["global_seen_texts"] = states["global_seen_texts"][-100:]
            with open(STATE_FILE, 'w') as f:
                json.dump(states, f)

        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
