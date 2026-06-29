import os
import sys
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Required environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS")

# Validate required configuration
missing_vars = []
if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.strip() == "your_telegram_bot_token_here":
    missing_vars.append("TELEGRAM_TOKEN")
if not GEMINI_API_KEY or GEMINI_API_KEY.strip() == "your_gemini_api_key_here":
    missing_vars.append("GEMINI_API_KEY")
if not ALLOWED_USERS_RAW or ALLOWED_USERS_RAW.strip() == "123456789":
    missing_vars.append("ALLOWED_USERS")

if missing_vars:
    print(f"Error: Missing or default environment variables configured: {', '.join(missing_vars)}")
    print("Please configure them in your .env file.")
    # We do not sys.exit(1) here immediately in case it's imported for tests, 
    # but we will raise when starting the bot.

# Parse allowed users into a set of integers
ALLOWED_USERS = set()
if ALLOWED_USERS_RAW:
    for user_id in ALLOWED_USERS_RAW.split(","):
        try:
            ALLOWED_USERS.add(int(user_id.strip()))
        except ValueError:
            print(f"Warning: Invalid user ID '{user_id}' in ALLOWED_USERS. Skipping.")

# Optional configurations
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
