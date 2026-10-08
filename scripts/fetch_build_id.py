import re
import requests

r = requests.get("https://chatgpt.com/", timeout=15,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"})
print("status:", r.status_code)
text = r.text
m1 = re.search(r'buildId[^a-z]+([a-f0-9]{40})', text)
m2 = re.search(r'data-build[^>]*"([^"]+)"', text)
m3 = re.search(r'(prod-[a-f0-9]{40})', text)
print("buildId:", m1.group(1) if m1 else None)
print("data-build:", m2.group(1) if m2 else None)
print("prod-hash:", m3.group(1) if m3 else None)
# OAI client version header 线索
m4 = re.findall(r'(?:version|build)[\"\':\s=]+([0-9]{7,10})', text)
print("数字版本线索:", m4[:5])
