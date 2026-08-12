"""`LKE-2R` 的**歸因診斷** —— U／T／Ø 的 confidence 分布。

**描述性，不改任何 gate、不產生新的 PASS／FAIL。**
prereg 要求 FAIL 時把結論拆成 `U extraction`／`selective-confidence transfer`／
`downstream ceiling` 三者之一；U 的 raw exact 是 100%，所以不是前者，
本檔量的是後者到底長什麼樣。
"""
import os, sys
import torch
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from transformers import AutoTokenizer
import lke2r_data as D
from lke1_run import predict, train as train_extractor

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = [0.5, 0.7, 0.85, 0.9, 0.95, 0.975, 0.99, 0.999, 0.9999, 1.0]


def main(seed=20260812):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    art = D.load()
    m = train_extractor(tok, art, seed)
    S = {}
    for name, qs in (("U (ID)", [e["q"] for e in art["u_cells"]["ID"]]),
                     ("U (KxP)", [e["q"] for e in art["u_cells"]["KxP"]]),
                     ("T", [e["q"] for e in art["to_test"]["T"]]),
                     ("O", [e["q"] for e in art["to_test"]["O"]])):
        S[name] = torch.tensor([p[1] for p in predict(m, tok, qs)])

    qs = torch.tensor([0.01, 0.25, 0.5, 0.75, 0.99])
    print(f"  LKE-2R confidence 分布   seed={seed}（描述性診斷）")
    print(f"  {'stratum':>8s} {'n':>4s} {'均':>7s} {'p1':>7s} {'p25':>7s} "
          f"{'中位':>7s} {'p75':>7s} {'p99':>7s}")
    for k, c in S.items():
        v = torch.quantile(c, qs)
        print(f"  {k:>8s} {len(c):>4d} {c.mean():>7.4f} " +
              " ".join(f"{x:>7.4f}" for x in v))

    print("\n  ---- 尾巴：各門檻以上有多少 T／Ø 被接受（這就是 grid 失敗的原因）")
    print(f"  {'tau':>8s} {'T accept':>10s} {'Ø accept':>10s} {'U(ID) 誤拒':>12s}")
    for t in GRID:
        nt = "{}/300".format(int((S["T"] >= t).sum()))
        no = "{}/300".format(int((S["O"] >= t).sum()))
        nu = "{}/150".format(int((S["U (ID)"] < t).sum()))
        print(f"  {t:>8} {nt:>10s} {no:>10s} {nu:>12s}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20260812)
