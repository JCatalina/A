import os
import logging
from typing import Optional
from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from data_fetcher import DataFetcher
from scanner_engine import ScannerEngine
from index_engine import IndexEngine
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("A-Stock-Quant-Server")

app = FastAPI(
    title="A股高胜率技术指标与多维支撑压力位量化分析系统",
    version="2.1.0"
)

# 允许跨域 (allow_origins=["*"] 与 credentials 不兼容，本地单机部署关闭凭证)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

data_fetcher = DataFetcher()
scanner_engine = ScannerEngine(data_fetcher)
index_engine = IndexEngine()

@app.get("/api/index/analysis")
def get_index_macro_analysis(
    symbol: str = Query("sh000001", description="指数代码: sh000001, sz399001, sz399006, sh000688"),
    scale: str = Query("240", description="K线周期: 30, 60, 240, 1200")
):
    """获取大盘指数多周期深度研判数据（含30分/60分/日K/周K、支撑压力、核心结论）"""
    res = index_engine.analyze_index_macro(symbol, scale=scale)
    if not res:
        return JSONResponse(status_code=404, content={"status": "error", "message": f"未获取到指数 {symbol} 的多周期行情数据"})
    return {"status": "success", "data": res}

# 静态前端路径
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

@app.get("/api/market/indices")
def get_indices():
    """获取大盘指数数据"""
    indices = data_fetcher.get_market_indices()
    return {"status": "success", "data": indices}

@app.get("/api/stock/list")
def get_stocks(query: Optional[str] = Query(None, description="搜索关键词：代码/名称")):
    """获取或搜索股票列表"""
    stocks = data_fetcher.get_stock_list()
    if query:
        q = query.strip().upper()
        stocks = [
            s for s in stocks 
            if q in s["code"] or q in s["name"] or q in s["industry"]
        ]
    return {"status": "success", "total": len(stocks), "data": stocks[:60]}

@app.get("/api/stock/analysis")
def get_stock_analysis(
    code: str = Query(..., description="股票代码，如 600519 或 000001"),
    period: str = Query("daily", description="K线周期: daily 或 weekly")
):
    """获取指定股票的完整多维指标、支撑/压力带与量化交易计划"""
    result = scanner_engine.analyze_single_stock(code, period=period)
    if not result:
        return JSONResponse(status_code=404, content={"status": "error", "message": f"未找到股票 {code} 的行情数据或K线数据不足"})
    
    return {"status": "success", "data": result}

@app.post("/api/screener/run")
def trigger_screener(
    background_tasks: BackgroundTasks,
    strategy: str = Query("ALL", description="策略类别: ALL, SUPPORT_PULLBACK, BREAKOUT_PRESSURE, MAIN_WAVE_TREND, OVERSOLD_DIVERGENCE"),
    limit: int = Query(120, description="扫描股票数量")
):
    """启动后台全市场/活跃池批量扫描选股"""
    if scanner_engine.is_scanning:
        return {"status": "running", "message": "已有扫描任务正在执行中", "progress": scanner_engine.scan_progress}
    
    # 异步执行扫描
    background_tasks.add_task(scanner_engine.scan_market, strategy, limit)
    return {"status": "started", "message": f"已启动策略 {strategy} 的全市场扫描任务", "progress": 0}

@app.get("/api/screener/status")
def get_screener_status():
    """获取当前扫描进度状态"""
    return {
        "status": "success",
        "is_scanning": scanner_engine.is_scanning,
        "progress": scanner_engine.scan_progress
    }

@app.get("/api/screener/results")
def get_screener_results(strategy: str = Query("ALL")):
    """获取最新选股结果"""
    res = scanner_engine.last_results.get(strategy, [])
    # 检查 res 中的数据是否有有效中文名称，如果没有则重新生成
    if res and (not res[0].get("name") or res[0].get("name") == res[0].get("code") or str(res[0].get("name")).isdigit()):
        res = []
        scanner_engine.last_results = {}

    if not res and strategy != "ALL" and "ALL" in scanner_engine.last_results:
        # 从 ALL 中过滤
        all_res = scanner_engine.last_results["ALL"]
        res = [r for r in all_res if strategy in r.get("matched_strategies", [])]
    
    # 如果还没有扫描结果，自动分析核心标的池并按统一策略匹配条件过滤 (标记为演示数据)
    if not res:
        demo_codes = [
            "600519", "300750", "300308", "002594", "300033",
            "601127", "002475", "600900", "002460", "300274",
            "601899", "600028", "002230", "601138"
        ]
        demo_results = []
        for c in demo_codes:
            stk_res = scanner_engine.analyze_single_stock(c)
            if stk_res:
                # 与正式扫描完全一致的严格策略匹配，不放水
                stk_res["matched_strategies"] = ScannerEngine.match_strategies(stk_res)
                stk_res["is_demo"] = True  # 标记为演示数据(未执行全市场扫描)，前端需展示对应角标
                demo_results.append(stk_res)

        # 胜率从高到低排序
        demo_results.sort(key=lambda x: x["prediction"].get("bullish_probability", 0), reverse=True)
        scanner_engine.last_results["ALL"] = demo_results
        res = demo_results if strategy == "ALL" else [r for r in demo_results if strategy in r.get("matched_strategies", [])]

    return {
        "status": "success",
        "strategy": strategy,
        "total": len(res),
        "data": res
    }

# 挂载前端静态页面
if os.path.exists(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def serve_index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
