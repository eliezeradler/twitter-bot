import os
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
        res_media = requests.get(media_url, timeout=30)
        res_media.raise_for_status()
        
        # זיהוי אוטומטי של סוג התוכן כדי שגוגל יציג תצוגה מקדימה של התמונות והסרטונים
        content_type = "image/jpeg"
        if filename.endswith(".mp4"):
            content_type = "video/mp4"
        elif filename.endswith(".png"):
            content_type = "image/png"
        
        upload_url = f"https://chat.googleapis.com/upload/v1/{SPACE_NAME}/attachments:upload?filename={filename}&uploadType=media"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type
        }
        
        print(f"Uploading file as {content_type} to Google Chat servers...")
        res = requests.post(upload_url, headers=headers, data=res_media.content)
        
        if res.status_code != 200:
            print(f"Upload failed: {res.text}")
            return None
            
        data = res.json()
        return data.get('attachmentDataRef', {}).get('attachmentUploadToken')
        
    except Exception as e:
        print(f"Error uploading: {e}")
        return None

def main():
    if not RSS_URLS: return
    states = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try: states = json.load(f)
            except: pass
states = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try: states = json.load(f)
            except: pass

    space_mapping = {}
    if os.path.exists('spaces.json'):
        with open('spaces.json', 'r', encoding='utf-8') as f:
            try:
                space_mapping = json.load(f)
                print(f"Successfully loaded {len(space_mapping)} spaces from JSON.")
            except Exception as e:
                print(f"ERROR reading spaces.json: {e}")
    else:
        print("WARNING: spaces.json file not found in the directory!")

    token = None
    for rss_url in RSS_URLS:
        if not rss_url: continue
        print(f"\nChecking feed: {rss_url}")
        feed = feedparser.parse(rss_url)
        feed_title = getattr(feed.feed, 'title', 'מקור לא ידוע')
        last_id = states.get(rss_url, "")
        
        new_items = []
        for entry in feed.entries:
            entry_id = getattr(entry, 'id', getattr(entry, 'link', ''))
            if entry_id == last_id: break
            new_items.append(entry)
            
        if not last_id and len(new_items) > 2: new_items = new_items[:2]
        if not new_items: continue
        new_items.reverse()
        
        if not token: token = get_user_credentials()
        
        for item in new_items:
            # משיכת הכותרת והתוכן המלא
            raw_title = getattr(item, 'title', '')
            raw_desc = getattr(item, 'description', '')
            
            if raw_desc:
                # ניקוי קוד HTML מתוך התוכן המלא באמצעות BeautifulSoup
                soup = BeautifulSoup(raw_desc, 'html.parser')
                # הפיכת תגיות שבירת שורה לירידות שורה רגילות בטקסט
                for br in soup.find_all("br"):
                    br.replace_with("\n")
                
                # חילוץ הטקסט הנקי
                text = soup.get_text().strip()
                
                # לפעמים RSSHub שם רק תמונה בתוכן והטקסט נשאר בכותרת, אז נוודא שלא קיבלנו טקסט ריק
                if not text:
                    text = raw_title.strip()
            else:
                text = raw_title.strip()
            link = getattr(item, 'link', '')
            media_url = ""
            filename = "attachment.jpg"
            
            # לוגיקת זיהוי מדיה
            if hasattr(item, 'enclosures') and item.enclosures:
                enc = item.enclosures[0]
                media_url = enc.get('href', enc.get('url', ''))
                if 'video' in enc.get('type', '') or media_url.endswith('.mp4'):
                    filename = "video.mp4"
                    
            if not media_url and hasattr(item, 'media_content') and item.media_content:
                media_url = item.media_content[0].get('url', '')

            if not media_url:
                html_content = getattr(item, 'content', [{'value': ''}])[0].get('value', '') if hasattr(item, 'content') else getattr(item, 'description', '')
                if html_content:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    vid = soup.find('video')
                    if vid and vid.get('src'):
                        media_url = vid['src']
                        filename = "video.mp4"
                    else:
                        img = soup.find('img')
                        if img and img.get('src'): media_url = img['src']

            # ניקוי שם המקור מהמילים המבוקשות וסימני פיסוק מיותרים
            clean_title = feed_title.replace("Telegram Channel", "").replace("חדשות ללא צנזורה", "").replace("-", "").strip()
            clean_title = clean_title.strip("•").strip()
            
            # בניית ההודעה ללא הקישור בסוף
            payload = {"text": f"*{clean_title}*\n\n{text}"}
            
            if media_url:
                attachment_token = upload_media_to_chat(token, media_url, filename)
                if attachment_token:
                    print("Attaching file using upload token...")
                    payload["attachment"] = [{"attachmentDataRef": {"attachmentUploadToken": attachment_token}}]
            
            # ניתוב לחדר הספציפי או לחדר ברירת המחדל
            current_space = space_mapping.get(rss_url, SPACE_NAME)
            msg_url = f"https://chat.googleapis.com/v1/{current_space}/messages"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            
            res = requests.post(msg_url, headers=headers, json=payload)
            if res.status_code == 200:
                print("Message sent successfully!")
            else:
                print(f"Error posting: {res.text}")
                
        states[rss_url] = getattr(new_items[-1], 'id', getattr(new_items[-1], 'link', ''))
        
    with open(STATE_FILE, 'w') as f:
        json.dump(states, f)

if __name__ == "__main__":
    main()
