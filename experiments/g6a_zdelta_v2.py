"""G6a：**`zdelta-v2`** —— composition-aware 的交付訓練。

§4.45 定位到的機制：**交付進來的值參與「其後的合成」時比文字值差**，
且 **mixed carrier 有額外且獨立的懲罰**。`j=0` 的讀出已經是 100%，
所以**重建保真度不是 blocker** —— 要修的是**交付 × 合成的交互**。

### 命名與紀律（Codex）

**`zdelta-v2` 是新的 checkpoint、新的 config hash、新的結果章節。**
§4.25 的 **`zdelta-v1` 與 G1/G3/G5 全部維持當時的權重與裁決** ——
**不重算、不覆寫**。v2 只能列為對 v1 的 **prospective comparison**；
即使通過，也**不能反向把舊的 FAIL 改成 PASS**，整鏈 validation 另做。

### 第一階段只訓 delivery

**core / writer / store / retriever 全部凍結**，只訓 `zdelta-v2`。
若連 small-overfit 都失敗，再另立 `core + zdelta-v2` 的架構題 ——
**不得在同一個實驗裡臨時解凍**。

### 訓練設計

⚠️ 兩個目標**不是可分的兩個 loss**：用**同一批 factorial episode**
同時交叉 `j ∈ {0,1,2}` 與 carrier 配置 —— 否則模型會**從沒看過真正失敗的交互格**
（composition loss 只在 allph、augmentation 只在 j=0 就是這個陷阱）。

**泛化**：train 只覆蓋部分 `mixture × slot count × j`，
test 留出**完整組合**，另留一條 **j 外推**（train j≤2、test **j=3**）。
**primary 必須包含追平 `text carry` 的 paired gap**，不只看相對 v1 的改善。
**anchors**：`j=0` 與原 all-placeholder 作 no-regression。
"""
import argparse, json, os, random, sys, time
from collections import defaultdict
import torch, torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import g1_renderer as R, g3a_train as G3, g3b_closure as G3B, g3d_scale_audit as G3D
import g5b_segmented_reasoning as G5B
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from model.memory_module import (ADDR_DIM, PERM_N, LatentStore, MemoryEntry,
                                 perm_to_latent)

HERE = os.path.dirname(os.path.abspath(__file__))
IDENT = list(range(PERM_N))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]


def make_item(rng, perms, j, cfg):
    """交付 1 個 P，其後 j 個真實置換；`cfg` 決定那 j 個走交付還是文字。"""
    P = list(perms[rng.randrange(len(perms))])
    ps = [list(perms[rng.randrange(len(perms))]) for _ in range(j)]
    gold = P
    for q in ps:
        gold = G5B.compose(gold, q)
    delivered = [P] + (ps if cfg == "allph" else [])
    explicit = [] if cfg == "allph" else ps
    slots = [None] * len(delivered) + explicit
    return slots, delivered, gold


def batch(tok, items):
    """同一 (j,cfg) 的 slot 結構固定 → 可直接批次。"""
    X, Y, pos = [], [], None
    for slots, _, gold in items:
        ids, p = G5B.slot_positions(tok, IDENT, slots)
        full = tok(tok.bos_token + G5B.render_slots(IDENT, slots) + R._nums(gold)
                   + tok.eos_token, add_special_tokens=False).input_ids
        y = list(full); y[:len(ids)] = [-100] * len(ids)
        X.append(full); Y.append(y); pos = p
    return torch.tensor(X), torch.tensor(Y), pos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=606060)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        a.steps = 400

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl_v1, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                        "results_g2b.json")
    dl = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                       os.path.join(HERE, "g1_renderer_artifact.json"))
    dl.load_state_dict(dl_v1.state_dict())          # 從 v1 起手，但**另存為 v2**
    for p in m.parameters():
        p.requires_grad_(False)
    for p in dl.parameters():
        p.requires_grad_(True)
    _, perms = R.perm_splits()

    # **train 只覆蓋部分格子**；j=3 與 (mixed, j=2) 留作 held-out
    TRAIN = [(0, "allph"), (0, "mixed"), (1, "allph"), (1, "mixed"), (2, "allph")]
    HELD = [(2, "mixed"), (3, "allph"), (3, "mixed")]
    print(f"  **只訓 `zdelta-v2`**（{sum(p.numel() for p in dl.parameters())/1e6:.2f}M）；"
          f"core/writer/store/retriever 全凍結")
    print(f"  train 格子 {TRAIN}")
    print(f"  **held-out** {HELD}  ← 含 **j 外推**（train j≤2、test j=3）\n")

    opt = torch.optim.AdamW(dl.parameters(), lr=a.lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps,
                                                pct_start=0.05)
    rng = random.Random(a.seed)
    dl.train(); t0 = time.time()
    for step in range(1, a.steps + 1):
        j, cfg = TRAIN[step % len(TRAIN)]
        items = [make_item(rng, perms, j, cfg) for _ in range(a.bs)]
        X, Y, pos = batch(tok, items)
        lat = torch.stack([torch.stack([perm_to_latent(v) for v in d])
                           for _, d, _ in items]).to(DEVICE)
        s0 = time.time()
        box = {"i": 0}
        def fn(xk, xv, _l=lat, _p=pos.to(DEVICE)):
            i = box["i"]; box["i"] = (i + 1) % NL
            k, v = dl(i, _l, xk[:, _p], xv[:, _p])
            return _p, k, v
        logits = m(X.to(DEVICE), kv_override=fn).logits
        loss = F.cross_entropy(logits[:, :-1].reshape(-1, logits.size(-1)).float(),
                               Y.to(DEVICE)[:, 1:].reshape(-1), ignore_index=-100)
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(dl.parameters(), 1.0)
        opt.step(); sched.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time()-s0)*(1/a.throttle-1))
        if step % 300 == 0:
            print(f"  step {step:5d}/{a.steps} loss={loss.item():.4f} "
                  f"{step/(time.time()-t0):.1f} it/s", flush=True)

    # ---- 評測：v1 vs v2 vs text，逐 (j,cfg) ----
    dl.eval()
    erng = random.Random(a.seed + 99)
    print(f"\n  {'格子':<14s} {'v1':>8s} {'**v2**':>8s} {'text':>8s} {'v2−text':>9s}")
    out = {}
    for j, cfg in TRAIN + HELD:
        n = 100 if not a.smoke else 40
        acc = {}
        for name, D in (("v1", dl_v1), ("v2", dl)):
            ok = 0
            for _ in range(n):
                r2 = random.Random(hash((j, cfg, _, a.seed)) & 0xffffffff)
                slots, deliv, gold = make_item(r2, perms, j, cfg)
                lat = torch.stack([perm_to_latent(v) for v in deliv]).to(DEVICE)
                got = G5B.call_core(m, tok, IDENT, slots, D, lat)
                ok += int(got == gold)
            acc[name] = ok / n
        ok = 0
        for _ in range(n):
            r2 = random.Random(hash((j, cfg, _, a.seed)) & 0xffffffff)
            slots, deliv, gold = make_item(r2, perms, j, cfg)
            ps = [v for v in deliv] + [s for s in slots if s is not None]
            ok += int(G5B.call_core(m, tok, IDENT, ps) == gold)
        acc["text"] = ok / n
        tag = f"j={j} {cfg}" + ("  **HELD**" if (j, cfg) in HELD else "")
        print(f"  {tag:<14s} {acc['v1']:7.1%} {acc['v2']:7.1%} {acc['text']:7.1%} "
              f"{(acc['v2']-acc['text'])*100:+8.1f}pp")
        out[f"j{j}_{cfg}"] = {**acc, "held_out": (j, cfg) in HELD}
    print(f"\n  ⚠️ **primary 是「追平 `text`」**，不是「贏過 v1」。"
          f"\n     held-out 格子（含 **j 外推**）才分得開"
          f"「補齊見過的格子」與「學到 composition-stable delivery」。")
    torch.save(dl.state_dict(), os.path.join(HERE, "zdelta_v2.pth"))
    json.dump({"train_cells": TRAIN, "held_out": HELD, "steps": a.steps,
               "lr": a.lr, "seed": a.seed, "acc": out, "smoke": a.smoke},
              open(os.path.join(HERE, f"results_g6a{'_smoke' if a.smoke else ''}.json"),
                   "w"), indent=2, ensure_ascii=False)
    print(f"  -> zdelta_v2.pth / results_g6a.json")


if __name__ == "__main__":
    main()
