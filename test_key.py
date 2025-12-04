# test_key.py
import requests, os
resp = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {k}"})
print(resp.status_code, resp.text[:800])
