import re
import os
import time
import json
import feedparser
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import requests

SPACE_NAME = os.environ.get('CHAT_SPACE')
RSS_URLS_ENV = os.environ.get('RSS_URL', '')
# מחליף ירידות שורה בפסיקים כדי שהקוד יתמוך גם ברשימה
RSS_URLS_ENV = RSS_URLS_ENV.replace('\n', ',').replace('\r', ',')
RSS_URLS = [url.strip() for url in RSS_URLS_ENV.split(',') if url.strip()]
STATE_FILE = 'last_ids.json'

CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('GOOGLE_REFRESH_TOKEN')

def get_user_credentials():
    print("Authenticating as USER via OAuth...")
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

def upload_media_to_chat(token, media_url, filename):
    try:
        print(f"Downloading media: {media_url}")
        res_media = requests.get(media_url, timeout=60)
        res_media.raise_for_status()
        
        # זיהוי אוטומטי של סוג התוכן כדי שגוגל ידע איך להציג אותו
        content_type = "application/octet-stream" # ברירת מחדל לקבצים כלליים
        if filename.endswith(".mp4"):
            content_type = "video/mp4"
        elif filename.endswith(".jpg") or filename.endswith(".jpeg"):
            content_type = "image/jpeg"
        elif filename.endswith(".png"):
            content_type = "image/png"
        elif filename.endswith(".webp"):
            content_type = "image/webp"
        elif filename.endswith(".mp3"):
            content_type = "audio/mpeg"
        elif filename.endswith(".pdf"):
            content_type = "application/pdf"
        
        upload_url = f"https://chat.googleapis.com/upload/v1/{SPACE_NAME}/attachments:upload?filename={filename}&uploadType=media"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type
        }
        
        print(f"Uploading file as {content_type} to Google Chat servers...")
        res = requests.post(upload_url, headers=headers, data=res_media.content, timeout=60)
        
        if res.status_code != 200:
            print(f"Upload failed: {res.text}")
            return None
            
        data = res.json()
        return data.get('attachmentDataRef', {}).get('attachmentUploadToken')
        
    except Exception as e:
        print(f"Error uploading: {e}")
        return None
# רשימה מעודכנת הממוקדת בסממנים שיווקיים ומסחריים
AD_WORDS = [
    "לפרטים נוספים לחצו",
    "לרכישה",
    "להזמנות",
    "מכירת",
    "לשליחת קורות חיים",
    "לפרטים והרשמה",
    "הלינק",
    "השאירו פרטים",
    "מספר המקומות מוגבל",
    "השאירו פרטים",
    "אסור לכם לפספס",
    "לחצו כאן ",
    "לפרטים נוספים",
    "יפה תורה עם דרך ארץ",
    "לפרטים מלאים",
    "לרכישת כרטיסים",
    "utm_source=",   # מזהה קישורי פרסומות קלאסי
    "utm_campaign=", # מזהה קישורי פרסומות קלאסי
    "ללא עלות וללא התחייבות"
]

def is_ad(text):
    """בודק אם הטקסט מכיל סממנים מובהקים של פרסומת"""
    if not text:
        return False
    for word in AD_WORDS:
        if word in text:
            return True
    return False

def clean_text(text):
    """מנקה קישורים מהטקסט"""
    if not text:
        return ""
    # הסרת קישורי טלגרם
    cleaned = re.sub(r'(https?://)?t\.me/[^\s]+', '', text)
    # הסרת קישורי אינטרנט רגילים
    cleaned = re.sub(r'https?://[^\s]+', '', cleaned)
    # ניקוי רווחים מיותרים שנוצרו אחרי המחיקה
    return cleaned.strip()

def main():
    if not RSS_URLS: return
    states = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try: states = json.load(f)
            except: pass

    # הבטחת קיום רשימת כותרות גלובלית למניעת כפילויות ממקורות שונים
    if "global_seen_titles" not in states:
        states["global_seen_titles"] = []

    token = None
    for rss_url in RSS_URLS:
        if not rss_url: continue
        print(f"\nChecking feed: {rss_url}")
        feed = feedparser.parse(rss_url)
        feed_title = getattr(feed.feed, 'title', 'מקור לא ידוע')
        
        # הפיכת הזיכרון הישן (טקסט) לרשימה חכמה
        last_ids = states.get(rss_url, [])
        if isinstance(last_ids, str): 
            last_ids = [last_ids]
        
        new_items = []
        for entry in feed.entries:
            post_text = entry.get('summary', entry.get('title', ''))

            if is_ad(post_text):
                print("Ad detected, skipping message.")
                continue

            entry_id = getattr(entry, 'id', getattr(entry, 'link', ''))
            
            # 1. מניעת כפילויות מאותו מקור
            if entry_id in last_ids:
                break

            # 2. מניעת כפילויות ממקורות שונים
            item_title = getattr(entry, 'title', '').strip()
            if item_title and item_title in states["global_seen_titles"]:
                print("Duplicate content from another source, skipping.")
                continue

            new_items.append(entry)
            
        if not last_ids and len(new_items) > 2: new_items = new_items[:2]
        if not new_items: continue
        
        new_items.reverse()
        if not token: token = get_user_credentials()
        
        for item in new_items:
            raw_title = getattr(item, 'title', '')
            raw_desc = getattr(item, 'description', '')
            
            if raw_desc:
                soup = BeautifulSoup(raw_desc, 'html.parser')
                for br in soup.find_all("br"):
                    br.replace_with("\n")
                text = soup.get_text().strip()
                if not text:
                    text = raw_title.strip()
            else:
                text = raw_title.strip()
            
            text = clean_text(text)
            
            link = getattr(item, 'link', '')
            attachment_tokens = []
            
            # 1. חיפוש כל הקבצים המצורפים להודעה (עובר על כל הרשימה)
            if hasattr(item, 'enclosures') and item.enclosures:
                for enc in item.enclosures:
                    media_url = enc.get('href', enc.get('url', ''))
                    if not media_url:
                        continue
                        
                    enc_type = enc.get('type', '')
                    filename = "file.dat"
                    
                    # קביעת שם וסיומת הקובץ לפי הסוג
                    if 'video' in enc_type or media_url.endswith('.mp4'):
                        filename = "video.mp4"
                    elif 'image' in enc_type or media_url.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        filename = "image.jpg"
                        if media_url.endswith('.png'): filename = "image.png"
                    elif 'audio' in enc_type or media_url.endswith(('.mp3', '.ogg', '.wav')):
                        filename = "audio.mp3"
                    elif 'pdf' in enc_type or media_url.endswith('.pdf'):
                        filename = "document.pdf"
                        
                    token_val = upload_media_to_chat(token, media_url, filename)
                    if token_val:
                        attachment_tokens.append(token_val)

            # 2. גיבוי: אם אין enclosures, נסרוק את התוכן וננסה לשלוף את כל התמונות והסרטונים
            if not attachment_tokens:
                html_content = getattr(item, 'content', [{'value': ''}])[0].get('value', '') if hasattr(item, 'content') else getattr(item, 'description', '')
                if html_content:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    for vid in soup.find_all('video'):
                        if vid.get('src'):
                            token_val = upload_media_to_chat(token, vid['src'], "video.mp4")
                            if token_val: attachment_tokens.append(token_val)
                    for img in soup.find_all('img'):
                        if img.get('src'):
                            token_val = upload_media_to_chat(token, img['src'], "image.jpg")
                            if token_val: attachment_tokens.append(token_val)

            clean_title = feed_title.replace("Telegram Channel", "").replace("חדשות ללא צנזורה", "").replace("-", "").strip()
            clean_title = clean_title.strip("•").strip()
            
            payload = {"text": f"*{clean_title}*\n\n{text}"}
            
            # צירוף של כל הקבצים שהעלינו לתוך ההודעה
            if attachment_tokens:
                print(f"Attaching {len(attachment_tokens)} files to message...")
                payload["attachment"] = [{"attachmentDataRef": {"attachmentUploadToken": t}} for t in attachment_tokens]
            
            msg_url = f"https://chat.googleapis.com/v1/{SPACE_NAME}/messages"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            
            res = requests.post(msg_url, headers=headers, json=payload)
            if res.status_code == 200:
                print("Message sent successfully!")
                
                # עדכון הזיכרון
                entry_id = getattr(item, 'id', getattr(item, 'link', ''))
                item_title = getattr(item, 'title', '').strip()
                
                if entry_id not in last_ids:
                    last_ids.append(entry_id)
                if item_title and item_title not in states["global_seen_titles"]:
                    states["global_seen_titles"].append(item_title)
                    
                # שמירת גיבוי מיידית לקובץ
                states[rss_url] = last_ids[-50:]
                states["global_seen_titles"] = states["global_seen_titles"][-100:]
                try:
                    with open(STATE_FILE, 'w') as f:
                        json.dump(states, f)
                except Exception as e:
                    print(f"Error saving backup: {e}")
            else:
                print(f"Error posting: {res.text}")
                
            # השהיית זמן של 3 שניות לפני מעבר להודעה הבאה כדי למנוע חסימה (429) מגוגל
            time.sleep(3)
                
        # שמירת 50 המזהים האחרונים לכל ערוץ
        states[rss_url] = last_ids[-50:]
        
    # שמירת 100 הכותרות האחרונות גלובלית
    states["global_seen_titles"] = states["global_seen_titles"][-100:]
        
    with open(STATE_FILE, 'w') as f:
        json.dump(states, f)

if __name__ == "__main__":
    main()
