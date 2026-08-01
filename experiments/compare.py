"""跨實驗比較 k* 時，先檢查訓練分布是否一致。

存在的理由：天花板 k* 對訓練分布極度敏感 —— 同一個 loop1，在 k<=12 的分布下
量到 k*~7，在 k<=24 下只有 2.69。這個陷阱在 research.md §4.3 記錄過，
但仍然被重蹈了兩次。文件擋不住，所以改成執行期會擋。
"""
import sys, json, os, argparse

HERE = os.path.dirname(os.path.abspath(__file__))


def ceil50(per_k):
    prev = None
    for k in sorted(map(int, per_k)):
        a = per_k[str(k)]["acc"]
        if a < 0.5 and prev and prev[1] >= 0.5:
            k0, a0 = prev
            return k0 + (a0 - 0.5) / (a0 - a)
        prev = (k, a)
    return float("inf")


def load(spec):
    fn, cfg = spec.split(":")
    d = json.load(open(os.path.join(HERE, fn)))
    if cfg not in d:
        sys.exit(f"❌ {fn} 裡沒有 config '{cfg}'（有：{list(d)}）")
    return fn, cfg, d[cfg]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="+", help="檔名:config，例如 results_perm.json:loop2")
    ap.add_argument("--force", action="store_true", help="訓練分布不一致時仍然比較")
    a = ap.parse_args()

    rows = [load(s) for s in a.specs]
    dists = [r[2].get("train_dist") for r in rows]
    known = [d for d in dists if d]
    if len(known) < len(dists):
        print("⚠️  部分結果沒有 train_dist（舊格式），無法自動檢查一致性")
    elif len({(d["task"], d["max_k"], d["steps"]) for d in known}) > 1:
        print("❌ 訓練分布不一致 —— k* 不可跨這些結果比較：")
        for (fn, cfg, _), d in zip(rows, dists):
            print(f"     {fn}:{cfg}  task={d['task']} max_k={d['max_k']} steps={d['steps']}")
        if not a.force:
            sys.exit("   （確定要比就加 --force）")

    print(f"\n{'結果':34s} {'k*':>8s} {'整體':>8s}")
    print("-" * 54)
    for fn, cfg, r in rows:
        c = ceil50(r["per_k"])
        print(f"{fn+':'+cfg:34s} {(f'{c:8.2f}' if c != float('inf') else '     >max')} {r['overall']:8.1%}")


if __name__ == "__main__":
    main()
