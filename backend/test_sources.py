import requests
import akshare as ak

print("Testing Akshare / Sina / Tencent / Eastmoney...")

headers = {"User-Agent": "Mozilla/5.0"}

# 1. 腾讯行情接口
try:
    r = requests.get("http://qt.gtimg.cn/q=sh600519", headers=headers, timeout=5)
    print("Tencent status:", r.status_code, "len:", len(r.text))
    print("Tencent sample:", r.text[:80])
except Exception as e:
    print("Tencent error:", e)

# 2. 新浪行情接口
try:
    r = requests.get("http://hq.sinajs.cn/list=sh600519", headers={"Referer": "https://finance.sina.com.cn", **headers}, timeout=5)
    print("Sina status:", r.status_code, "len:", len(r.text))
    print("Sina sample:", r.text[:80])
except Exception as e:
    print("Sina error:", e)

# 3. 东方财富
try:
    r = requests.get("http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500000&lmt=10", headers=headers, timeout=5)
    print("Eastmoney status:", r.status_code, "len:", len(r.text))
except Exception as e:
    print("Eastmoney error:", e)
