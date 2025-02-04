import os
import time
import logging
import sqlite3
import requests
import concurrent.futures
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

# Telegram config from env vars
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# how long to wait between cycles
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "0"))

USERS_FILE = os.getenv("USERS_FILE", "users.txt")
NUM_THREADS = int(os.getenv("NUM_THREADS", "5"))

# db (for deduplication); should be mounted externally for persistence
DB_FILENAME = os.getenv("DB_FILENAME", "alerted_posts.db")

# only consider tweets within specified days (so we're not inundated when first running bot)
TWEET_TIME_THRESHOLD = timedelta(days=7)

CRYPTO_KEYWORDS = [
    "crypto", "coin", "token", 
    "safemoon", "$", "moonshot"
]

# --- db functs ---
def init_db():
    conn = sqlite3.connect(DB_FILENAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT NOT NULL,
            link TEXT NOT NULL,
            alerted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(handle, link)
        )
    """)
    conn.commit()
    conn.close()

def already_alerted(handle, link):
    """Return True if the post (handle and link) is already in the database."""
    conn = sqlite3.connect(DB_FILENAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM posts WHERE handle=? AND link=?", (handle, link))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_alerted(handle, link):
    conn = sqlite3.connect(DB_FILENAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO posts (handle, link) VALUES (?, ?)", (handle, link))
        conn.commit()
    except sqlite3.IntegrityError:
        # Duplicate entry, do nothing
        pass
    finally:
        conn.close()

# --- telegram alert functs ---
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Telegram bot token or chat ID not set in .env")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            logging.error(f"Failed to send Telegram alert: {r.text}")
        else:
            logging.info("Telegram alert sent successfully.")
    except Exception as e:
        logging.error(f"Exception sending Telegram alert: {e}")

# --- users file management ---
def load_handles(filename):
    handles = []
    try:
        with open(filename, "r") as f:
            for line in f:
                handle = line.strip()
                if handle:
                    handles.append(handle)
    except Exception as e:
        logging.error(f"Error reading users file '{filename}': {e}")
    return handles

# --- scraping functs ---
def process_handle(handle):
    """
    process single handle -> launch playwright
     -> scrape tweets/followers -> send message
    """
    try:
        logging.info(f"Processing account: @{handle}")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            profile_url = f"https://x.com/{handle}"
            page.goto(profile_url, timeout=60000)
            time.sleep(5)  # wait for page to load

            # scrap tweets
            tweets_data = []
            articles = page.query_selector_all("article")
            for article in articles:
                try:
                    time_elem = article.query_selector("time")
                    if time_elem:
                        tweet_time_str = time_elem.get_attribute("datetime")
                        tweet_time = datetime.fromisoformat(tweet_time_str.replace("Z", "+00:00"))
                    else:
                        tweet_time = None

                    if tweet_time is None or (datetime.now(tweet_time.tzinfo) - tweet_time) > TWEET_TIME_THRESHOLD:
                        continue

                    tweet_text = article.inner_text()
                    link_elem = article.query_selector("a[href*='/status/']")
                    tweet_link = link_elem.get_attribute("href") if link_elem else profile_url
                    if tweet_link.startswith("/"):
                        tweet_link = "https://x.com" + tweet_link

                    tweets_data.append({
                        "time": tweet_time,
                        "text": tweet_text,
                        "link": tweet_link
                    })
                except Exception as e:
                    logging.error(f"Error extracting tweet data for @{handle}: {e}")

            # scrape follower count
            page.goto(profile_url, timeout=60000)
            time.sleep(5)
            follower_elem = page.query_selector("a:has-text('Followers')")
            if follower_elem:
                follower_count = follower_elem.inner_text().strip()
            else:
                follower_count = "N/A"

            context.close()
            browser.close()

            # process tweets; check against db and send alert if tweet is novel
            for tweet in tweets_data:
                if already_alerted(handle, tweet["link"]):
                    logging.info(f"Skipping already alerted tweet for @{handle}: {tweet['link']}")
                    continue
                if any(keyword in tweet["text"].lower() for keyword in CRYPTO_KEYWORDS):
                    message = (
                        f"*User:* @{handle}\n"
                        f"*Followers:* {follower_count}\n\n"
                        f"*Tweet:*\n{tweet['text']}\n\n"
                        f"[View Tweet]({tweet['link']})"
                    )
                    send_telegram_alert(message)
                    add_alerted(handle, tweet["link"])
    except Exception as e:
        logging.error(f"Error processing @{handle}: {e}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    init_db()
    handles = load_handles(USERS_FILE)
    if not handles:
        logging.error("No handles loaded. Exiting.")
        return

    logging.info(f"Loaded {len(handles)} handles from {USERS_FILE}")
    while True:
        current_handles = load_handles(USERS_FILE)
        logging.info(f"Starting processing cycle for {len(current_handles)} handles with {NUM_THREADS} processes.")
        with concurrent.futures.ProcessPoolExecutor(max_workers=NUM_THREADS) as executor:
            futures = [executor.submit(process_handle, handle) for handle in current_handles]
            concurrent.futures.wait(futures)
        logging.info(f"Cycle complete. Sleeping for {CHECK_INTERVAL_SECONDS} seconds...")
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
