
import os
import time
import json
import asyncio
import random
import re
import difflib
import aiohttp
from telethon import TelegramClient
from telethon.sessions import StringSession
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

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

# ==========================================
# מזהי תיקיות בגוגל דרייב
# ==========================================
DRIVE_FOLDER_BACKUP = "15NzvBcWNwF5d8lv9DHlMldHW5RJOGTlr"
DRIVE_FOLDER_LARGE = "1AwO8vkFtvbNbagaTCZhHpKLl4yHebggz"

# ==========================================
# מנגנון מניעת עומסים מתוקן (קיצוב בסיום הפעולה)
# ==========================================
MIN_WRITE_INTERVAL = 1.2
_last_write_ts = 0.0

async def pace_write():
    global _last_write_ts
    now = time.time()
    wait_needed = MIN_WRITE_INTERVAL - (now - _last_write_ts)
    if wait_needed > 0:
        await asyncio.sleep(wait_needed)

def mark_write_done():
    global _last_write_ts
    _last_write_ts = time.time()

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
    text = re.sub(r'(?m)^.*?(?:וואטס?אפ|טלגרם).*?(?:https?://\S+|t\.me/\S+).*$\n?', '', text, flags=re.IGNORECASE)
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
    text = '\n'.join(valid_lines).strip()
    text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)
    return text

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
        scopes=[
            "https://www.googleapis.com/auth/chat.messages",
            "https://www.googleapis.com/auth/drive.file"
        ]
    )
    creds.refresh(Request())
    return creds

def sync_upload_to_drive(creds, file_path, filename, folder_id=None):
    drive_service = build('drive', 'v3', credentials=creds)
    file_metadata = {'name': filename}
    
    if folder_id:
        file_metadata['parents'] = [folder_id]
        
    media = MediaFileUpload(file_path, resumable=True)
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
    file_id = file.get('id')
    permission = {'type': 'anyone', 'role': 'reader'}
    drive_service.permissions().create(fileId=file_id, body=permission).execute()
    return file.get('webViewLink')

async def upload_to_drive_async(creds, file_path, filename, folder_id=None):
    return await asyncio.to_thread(sync_upload_to_drive, creds, file_path, filename, folder_id)

def sync_upload_media_to_chat(creds, space_name, file_path, filename):
    content_type = "application/octet-stream"
    if filename.endswith(".mp4"): content_type = "video/mp4"
    elif filename.endswith((".jpg", ".jpeg")): content_type = "image/jpeg"
    elif filename.endswith(".png"): content_type = "image/png"
    elif filename.endswith(".webp"): content_type = "image/webp"
    elif filename.endswith(".mp3"): content_type = "audio/mpeg"
    elif filename.endswith(".pdf"): content_type = "application/pdf"

    chat_service = build('chat', 'v1', credentials=creds)
    media = MediaFileUpload(file_path, mimetype=content_type)
    request = chat_service.media().upload(
        parent=space_name,
        body={'filename': filename},
        media_body=media
    )
    return request.execute()

async def upload_media_to_chat_async(creds, space_name, file_path, filename):
    await pace_write()
    try:
        token = await asyncio.to_thread(sync_upload_media_to_chat, creds, space_name, file_path, filename)
        mark_write_done()
        return token, None
    except Exception as e:
        mark_write_done()
        return None, str(e)

async def execute_request_with_official_backoff(session, method, url, headers, data=None, json_payload=None):
    # ללא ניסיונות חוזרים ארוכים כדי למנוע עיכובים בדיווחי חדשות
    max_attempts = 1  
    last_error = "שגיאה לא ידועה"
    
    await pace_write()
    status_code = 0
    error_text = ""
    is_success = False
    res_data = None
    
    try:
        async with session.request(method, url, headers=headers, data=data, json=json_payload, timeout=60) as res:
            status_code = res.status
            if status_code in (200, 201):
                res_data = await res.json()
                is_success = True
            else:
                error_text = await res.text()
                last_error = f"HTTP {status_code} - {error_text}"
    except Exception as e:
        last_error = f"שגיאת רשת/מערכת: {str(e)}"
    finally:
        mark_write_done()
        
    if is_success:
        return True, res_data
            
    return False, f"נכשל סופית. שגיאה: {last_error}"

async def send_chat_message(session, token, text, attachments):
    payload = {"text": text}
    if attachments:
        payload["attachment"] = attachments
        
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

    creds = get_user_credentials() if not is_global_initial_run else None
    token = creds.token if creds else None

    async with aiohttp.ClientSession() as aio_session:
        client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
        await client.connect()

        for channel in TARGET_CHANNELS:
            print(f"\\n--- Checking channel: {channel} ---")
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
                        highest_id_processed = max(highest_id_processed, message.id)
                        continue
                    
                    if clean_msg and is_too_similar(clean_msg, states["global_seen_texts"], threshold=0.70):
                        highest_id_processed = max(highest_id_processed, message.id)
                        continue

                    file_path = None
                    attachment_tokens = []
                    upload_errors = []
                    drive_link = None

                    if message.media:
                        file_size_mb = 0
                        if hasattr(message, 'file') and message.file and message.file.size:
                            file_size_mb = message.file.size / (1024 * 1024)
                            
                        print(f"Downloading media ({file_size_mb:.1f}MB)...")
                        try:
                            # פסק זמן אחיד של 15 דקות לכלל ההורדות מטלגרם
                            download_timeout = 900
                            file_path = await asyncio.wait_for(client.download_media(message), timeout=download_timeout)
                        except asyncio.TimeoutError:
                            print(" > שגיאה: הורדת הקובץ מטלגרם לקחה יותר מדי זמן ונקטעה.")
                            upload_errors.append("שגיאת רשת: זמן הורדת הקובץ מטלגרם חרג מ-15 דקות.")
                        except Exception as e:
                            print(f" > שגיאה בהורדת מדיה: {e}")
                            upload_errors.append(f"שגיאה בהורדת הקובץ מטלגרם: {e}")

                    if file_path:
                        filename = os.path.basename(file_path)
                        
                        # כל הקבצים עד 200MB עולים ישירות לצ'אט (המגבלה הרשמית המקסימלית של Google Chat)
                        if file_size_mb > 200:
                            print(" > גודל חורג מ-200MB (מקסימום של גוגל צ'אט), מגבה לתיקיית 'קבצים גדולים' בדרייב...")
                            try:
                                drive_link = await upload_to_drive_async(creds, file_path, filename, folder_id=DRIVE_FOLDER_LARGE)
                            except Exception as e:
                                upload_errors.append(f"העלאת קובץ גדול לדרייב נכשלה: {e}")
                        else:
                            uploaded_attachment, upload_error = await upload_media_to_chat_async(creds, SPACE_NAME, file_path, filename)
                            if uploaded_attachment:
                                attachment_tokens.append(uploaded_attachment)
                                print(" > קובץ מדיה הועלה בהצלחה (מצורף ישירות להודעה בצ'אט)!")
                            elif upload_error:
                                print(f" > שגיאה בהעלאה לצ'אט: {upload_error}. מגבה לדרייב...")
                                try:
                                    drive_link = await upload_to_drive_async(creds, file_path, filename, folder_id=DRIVE_FOLDER_BACKUP)
                                except Exception as e:
                                    upload_errors.append(f"כשל כפול (צ'אט + דרייב): {upload_error} | {e}")

                        if drive_link:
                            clean_msg += f"\n\n📹 *לצפייה בסרטון / קובץ בדרייב:*\n{drive_link}"
                        elif attachment_tokens:
                            # מרווח יזום של שניה אחת + מילי-שניות רנדומליות בין העלאת המדיה לשליחת ההודעה
                            await asyncio.sleep(1.0 + random.uniform(0.1, 0.3))

                    if not clean_msg and not attachment_tokens and not drive_link and not upload_errors:
                        highest_id_processed = max(highest_id_processed, message.id)
                        if file_path:
                            try: os.remove(file_path)
                            except: pass
                        continue

                    formatted_text = f"📢 *{channel_title}*\n\n{clean_msg}" if clean_msg else f"📢 *{channel_title}*\n\n_[הודעת מדיה ללא טקסט]_"
                    if upload_errors:
                        formatted_text += f"\n\n⚠️ _הערת מערכת: לא ניתן היה לצרף את הקובץ המקורי ({upload_errors[0]})_"
                    
                    success, send_error = await send_chat_message(aio_session, token, formatted_text, attachment_tokens)
                    if success:
                        highest_id_processed = max(highest_id_processed, message.id)
                        if clean_msg:
                            states["global_seen_texts"].append(clean_msg)
                        # מרווח יזום של שניה אחת + מילי-שניות רנדומליות בין הודעה להודעה הבאה למניעת עומס רגעי
                        await asyncio.sleep(1.0 + random.uniform(0.2, 0.5))
                    else:
                        print(f"Message failed: {send_error}. מפעיל גיבוי טקסט לתיקיית 'גיבוי' בדרייב...")
                        temp_txt_filename = f"Message_{channel_title}_{message.id}.txt"
                        try:
                            with open(temp_txt_filename, 'w', encoding='utf-8') as tf:
                                tf.write(formatted_text)
                            text_drive_link = await upload_to_drive_async(creds, temp_txt_filename, temp_txt_filename, folder_id=DRIVE_FOLDER_BACKUP)
                            print(f" > הודעת הטקסט הועלתה לדרייב בהצלחה: {text_drive_link}")
                            if clean_msg:
                                states["global_seen_texts"].append(clean_msg)
                        except Exception as e:
                            print(f" > שגיאה בהעלאת הודעת הטקסט לדרייב: {e}")
                        finally:
                            if os.path.exists(temp_txt_filename):
                                try: os.remove(temp_txt_filename)
                                except: pass
                            
                            highest_id_processed = max(highest_id_processed, message.id)

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
