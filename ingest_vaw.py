import os
import json
import time
import feedparser
import requests
from datetime import datetime, timezone
from google import genai
from google.genai import types

# --- CONFIGURATION ---
SUPABASE_URL = "https://jbqoqoufhzlxroslwdzs.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "GEMINI_API_KEY")

# --- WHATSAPP CLOUD API CONFIGURATION ---
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
WHATSAPP_TO_NUMBER = os.environ.get("WHATSAPP_TO_NUMBER", "")

RSS_FEEDS = [
    {"name": "Dawn News", "url": "https://www.dawn.com/feeds/home"},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "The Express Tribune", "url": "https://tribune.com.pk/feed/home"},
    {"name": "Geo News", "url": "https://www.geo.tv/rss/1/1"},
]

KEYWORDS = [
    "woman", "women", "girl", "wife", "bride", "daughter", "female",
    "domestic violence", "femicide", "honor killing", "honour killing",
    "acid attack", "harassment", "assault", "rape", "abduction", "dowry"
]

# Models with independent Free Tier daily quotas (excludes paid-only 'omni'/'pro'/'image')
FREE_TIER_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]

client = genai.Client(api_key=GEMINI_API_KEY)

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal"
}

def url_exists(source_url: str) -> bool:
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/incidents",
        headers=HEADERS,
        params={"source_url": f"eq.{source_url}", "select": "id"}
    )
    return res.status_code == 200 and len(res.json()) > 0

def send_whatsapp_digest(new_incidents: list):
    """Sends a WhatsApp Utility template alert (vaw_draft_alert) when new drafts are queued."""
    if not new_incidents:
        return
    if not (WHATSAPP_TOKEN and WHATSAPP_PHONE_ID and WHATSAPP_TO_NUMBER):
        print("  [WHATSAPP SKIPPED] Missing WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, or WHATSAPP_TO_NUMBER.")
        return

    api_url = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_ID}/messages"
    wa_headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    # Meta prohibits newlines or >4 consecutive spaces inside template parameters
    summary_items = []
    for idx, item in enumerate(new_incidents[:5], start=1):
        headline = (item.get("headline") or item.get("title") or "Untitled Incident").replace("\n", " ").strip()
        loc = (item.get("location") or item.get("city") or item.get("province") or "Pakistan").replace("\n", " ").strip()
        src = (item.get("source") or item.get("source_name") or "RSS").replace("\n", " ").strip()
        summary_items.append(f"{idx}. {headline[:60]} - {loc} ({src})")

    if len(new_incidents) > 5:
        summary_items.append(f"+{len(new_incidents) - 5} more")

    incidents_line = " | ".join(summary_items)
    supabase_url = "https://supabase.com/dashboard/project/jbqoqoufhzlxroslwdzs/editor"

    payload = {
        "messaging_product": "whatsapp",
        "to": WHATSAPP_TO_NUMBER,
        "type": "template",
        "template": {
            "name": "vaw_draft_alert",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(len(new_incidents))},
                        {"type": "text", "text": incidents_line[:900]},
                        {"type": "text", "text": supabase_url}
                    ]
                }
            ]
        }
    }

    try:
        resp = requests.post(api_url, headers=wa_headers, json=payload, timeout=20)
        if resp.status_code in (200, 201):
            print(f"  [WHATSAPP SENT] Utility alert delivered to {WHATSAPP_TO_NUMBER} ({len(new_incidents)} incident(s)).")
        else:
            print(f"  [WHATSAPP ERROR] {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"  [WHATSAPP EXCEPTION] {e}")
def extract_batch(candidates: list, pub_name: str):
    """Analyzes all candidate headlines from a feed in ONE single Gemini request."""
    items_text = "\n".join([
        f"[{i}] Headline: {c['title']} | Summary: {c['summary'][:220]} | Date: {c['pub_date']}"
        for i, c in enumerate(candidates)
    ])

    prompt = f"""
    Review the numbered news items below from {pub_name}.
    Identify ONLY items that report a specific, real-world incident of violence against women or girls (VAW), such as domestic abuse, femicide, honor killing, acid attack, sexual assault, abduction, stalking, or cyber harassment.
    Ignore political news, general crime unrelated to gender-based violence, animal attacks, accidents, opinion pieces, or policy debates.

    News Items:
    {items_text}

    Return a JSON object with a single key "incidents" containing an array of ONLY the matching VAW incidents (return {{"incidents": []}} if none qualify):
    {{
      "incidents": [
        {{
          "index": 0,
          "headline": "concise factual headline",
          "incident_type": "Femicide | Domestic Abuse | Physical Assault | Harassment | Cyber Violence",
          "incident_date": "YYYY-MM-DD",
          "city": "City name",
          "country": "Country name",
          "latitude": 0.0,
          "longitude": 0.0,
          "victim_count": 1,
          "perpetrator_count": 1,
          "severity_level": "moderate | high | extreme",
          "summary": "2-sentence objective summary"
        }}
      ]
    }}
    """

    last_error = None
    for model_name in FREE_TIER_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            parsed = json.loads(response.text)
            return parsed.get("incidents", []), model_name
        except Exception as e:
            last_error = e
            err = str(e)
            if "429" in err or "404" in err or "503" in err:
                continue
            raise e

    raise last_error

def run_pipeline():
    print("Starting batched VAW RSS ingestion pipeline...")
    inserted_items = []

    for feed_info in RSS_FEEDS:
        feed = feedparser.parse(feed_info["url"], agent="Mozilla/5.0")
        candidates = []

        for entry in feed.entries[:30]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")
            text_blob = f"{title} {summary}".lower()

            if not any(kw in text_blob for kw in KEYWORDS):
                continue
            if not link or url_exists(link):
                continue

            candidates.append({
                "title": title,
                "summary": summary,
                "link": link,
                "pub_date": entry.get("published", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            })

        print(f"Scanning {feed_info['name']}: {len(feed.entries)} entries -> {len(candidates)} keyword candidates")
        if not candidates:
            continue

        try:
            matches, used_model = extract_batch(candidates, feed_info["name"])
            print(f"  -> Processed via {used_model}: {len(matches)} VAW incident(s) found")

            for data in matches:
                idx = data.get("index", 0)
                if idx < 0 or idx >= len(candidates):
                    continue
                src = candidates[idx]

                payload = {
                    "headline": data["headline"],
                    "incident_type": data["incident_type"],
                    "incident_date": data["incident_date"],
                    "city": data["city"],
                    "country": data["country"],
                    "latitude": data["latitude"],
                    "longitude": data["longitude"],
                    "victim_count": data.get("victim_count", 1),
                    "perpetrator_count": data.get("perpetrator_count", 1),
                    "severity_level": data.get("severity_level", "high"),
                    "summary": data["summary"],
                    "publication_name": feed_info["name"],
                    "source_url": src["link"],
                    "verified": False,
                    "status": "draft"
                }

                res = requests.post(f"{SUPABASE_URL}/rest/v1/incidents", headers=HEADERS, json=payload)
                if res.status_code in (200, 201):
                    inserted_items.append(payload)
                    print(f"  [QUEUED] {data['city']}, {data['country']} — {data['headline']}")
                else:
                    print(f"  [DB ERROR] {res.status_code}: {res.text}")

            time.sleep(2)
        except Exception as e:
            print(f"  [FEED ERROR] {feed_info['name']}: {e}")

    print(f"Run complete. {len(inserted_items)} new incidents added to draft queue.")
    send_whatsapp_digest(inserted_items)

if __name__ == "__main__":
    run_pipeline()
