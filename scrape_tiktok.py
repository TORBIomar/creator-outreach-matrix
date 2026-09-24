import os
import json
import requests

# n8n webhook endpoint (configurable via environment variable)
WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook-test/tiktok-leads")

mock_payload = {
    "leads": [
        {
            "username": "fashion_mia",
            "profile_url": "https://www.tiktok.com/@fashion_mia",
            "bio": "Minimal everyday style ✨\nJewelry & PR: collabs.mia@fashionagency.com\nBased in NYC 📍",
        },
        {
            "username": "daily_vibe_check",
            "profile_url": "https://www.tiktok.com/@daily_vibe_check",
            "bio": "Just daily vlogs and thrifting ✌️ No DMs, tap the link below!",
        },
        {
            "username": "luxe_aesthetics",
            "profile_url": "https://www.tiktok.com/@luxe_aesthetics",
            "bio": "Gold layers & accessories 💍\nBusiness inquiries: partnerships@luxeaesthetic.io",
        },
    ]
}


def send_test_leads():
    print(f"Sending {len(mock_payload['leads'])} test leads to n8n...")
    try:
        response = requests.post(
            WEBHOOK_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(mock_payload),
            timeout=10,
        )
        if response.status_code == 200:
            print("Status 200: Test payload successfully received by n8n!")
            print(f"Response: {response.text}")
        else:
            print(f"Failed with status code {response.status_code}: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"Connection error: {e}")


if __name__ == "__main__":
    send_test_leads()
