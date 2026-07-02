import os
import json
import requests
import feedparser
from bs4 import BeautifulSoup
import google.auth
from google.auth.transport.requests import Request

SPACE_NAME = os.environ.get('CHAT_SPACE')
RSS_URLS_ENV = os.environ.get('RSS_URL', '')
RSS_URLS = [url.strip() for url in RSS_URLS_ENV.split(',')] if RSS_URLS_ENV else []
STATE_FILE = 'last_ids.json'

def get_auth_token():
    print("Authenticating with Google Chat...")
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/chat.bot'])
    credentials.refresh(Request())
    return credentials.token

def upload_media_to_chat(token, media_url, filename):
    try:
        print(f"Downloading media: {media_url}")
        media_response = requests.get(media_url, timeout=30)
        media_response.raise_for_status()
        media_data = media_response.content
        
        upload_url = f"https://chat.googleapis.com/upload/v1/{SPACE_NAME}/attachments:upload?filename={filename}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream"
        }
        res = requests.post(upload_url, headers=headers, data=media_data)
        res.raise_for_status()
        
        return res.json().get('attachmentDataRef', {}).get('resourceName')
    except Exception as e:
        print(f"Error uploading media {filename}: {e}")
        return None

def main():
    print(f"Starting bot... Found {len(RSS_URLS)} URLs to process.")
    if not RSS_URLS:
        print("No RSS URLs found. Exiting.")
        return

    states = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                states = json.load(f)
                print(f"Loaded previous state: {states}")
            except Exception:
                print("Could not read state file, starting fresh.")
    else:
        print("No state file found. This is a fresh run.")
            
    token = None
    
    for rss_url in RSS_URLS:
        if not rss_url:
            continue
            
        print(f"\n--- Checking feed: {rss_url} ---")
        feed = feedparser.parse(rss_url)
        print(f"Feed entries found: {len(feed.entries)}")
        
        last_id = states.get(rss_url, "")
        print(f"Last processed ID for this feed: {last_id}")
        
        new_items = []
        for entry in feed.entries:
            entry_id = getattr(entry, 'id', getattr(entry, 'link', ''))
            if entry_id == last_id:
                break
            new_items.append(entry)
            
        print(f"Found {len(new_items)} new items.")
        
        # מניעת הצפה בריצה הראשונה
        if not last_id and len(new_items) > 2:
            print("First run detected. Limiting to 2 items to prevent spam.")
            new_items = new_items[:2]

        if not new_items:
            print("No new items to post for this feed.")
            continue
            
        new_items.reverse()
        
        if not token:
            token = get_auth_token()
        
        for item in new_items:
            text = getattr(item, 'title', '')
            link = getattr(item, 'link', '')
            print(f"Attempting to post item: {text[:30]}...")
            
            media_url = ""
            filename = "attachment.jpg" 
            
            if hasattr(item, 'enclosures') and item.enclosures:
                enc = item.enclosures[0]
                media_url = enc.get('url', '')
                enc_type = enc.get('type', '')
                if 'video' in enc_type or media_url.endswith('.mp4'):
                    filename = "video_content.mp4"
                else:
                    filename = "image_content.jpg"
                    
            if not media_url and hasattr(item, 'description'):
                soup = BeautifulSoup(item.description, 'html.parser')
                video_tag = soup.find('video')
                if video_tag and video_tag.get('src'):
                    media_url = video_tag['src']
                    filename = "video_content.mp4"
                else:
                    img_tag = soup.find('img')
                    if img_tag and img_tag.get('src'):
                        media_url = img_tag['src']
                        filename = "image_content.jpg"
            
            message_payload = {
                "text": f"{text}\n\n🔗 מקור: {link}"
            }
            
            if media_url:
                attachment_id = upload_media_to_chat(token, media_url, filename)
                if attachment_id:
                    message_payload["attachment"] = [{"attachmentDataRef": {"resourceName": attachment_id}}]
                    
            msg_url = f"https://chat.googleapis.com/v1/{SPACE_NAME}/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            res = requests.post(msg_url, headers=headers, json=message_payload)
            if res.status_code == 200:
                print("Message sent to Google Chat successfully!")
            else:
                print(f"FAILED to send message to Google Chat: {res.text}")
                
        states[rss_url] = getattr(new_items[-1], 'id', getattr(new_items[-1], 'link', ''))
        
    with open(STATE_FILE, 'w') as f:
        json.dump(states, f)
        print("\nSaved new state to file.")

if __name__ == "__main__":
    main()
