import requests
import json

# 1. 测试新浪 K 线接口
try:
    url_sina = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=100"
    headers_sina = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://finance.sina.com.cn"
    }
    r = requests.get(url_sina, headers=headers_sina, timeout=5)
    print("Sina KLine status:", r.status_code, "len:", len(r.text))
    if r.status_code == 200:
        data = r.json()
        print("Sina KLine count:", len(data), "sample:", data[-1])
except Exception as e:
    print("Sina KLine error:", e)

# 2. 测试网易 163 K 线接口
try:
    url_163 = "http://img1.money.126.net/data/hs/kline/day/history/2026/0600519.json"
    r = requests.get(url_163, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
    print("163 KLine status:", r.status_code, "len:", len(r.text))
except Exception as e:
    print("163 KLine error:", e)

# 3. 测试腾讯手机端接口 (https://proxy.finance.qq.com 或 http://qt.gtimg.cn)
try:
    url_qq = "http://qt.gtimg.cn/q=s_sh600519"
    r = requests.get(url_qq, timeout=5)
    print("QQ Realtime status:", r.status_code, "text:", r.text[:50])
except Exception as e:
    print("QQ Realtime error:", e)
