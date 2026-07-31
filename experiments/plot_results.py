"""把 run_ablation.py 的 results.json 畫成收斂曲線圖 + 輸出比較表。"""
import os
import sys
import json
import math

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results.json")
OUT_SVG = os.path.join(HERE, "convergence.svg")

ORDER = ["vanilla", "engram", "complete_att", "engram+ca"]
LABEL = {
    "vanilla": "Vanilla",
    "engram": "Engram",
    "complete_att": "Complete Attention",
    "engram+ca": "Engram + Complete Attn",
}
# 色盲友善的四色
COLOR = {
    "vanilla": "#6b7280",
    "engram": "#2563eb",
    "complete_att": "#d97706",
    "engram+ca": "#059669",
}


def main():
    results = json.load(open(RESULTS))
    present = [n for n in ORDER if n in results]

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(17, 5))

    ax = axes[0]
    for n in present:
        h = results[n]["train_hist"]
        ax.plot(h["step"], h["train_loss"], color=COLOR[n], label=LABEL[n], lw=1.4)
    ax.set_xlabel("step"); ax.set_ylabel("train loss")
    ax.set_title("Training loss")
    ax.grid(alpha=.25); ax.legend(fontsize=9)

    ax = axes[1]
    for n in present:
        v = results[n]["val_hist"]
        ax.plot(v["step"], v["val_loss"], color=COLOR[n], label=LABEL[n], lw=1.8, marker="o", ms=3)
    ax.set_xlabel("step"); ax.set_ylabel("held-out val loss")
    ax.set_title("Validation loss (2000 unseen sequences)")
    ax.grid(alpha=.25); ax.legend(fontsize=9)

    # 收斂速度：以 wall-clock 為 x 軸才公平 —— 每步的成本差很多
    ax = axes[2]
    for n in present:
        r = results[n]
        v = r["val_hist"]
        secs = [s / r["it_per_s"] / 60 for s in v["step"]]
        ax.plot(secs, v["val_loss"], color=COLOR[n], label=LABEL[n], lw=1.8, marker="o", ms=3)
    ax.set_xlabel("wall-clock (min)"); ax.set_ylabel("held-out val loss")
    ax.set_title("Val loss vs wall-clock (same GPU)")
    ax.grid(alpha=.25); ax.legend(fontsize=9)

    fig.suptitle("MiniMind ablation — 8L/512d, 24.6M tokens, RTX 4070", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT_SVG, format="svg", bbox_inches="tight")
    print(f"✅ 圖已存至 {OUT_SVG}")

    # --- 比較表 ---
    base = results.get("vanilla", {}).get("final_val_loss")
    print()
    print(f"{'config':24s} {'params':>9s} {'val_loss':>9s} {'ppl':>8s} {'Δ vs van':>9s} "
          f"{'it/s':>6s} {'min':>6s} {'VRAM':>7s}")
    print("-" * 92)
    for n in present:
        r = results[n]
        d = f"{r['final_val_loss']-base:+.4f}" if base else "—"
        print(f"{LABEL[n]:24s} {r['params_total_M']:8.1f}M {r['final_val_loss']:9.4f} "
              f"{r['final_val_ppl']:8.2f} {d:>9s} {r['it_per_s']:6.2f} "
              f"{r['wall_clock_s']/60:6.1f} {r['peak_vram_GiB']:6.2f}G")

    # 達到 vanilla 最終水準所需的步數 / 時間
    if base:
        print(f"\n達到 vanilla 最終 val_loss ({base:.4f}) 所需：")
        for n in present:
            r = results[n]
            v = r["val_hist"]
            hit = next((s for s, l in zip(v["step"], v["val_loss"]) if l <= base), None)
            if hit is None:
                print(f"  {LABEL[n]:24s} 未達到")
            else:
                print(f"  {LABEL[n]:24s} {hit:5d} 步 ({hit/r['it_per_s']/60:5.1f} min)"
                      f"  = vanilla 的 {hit/max(v['step']):.0%} 步數")


if __name__ == "__main__":
    main()
