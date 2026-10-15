"""
横截面因子模型: 面板构建 + 走前评估 CLI

用法 (backend 目录下):
    python run_factor_eval.py                 # 活跃池前 800 只 × 2000 根, 重建面板并评估 (~1~2 分钟)
    python run_factor_eval.py --reuse-panel   # 复用 cache/factor_panel.npz, 只重跑评估 (秒级)
    python run_factor_eval.py --stocks 400 --bars 1500

输出: backend/eval_reports/factor_model_latest.json (服务端 /api/factor/latest 直接读取)
"""
import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_fetcher import DataFetcher            # noqa: E402
import factor_engine as fe                      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="A股横截面因子模型走前评估")
    ap.add_argument("--stocks", type=int, default=800, help="按当前成交额取前 N 只 (默认 800)")
    ap.add_argument("--bars", type=int, default=fe.PANEL_BARS, help="每只股票日K根数 (默认 2000)")
    ap.add_argument("--workers", type=int, default=4, help="抓取线程数 (过高会触发腾讯反爬)")
    ap.add_argument("--reuse-panel", action="store_true", help="复用磁盘面板, 只重跑评估")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    t0 = time.time()

    def progress(msg: str) -> None:
        print(f"[factor {time.time() - t0:6.1f}s] {msg}", flush=True)

    engine = fe.FactorEngine(DataFetcher(), universe_size=args.stocks, bars=args.bars)
    if args.reuse_panel and engine.panel is not None:
        progress(f"reusing panel: {len(engine.panel.codes)} codes × {len(engine.panel.dates)} dates")
        report = fe.walk_forward_evaluate(engine.panel)
        os.makedirs(fe.REPORT_DIR, exist_ok=True)
        with open(fe.REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        engine.report = report
        engine._refresh_scores()
    else:
        report = engine.rebuild(workers=args.workers, progress=progress)

    ch = report["champion_icir_weighted"]
    ic = ch["rank_ic_exec"]
    q = ch["quintiles"]
    print("\n" + "=" * 72)
    print(f"状态: {report['status']}  |  {report['interpretation']}")
    print(f"面板: {report['panel']['n_codes']} 只 × {report['panel']['n_dates']} 日, 样本外 {ic['n_days']} 日 {ic['date_range']}")
    print(f"冠军(ICIR加权) 样本外 Rank-IC 均值 {ic['mean']}  NW-t {ic['nw_t']}  ICIR {ic['icir']}  IC>0 {ic['pct_positive']}%")
    print(f"  分年 IC: " + ", ".join(f"{y}={v['mean']:+.3f}" for y, v in ic["by_year"].items()))
    print(f"  五分位均值收益%: {q['mean_ret_10d_pct_by_quintile']}  Q5-Q1 {q['q5_minus_q1_pct']}% (NW-t {q['q5_minus_q1_nw_t']})")
    print(f"  五分位中位收益%: {q['median_ret_10d_pct_by_quintile']}  Q5-Q1 {q['q5_minus_q1_median_pct']}% (NW-t {q['q5_minus_q1_median_nw_t']})")
    print(f"  跑赢截面中位数概率%: {q['beat_median_prob_pct_by_quintile']}  Q5-Q1 {q['q5_minus_q1_beat_median_pp']}pp (NW-t {q['q5_minus_q1_beat_median_nw_t']})")
    print("挑战者 Rank-IC:", {k: v["rank_ic_exec"].get("mean") for k, v in report["challengers"].items()})
    print("单因子样本外 IC / NW-t / 最新权重:")
    for k, v in report["single_factors_oos"].items():
        print(f"  {v['label']:<18s} {v['mean']:+.4f}  t={v['nw_t']:+5.2f}  w={v['latest_weight_icir']}")
    print("注意事项:")
    for c in report["caveats"]:
        print("  -", c)
    print("\n报告:", fe.REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
