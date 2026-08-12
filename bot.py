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
IS_MANUAL_INIT = os.environ.get('INIT_RUN', 'false') == 'true'

# רשימת הערוצים בטלגרם למעקב
TARGET_CHANNELS_ENV = os.environ.get('TELEGRAM_CHANNELS', '')
TARGET_CHANNELS = [ch.strip() for ch in TARGET_CHANNELS_ENV.split(',') if ch.strip()]

# הגדרת שכפול למרחבים נוספים בגוגל צ'אט
EXTRA_GOOGLE_CHAT_SPACES = {
    'merkaz': 'spaces/AAQAxPM8YDI',
    'taagad_news': 'spaces/AAQAxPM8YDI'
}

STATE_FILE = 'last_ids.json'

# רשימה מסונכרנת של מילות פרסומת
AD_WORDS = [
    "לפרטים נוספים לחצו", "לרכישה", "להזמנות", "מכירת", "לשליחת קורות חיים",
    "לפרטים והרשמה", "הלינק", "השאירו פרטים", "מספר המקומות מוגבל",
    "אסור לכם לפספס", "לחצו כאן ", "לפרטים נוספים", "יפה תורה עם דרך ארץ",
    "לפרטים מלאים", "לרכישת כרטיסים", "utm_source=", "utm_campaign=", "ללא עלות",
    "לפרטים והזמנות", "לחצו כעת", "אל תפספסו"
]

def is_ad(text):
    if not text: return False
    for word in AD_WORDS:
        if word in text: return True
    return False

def clean_text(text):
    """ מסיר קישורים ושורות חתימה/הצטרפות מהטקסט """
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
    valid_lines = []
    
    for line in lines:
        if any(marker in line for marker in footer_markers) and len(line) < 80:
            continue
        valid_lines.append(line)
        
    return '\n'.join(valid_lines).strip()

def is_too_similar(new_text, seen_texts, threshold=0.70):
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

def upload_media_to_chat(token, file_path, filename, target_space):
    """ מעלה מדיה עם מנגנון השהיה מעריכית (Exponential Backoff) במקרה של 429 """
    try:
        content_type = "application/octet-stream"
        if filename.endswith(".mp4"): content_type = "video/mp4"
        elif filename.endswith((".jpg", ".jpeg")): content_type = "image/jpeg"
        elif filename.endswith(".png"): content_type = "image/png"
        elif filename.endswith(".webp"): content_type = "image/webp"
        elif filename.endswith(".mp3"): content_type = "audio/mpeg"
        elif filename.endswith(".pdf"): content_type = "application/pdf"
        
        upload_url = f"https://chat.googleapis.com/upload/v1/{target_space}/attachments:upload?filename={filename}&uploadType=media"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type
        }

        print(f"Uploading {filename} to {target_space}...")
        
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if file_size_mb > 200:
            return None, f"הקובץ כבד מדי ({file_size_mb:.1f}MB). גוגל חוסמת קבצים מעל 200MB."

        with open(file_path, 'rb') as f:
            file_data = f.read()

        last_error_msg = "שגיאה לא ידועה"
        upload_res = None
        
        # מנגנון ניסיונות חוזרים (עד 5 ניסיונות)
        for attempt in range(5):
            try:
                res = requests.post(upload_url, headers=headers, data=file_data, timeout=120)
                
                if res.status_code == 200:
                    upload_res = res.json()
                    break # ההעלאה הצליחה, יוצאים מהלולאה
                else:
                    error_msg = res.text
                    last_error_msg = error_msg
                    
                    # בדיקה האם זו שגיאת עומס
                    if ("429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg) and attempt < 4:
                        wait_time = 5 * (2 ** attempt) # 5, 10, 20, 40...
                        print(f" > עומס כתיבה (429). ממתין {wait_time} שניות ומנסה שוב (ניסיון {attempt + 1}/5)...")
                        time.sleep(wait_time)
                    else:
                        break # שגיאה אחרת (או שניסינו 5 פעמים), נוותר
                        
            except Exception as e:
                last_error_msg = str(e)
                if attempt < 4:
                    wait_time = 5 * (2 ** attempt)
                    print(f" > שגיאת רשת בהעלאה. ממתין {wait_time} שניות ומנסה שוב (ניסיון {attempt + 1}/5)...")
                    time.sleep(wait_time)
                else:
                    break

        if upload_res:
            return upload_res.get('attachmentDataRef', {}).get('attachmentUploadToken'), None
        else:
            print(f"Upload completely failed after retries. Error: {last_error_msg}")
            return None, last_error_msg
            
    except Exception as e:
        print(f"Critical error uploading: {e}")
        return None, str(e)

def send_chat_message(token, text, attachment_tokens, target_space):
    payload = {"text": text}
    if attachment_tokens:
        payload["attachment"] = [{"attachmentDataRef": {"attachmentUploadToken": t}} for t in attachment_tokens]
        
    msg_url = f"https://chat.googleapis.com/v1/{target_space}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    res = requests.post(msg_url, headers=headers, json=payload)
    if res.status_code == 200:
        print(f"Message sent successfully to {target_space}!")
        return True
    else:
        print(f"Error posting message to {target_space}: {res.text}")
        return False

async def main():
    if not TARGET_CHANNELS:
        print("No target channels configured.")
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
                messages = await client.get_messages(entity, limit=20)
            else:
                messages = await client.get_messages(entity, min_id=last_id, limit=20, reverse=True)
            
            if not messages:
                print("No new messages.")
                continue

            for message in messages:
                print(f"Processing message ID: {message.id}")
                
                raw_text = message.text or ""
                clean_msg = clean_text(raw_text)

                if is_channel_initial_run:
                    highest_id_processed = max(highest_id_processed, message.id)
                    if clean_msg and not is_ad(raw_text):
                        states["global_seen_texts"].append(clean_msg)
                    continue

                if is_ad(raw_text):
                    print("Ad detected, skipping.")
                    highest_id_processed = message.id
                    continue
                
                if clean_msg and is_too_similar(clean_msg, states["global_seen_texts"], threshold=0.70):
                    print("Similar content detected, skipping.")
                    highest_id_processed = message.id
                    continue

                target_spaces = [SPACE_NAME]
                if channel in EXTRA_GOOGLE_CHAT_SPACES:
                    extra_space = EXTRA_GOOGLE_CHAT_SPACES[channel]
                    if not extra_space.startswith('spaces/'):
                        extra_space = f"spaces/{extra_space}"
                    target_spaces.append(extra_space)

                file_path = None
                if message.media:
                    print("Downloading media via Telethon...")
                    file_path = await client.download_media(message)

                message_sent_successfully = False

                for space in target_spaces:
                    attachment_tokens = []
                    upload_errors = [] 
                    
                    if file_path:
                        filename = os.path.basename(file_path)
                        upload_token, upload_error = upload_media_to_chat(token, file_path, filename, space)
                        
                        if upload_token:
                            attachment_tokens.append(upload_token)
                        elif upload_error:
                            upload_errors.append(upload_error) 

                    if not clean_msg and not attachment_tokens and not upload_errors:
                        continue

                    formatted_text = f"*{channel_title}*\n\n{clean_msg}" if clean_msg else f"*{channel_title}*\n\n[ללא טקסט]"
                    
                    # הוספת טקסט השגיאה המדויק להודעה במקרה של כישלון העלאה
                    if upload_errors:
                        formatted_text += f"\n\n*(⚠️ הבוט לא הצליח להעלות קובץ מצורף להודעה זו. שגיאה: {upload_errors[0]})*"
                    
                    success = send_chat_message(token, formatted_text, attachment_tokens, space)
                    if success:
                        message_sent_successfully = True

                if file_path:
                    os.remove(file_path)
                    time.sleep(2) 

                if message_sent_successfully:
                    highest_id_processed = message.id
                    if clean_msg:
                        states["global_seen_texts"].append(clean_msg)

                time.sleep(1.5)

            states[channel] = highest_id_processed

        except Exception as e:
            print(f"Error processing channel {channel}: {e}")

        states["global_seen_texts"] = states["global_seen_texts"][-100:]
        with open(STATE_FILE, 'w') as f:
            json.dump(states, f)

    await client.disconnect()
    
    if is_global_initial_run:
        print("\n✅ ריצת האתחול הסתיימה בהצלחה! קו התחלה נשמר בזיכרון.")

if __name__ == "__main__":
    asyncio.run(main())
