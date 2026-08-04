"""G6b：交付 vs 文字的**逐步發散**診斷 —— 找誤差第一次被放大的合成步。

§4.45 定位到「交付 × 其後合成」的交互，但沒說清楚**放大發生在哪裡**。
[117] 我猜「v2 把 `j` 當條件變數」——**已被程式碼直接證偽**：
`ContextualKVAdapter(use_context=False)` 的 delta 只是 `(slot, z)` 的函數，
不看 native K/V、更看不到 `j`，所以**交付的 delta 跨 `j` 逐位元相同**。

因此分歧只能來自**與後續 token／attention dynamics 的交互**。這裡量：

  1. 交付 vs 文字在**同一題**上的 hidden 逐層距離
  2. 每多一步合成，該距離如何變化（**增益 > 1 就是放大**）
  3. 正確類別的 margin 何時被吃掉

⚠️ 結果先寫成**機制假說**，不是結論（Codex）。
"""
import os, random, sys, json
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import g1_renderer as R, g3b_closure as G3B, g3d_scale_audit as G3D
import g5b_segmented_reasoning as G5B
from g1_train import ARCH, DEVICE
from model.memory_module import ADDR_DIM, PERM_N, perm_to_latent

HERE = os.path.dirname(os.path.abspath(__file__)); IDENT = list(range(PERM_N))

@torch.no_grad()
def hidden_of(m, tok, state, slots, dl=None, lat=None):
    ids, pos = G5B.slot_positions(tok, state, slots)
    kw = {}
    if lat is not None:
        kw["kv_override"] = G5B._mk_fn(dl, lat.unsqueeze(0), pos.to(DEVICE)) \
            if hasattr(G5B, "_mk_fn") else None
    if lat is not None:
        import g3a_train as G3
        kw["kv_override"] = G3._mk_fn(dl, lat.unsqueeze(0), pos.to(DEVICE))
    h, _, _, _ = m.model(ids.unsqueeze(0).to(DEVICE), num_loops=ARCH["num_loops"], **kw)
    return h[0, -1]                                  # 最後一個 token 的最終層

def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                     "results_g2b.json")
    _, perms = R.perm_splits()
    rng = random.Random(13579)
    print(f"  交付的 delta 只是 `(slot, z)` 的函數 → **跨 j 逐位元相同**（已由程式碼證實）")
    print(f"  所以分歧只能來自與後續 token／attention dynamics 的交互\n")
    print(f"  {'j':>2s} {'‖h_deliv − h_text‖':>20s} {'相對 ‖h_text‖':>14s} {'逐步增益':>10s}")
    prev = None; out = []
    for j in range(0, 4):
        ds, rs = [], []
        for _ in range(60):
            P = list(perms[rng.randrange(len(perms))])
            ps = [list(perms[rng.randrange(len(perms))]) for _ in range(j)]
            lat = perm_to_latent(P).to(DEVICE).unsqueeze(0)
            hd = hidden_of(m, tok, IDENT, [None] + ps, dl, lat)
            ht = hidden_of(m, tok, IDENT, [P] + ps)
            ds.append(float((hd - ht).norm())); rs.append(float(ht.norm()))
        d = sum(ds)/len(ds); r = sum(rs)/len(rs)
        gain = (d/prev) if prev else float("nan")
        print(f"  {j:>2d} {d:>20.4f} {d/r:>14.4f} {gain:>10.3f}")
        out.append({"j": j, "dist": d, "rel": d/r, "gain": gain}); prev = d
    print(f"\n  **機制假說**（不是結論）：逐步增益若持續 >1，"
          f"代表 executor dynamics 對\n  「交付造成的初始偏移」有放大作用 ——"
          f"那就不是覆蓋問題，擴大 j 覆蓋修不好。")
    json.dump(out, open(os.path.join(HERE, "results_g6b.json"), "w"), indent=2)
    print(f"  -> results_g6b.json")

main()
