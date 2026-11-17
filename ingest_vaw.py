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
                # Immediately switch to next model's separate quota pool without burning retries
                continue
            raise e

    raise last_error

def run_pipeline():
    print("Starting batched VAW RSS ingestion pipeline...")
    inserted = 0

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
                    inserted += 1
                    print(f"  [QUEUED] {data['city']}, {data['country']} — {data['headline']}")
                else:
                    print(f"  [DB ERROR] {res.status_code}: {res.text}")

            time.sleep(2)
        except Exception as e:
            print(f"  [FEED ERROR] {feed_info['name']}: {e}")

    print(f"Run complete. {inserted} new incidents added to draft queue.")

if __name__ == "__main__":
    run_pipeline()