import requests
import json

headers = {"User-Agent": "Mozilla/5.0"}

# 测试腾讯日K
url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,100,qfq"
r = requests.get(url, headers=headers, timeout=5)
print("Tencent KLine Status:", r.status_code)
try:
    data = r.json()
    klines = data["data"]["sh600519"].get("qfqday", data["data"]["sh600519"].get("day", []))
    print("Tencent KLine Count:", len(klines))
    print("Sample KLine:", klines[-1])
except Exception as e:
    print("Tencent KLine Error:", e)
