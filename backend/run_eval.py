"""
点时间评估 CLI

用法示例 (在 backend 目录下):
    python run_eval.py                       # 默认: 股票池前 40 只, 700 根日K, 步长 3
    python run_eval.py --stocks 80 --step 2  # 更大样本
    python run_eval.py --codes 600519,000001,300750 --workers 1
    python run_eval.py --quick               # 12 只 / 步长 5, 几分钟内出结果

输出: backend/eval_reports/eval_<时间>.md / .json / _pit.csv 以及 latest.md / latest.json
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_fetcher import DataFetcher          # noqa: E402
from index_engine import IndexEngine          # noqa: E402
from eval_engine import EvalEngine            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="A股量化流水线点时间评估")
    ap.add_argument("--stocks", type=int, default=40, help="从活跃股票池取前 N 只 (默认 40)")
    ap.add_argument("--codes", type=str, default="", help="逗号分隔的股票代码，指定后忽略 --stocks")
    ap.add_argument("--bars", type=int, default=700, help="每只股票拉取的日K根数 (默认 700 ≈ 2.8 年)")
    ap.add_argument("--warmup", type=int, default=250, help="预热根数，首个评估点之前的历史长度 (默认 250)")
    ap.add_argument("--step", type=int, default=3, help="评估步长(交易日)，越小样本越多越慢 (默认 3)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="评估进程数")
    ap.add_argument("--index", type=str, default="sh000001", help="超额收益基准指数")
    ap.add_argument("--out", type=str, default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_reports"))
    ap.add_argument("--quick", action="store_true", help="快速模式: 12 只 / 步长 5 / 600 根")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.quick:
        args.stocks, args.step, args.bars = min(args.stocks, 12), max(args.step, 5), min(args.bars, 600)

    fetcher = DataFetcher()
    idx_engine = IndexEngine()

    if args.codes.strip():
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        pool = fetcher.get_stock_list()
        codes = [s["code"] for s in pool][: args.stocks]
    print(f"[eval] universe={len(codes)} bars={args.bars} warmup={args.warmup} step={args.step} workers={args.workers}")

    t0 = time.time()

    def progress(msg: str) -> None:
        print(f"[eval {time.time() - t0:6.1f}s] {msg}", flush=True)

    engine = EvalEngine(fetcher, idx_engine, warmup=args.warmup, step=args.step,
                        bars=args.bars, index_symbol=args.index)
    result = engine.run(codes, workers=args.workers, progress=progress)
    paths = EvalEngine.save(result, args.out)

    m = result["metrics"]
    print("\n" + "=" * 72)
    if "error" in m:
        print("ERROR:", m["error"])
        return 1
    b = m["baseline"]["ret_10"]
    print(f"评估点 {m['meta']['pit_points']} (可成交 {m['meta']['pit_fillable']}), 条件样本 {m['meta']['cond_points']}, 耗时 {m['meta']['elapsed_sec']}s")
    print(f"基线 10日: 命中 {b['hit']}% CI{b['ci']} 均值 {b['mean']}%")
    for st, blk in m["by_signal"].items():
        r = blk["ret_10"]
        print(f"  {st:26s} n={r['n']:5d} 命中 {r['hit']}% CI{r['ci']} 均值 {r['mean']}%  lift {blk['lift_hit10_pp']}pp")
    ic = m["rank_ic"]
    print("Rank-IC(10日):", ", ".join(f"{k}={v.get('pooled_ic_ret10')}" for k, v in ic.items()))
    if m["significance_flags"]:
        print("\n显著性提示:")
        for s in m["significance_flags"]:
            print("  -", s)
    print("\n报告:", paths["md"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
