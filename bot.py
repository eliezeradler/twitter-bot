import os
import time
import json
import asyncio
import re
import difflib
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# הגדרות סביבה
SPACE_NAME = os.environ.get('CHAT_SPACE')
CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('GOOGLE_REFRESH_TOKEN')

API_ID = os.environ.get('API_ID')
API_HASH = os.environ.get('API_HASH')
SESSION_STRING = os.environ.get('TELEGRAM_STRING_SESSION')

# רשימת הערוצים בטלגרם למעקב
TARGET_CHANNELS_ENV = os.environ.get('TELEGRAM_CHANNELS', '')
TARGET_CHANNELS = [ch.strip() for ch in TARGET_CHANNELS_ENV.split(',') if ch.strip()]

STATE_FILE = 'last_ids.json'

AD_WORDS = [
    "לפרטים נוספים לחצו", "לרכישה", "להזמנות", "מכירת", "לשליחת קורות חיים",
    "לפרטים והרשמה", "הלינק", "השאירו פרטים", "מספר המקומות מוגבל",
    "אסור לכם לפספס", "לחצו כאן ", "לפרטים נוספים", "יפה תורה עם דרך ארץ",
    "לפרטים מלאים", "לרכישת כרטיסים", "utm_source=", "utm_campaign=", "ללא עלות" ,"לפרטים והזמנות" ,"לחצו כעת" ,"אל תפספסו",
    
]

def is_ad(text):
    if not text: return False
    for word in AD_WORDS:
        if word in text: return True
    return False

def clean_text(text):
    """ מסיר קישורים ושורות חתימה/הצטרפות מהטקסט """
    if not text: return ""
    
    # 1. הסרת קישורי טלגרם ווואטסאפ מכל סוג
    text = re.sub(r'(https?://)?(t\.me|telegram\.me|chat\.whatsapp\.com|wa\.me)[^\s]*', '', text)
    
    # 2. הסרת שורות חתימה ודרכי הצטרפות
    footer_markers = [
        "לשליחת חומרים", 
        "להצטרפות:", 
        "ערוץ וואטסאפ", 
        "גם בטלגרם",
        "צאפ מגזין בטלגרם - חדשות ועדכונים סביב השעון:",
        "@ZiratNews",
        "@N12chat",
        "הכי חם ברשת - ’הערינג’",
        "דיווחים ראשוניים בערוץ",
        " רשת החדשות של בית שמש",
        "לעדכוני הפרגוד בטלגרם",
        "כדי להגיב לכתבה לחצו כאן",
        "לכל העדכונים",
        "דרך הקישור",
    ]
    
    lines = text.split('\n')
    valid_lines = []
    
    for line in lines:
        if any(marker in line for marker in footer_markers) and len(line) < 80:
            continue
        valid_lines.append(line)
        
    return '\n'.join(valid_lines).strip()

def is_too_similar(new_text, seen_texts, threshold=0.70):
    """ בודק דמיון של 70% ומעלה בהשוואה להודעות קודמות """
    if not new_text: return False
    check_text = new_text[:200]
    for seen in seen_texts:
        if difflib.SequenceMatcher(None, check_text, seen[:200]).ratio() >= threshold:
            return True
    return False

def get_user_credentials():
    print("Authenticating to Google Chat via OAuth...")
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

def upload_media_to_chat(token, file_path, filename):
    try:
        content_type = "application/octet-stream"
        if filename.endswith(".mp4"): content_type = "video/mp4"
        elif filename.endswith((".jpg", ".jpeg")): content_type = "image/jpeg"
        elif filename.endswith(".png"): content_type = "image/png"
        elif filename.endswith(".webp"): content_type = "image/webp"
        elif filename.endswith(".mp3"): content_type = "audio/mpeg"
        elif filename.endswith(".pdf"): content_type = "application/pdf"
        
        upload_url = f"https://chat.googleapis.com/upload/v1/{SPACE_NAME}/attachments:upload?filename={filename}&uploadType=media"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type
        }

        print(f"Uploading {filename} to Google Chat...")
        with open(file_path, 'rb') as f:
            file_data = f.read()
            
        res = requests.post(upload_url, headers=headers, data=file_data, timeout=120)
        
        if res.status_code != 200:
            print(f"Upload failed: {res.text}")
            return None
            
        data = res.json()
        return data.get('attachmentDataRef', {}).get('attachmentUploadToken')
        
    except Exception as e:
        print(f"Error uploading: {e}")
        return None

def send_chat_message(token, text, attachment_tokens):
    payload = {"text": text}
    if attachment_tokens:
        payload["attachment"] = [{"attachmentDataRef": {"attachmentUploadToken": t}} for t in attachment_tokens]
        
    msg_url = f"https://chat.googleapis.com/v1/{SPACE_NAME}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    res = requests.post(msg_url, headers=headers, json=payload)
    if res.status_code == 200:
        print("Message sent successfully!")
        return True
    else:
        print(f"Error posting message: {res.text}")
        return False

async def main():
    if not TARGET_CHANNELS:
        print("No target channels configured.")
        return

    # זיהוי האם זו ריצת אתחול
    is_initial_run = not os.path.exists(STATE_FILE)
    if is_initial_run:
        print("🚀 זוהתה ריצת אתחול! הבוט יסרוק וישמור את ההיסטוריה מבלי לשלוח הודעות לצ'אט.")

    states = {}
    if not is_initial_run:
        with open(STATE_FILE, 'r') as f:
            try: states = json.load(f)
            except: pass

    if "global_seen_texts" not in states:
        states["global_seen_texts"] = []

    # במידה וזו ריצת אתחול, אין טעם להתחבר לגוגל כי לא נשלח כלום
    token = get_user_credentials() if not is_initial_run else None

    client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
    await client.connect()

    for channel in TARGET_CHANNELS:
        print(f"\n--- Checking channel: {channel} ---")
        last_id = states.get(channel, 0)
        highest_id_processed = last_id

        try:
            # בקשת הודעות מהישנה לחדשה (רק מה שלא נקרא עדיין)
            messages = await client.get_messages(channel, min_id=last_id, limit=20, reverse=True)
            
            if not messages:
                print("No new messages.")
                continue

            for message in messages:
                print(f"Processing message ID: {message.id}")
                
                raw_text = message.text or ""
                clean_msg = clean_text(raw_text)

                # אם זו ריצת אתחול - פשוט שומרים את ה-ID ואת הטקסט למאגר (כדי למנוע כפילויות בעתיד) ומדלגים הלאה
                if is_initial_run:
                    highest_id_processed = message.id
                    if clean_msg and not is_ad(raw_text):
                        states["global_seen_texts"].append(clean_msg)
                    continue

                if is_ad(raw_text):
                    print("Ad detected, skipping message.")
                    highest_id_processed = message.id
                    continue
                
                # מניעת כפילויות - סף דמיון של 70%
                if clean_msg and is_too_similar(clean_msg, states["global_seen_texts"], threshold=0.70):
                    print("Similar content detected, skipping.")
                    highest_id_processed = message.id
                    continue

                attachment_tokens = []
                if message.media:
                    print("Downloading media via Telethon...")
                    file_path = await client.download_media(message)
                    if file_path:
                        filename = os.path.basename(file_path)
                        upload_token = upload_media_to_chat(token, file_path, filename)
                        if upload_token:
                            attachment_tokens.append(upload_token)
                        os.remove(file_path)
                        time.sleep(1.5)

                if not clean_msg and not attachment_tokens:
                    highest_id_processed = message.id
                    continue

                final_text = clean_msg if clean_msg else "קובץ מצורף"
                success = send_chat_message(token, final_text, attachment_tokens)
                
                if success:
                    highest_id_processed = message.id
                    if clean_msg:
                        states["global_seen_texts"].append(clean_msg)

                time.sleep(1)

        except Exception as e:
            print(f"Error reading channel {channel}: {e}")

        states[channel] = highest_id_processed
        states["global_seen_texts"] = states["global_seen_texts"][-100:]

        # שומר את הנתונים לקובץ בסיום הסריקה של כל ערוץ
        with open(STATE_FILE, 'w') as f:
            json.dump(states, f)

    await client.disconnect()
    
    if is_initial_run:
        print("\n✅ ריצת האתחול הסתיימה בהצלחה! קובץ last_ids.json נוצר. בריצה הבאה הבוט יתחיל לשלוח הודעות חדשות.")

if __name__ == "__main__":
    asyncio.run(main())
