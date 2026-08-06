"""A 臂 `physical-tag` —— 把「我是第幾格」明確編進交付。

**預先登記在 `BRIDGE_PREREG_tag.md`（寫於本檔任何結果之前）。本檔只執行。**

§4.53 定位到：交付把原生定址蓋掉（`‖ΔK‖/‖K_nat‖`=6.77×、`cos(K',K_nat)`=0.134），
**但 attention 不是選擇機制**（argmax 命中 41.0% vs 亂猜 41.9%，逐 slot permutation
null p=0.16）。所以下一步是**顯式 slot identity**。

### tag 為什麼不是 oracle（Codex [136] 的三條，全部滿足才有效）

1. tag 由 **fact 在 prompt 裡的位置**（fact index）決定，
   在 episode 生成／交付**之前**就固定，與 `used_id`、query target、答案、值內容**統計獨立**。
2. **每一條 delivered carrier 都拿同規格 tag**，不是只給被問的那格。
3. 哪些 fact 成為載體、問哪一格，每題重抽（`random_mask` + `make_episode`）。

> tag 只說「**我是第幾格**」，不說「**你要讀我**」。
> 模型仍得自己從文字算出「`dan b` 是第 2 條」才能去找 code[2]。

**這只測「身份能否被保留」，不測語意 binding** —— 後者是 B 臂 `address-tag`，
query 端要能獨立生出同一個 address。**兩臂不得合併成一個 PASS。**

### 實作

`tag` 是**固定正交碼**（8 維，QR 分解自固定 seed 的隨機矩陣，**永不訓練**），
接在 latent 後面一起餵進 `f`：`f([z ; tag])`。core 完全凍結，只訓 delivery，
架構／資料／步數與 `_f8` 對齊（12000 步、bs 32、`nfreq=8`、`mode=kv`）。
"""
import argparse
import json
import os
import random
import sys
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_delivery import SPAN, Delivery, mk_fn
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
TAG_DIM = 8                     # 鎖死
MAX_SLOT = 4                    # n_fact 最多 4


def tag_codes(dim=TAG_DIM, n=MAX_SLOT, seed=20260806):
    """固定正交碼，**交付前生成、永不訓練**。QR 保證彼此正交。"""
    g = torch.Generator().manual_seed(seed)
    q, _ = torch.linalg.qr(torch.randn(dim, dim, generator=g))
    return q[:n].contiguous()               # (n, dim)，列彼此正交


class TagDelivery(Delivery):
    """`f([z ; tag])`。只把 latent 維度加寬，其餘與 `Delivery` 完全相同。

    刻意繼承而不改 `bridge_delivery.py` —— 那個檔案產生了 `_f8/_f8v/_f8k`，
    不動它才能保證舊 artifact 可重現。
    """

    def __init__(self, latent_dim, *a, tag_dim=TAG_DIM, **kw):
        super().__init__(latent_dim + tag_dim, *a, **kw)
        self.tag_dim = tag_dim
        # 父類別在 nfreq>0 時只對第 0 維做 Fourier；tag 是 one-hot 式正交碼，
        # 不需要展開，跟著 `Z[..., 1:]` 那條路走即可。


def make_item_tag(tok, rng, j, codes):
    """與 `bridge_delivery.make_item` 同分布，但 `Z` 後面接上該格的 tag。"""
    ep = B.make_episode(rng, j)
    mask = B.random_mask(rng, ep, p=0.6, force_used=True)
    ids, pos = B.value_positions(tok, ep, mask)
    car = [i for i, m in enumerate(mask) if m]
    lat = torch.tensor(B.episode_latents(ep, mask), dtype=torch.float32)
    # tag 由 **fact index** 決定（prompt 裡的第幾條），與被問哪一格無關
    tg = torch.stack([codes[i] for i in car])
    lat = torch.cat([lat, tg], dim=1)

    Z = torch.zeros(len(ids), B.LATENT_DIM + codes.shape[1])
    M = torch.zeros(len(ids), 1)
    Z[pos] = lat.repeat_interleave(SPAN, dim=0)
    M[pos] = 1.0
    return ep, mask, ids, pos, Z, M


def batch(tok, rng, bs, js, codes):
    items, maxlen = [], 0
    for _ in range(bs):
        ep, mask, ids, pos, Z, M = make_item_tag(tok, rng, js[rng.randrange(len(js))], codes)
        p_c, ans = B.render(ep, mask)
        full = tok(tok.bos_token + p_c + " " + ans + tok.eos_token,
                   add_special_tokens=False).input_ids
        plen = len(ids)
        y = list(full); y[:plen] = [-100] * plen
        items.append((full, y, Z, M)); maxlen = max(maxlen, len(full))
    pad = tok.pad_token_id or 0
    X = torch.tensor([f + [pad] * (maxlen - len(f)) for f, _, _, _ in items])
    Y = torch.tensor([y + [-100] * (maxlen - len(y)) for _, y, _, _ in items])
    Zb = torch.stack([F.pad(z, (0, 0, 0, maxlen - z.shape[0])) for _, _, z, _ in items])
    Mb = torch.stack([F.pad(m, (0, 0, 0, maxlen - m.shape[0])) for _, _, _, m in items])
    return X, Y, Zb, Mb


@torch.no_grad()
def greedy_tag(m, tok, ids, loops, dl, Z, M, nl, max_new=4):
    kw = {"kv_override": mk_fn(dl, Z.unsqueeze(0).to(DEVICE),
                               M.unsqueeze(0).to(DEVICE), nl)}
    out = m(ids.unsqueeze(0).to(DEVICE), use_cache=True, num_loops=loops, **kw)
    pkv, got = out.past_key_values, []
    for _ in range(max_new):
        nxt = out.logits[:, -1].argmax(-1, keepdim=True)
        if nxt.item() == tok.eos_token_id:
            break
        got.append(nxt.item())
        out = m(nxt, past_key_values=pkv, use_cache=True, num_loops=loops)
        pkv = out.past_key_values
    return tok.decode(got, skip_special_tokens=True).strip()


@torch.no_grad()
def evaluate(m, tok, dl, loops, nl, js, n, seed, codes):
    """依 prereg：**逐 `n_delivered` 報**，並與 `1/n_del` 比較（primary）。"""
    from collections import defaultdict
    print(f"\n  {'j':>3s} {'n_car':>6s} {'正確率':>9s} {'95% CI':>18s} {'1/n':>7s}  判讀")
    out = {}
    for j in js:
        by = defaultdict(lambda: [0, 0])
        rng = random.Random(seed + j)
        for _ in range(n):
            ep, mask, ids, pos, Z, M = make_item_tag(tok, rng, j, codes)
            ok = int(greedy_tag(m, tok, ids, loops, dl, Z, M, nl) == ep.answer)
            nc = sum(mask)
            by[nc][0] += ok; by[nc][1] += 1
        for nc in sorted(by):
            k, t = by[nc]
            z, p = 1.96, k / t
            d = 1 + z * z / t
            c = (p + z * z / (2 * t)) / d
            h = z * ((p * (1 - p) / t + z * z / (4 * t * t)) ** 0.5) / d
            lo, hi = max(0, c - h), min(1, c + h)
            pred = 1.0 / nc
            if nc == 1:
                v = "gate（需 >=95%）" + ("  ✓" if p >= 0.95 else "  ✗ INVALID")
            elif lo > pred:
                v = "**脫離 1/n**"
            elif lo <= pred <= hi:
                v = "仍貼 1/n（亂挑）"
            else:
                v = "低於 1/n"
            print(f"  {j:>3d} {nc:>6d} {p:>8.1%} [{lo:>6.1%},{hi:>6.1%}] {pred:>7.1%}  {v}")
        out[j] = {str(k): by[k] for k in by}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default="bridge_core.pth")
    ap.add_argument("--steps", type=int, default=12000)
    ap.add_argument("--bs", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--js", type=int, nargs="+", default=[0, 1])
    ap.add_argument("--eval-js", type=int, nargs="+", default=[0, 1])
    ap.add_argument("--n-eval", type=int, default=250)
    ap.add_argument("--seed", type=int, default=424242)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--nfreq", type=int, default=8)
    ap.add_argument("--tag", default="_tagA")
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, a.core), map_location="cpu")
    arch = dict(blob["arch"])
    gates = json.load(open(os.path.join(HERE, "results_bridge_gates.json")))
    loops = gates["chosen_num_loops"]
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    for p in m.parameters():
        p.requires_grad_(False)

    codes = tag_codes().to(DEVICE)
    nl = arch["num_hidden_layers"] * arch["num_loops"]
    dl = TagDelivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                     arch["hidden_size"] // arch["num_attention_heads"],
                     nfreq=a.nfreq, mode="kv").to(DEVICE)
    nd = sum(p.numel() for p in dl.parameters())
    print(f"  A 臂 physical-tag   TAG_DIM={TAG_DIM}（固定正交碼，永不訓練）")
    print(f"  prereg: BRIDGE_PREREG_tag.md   core 凍結（{a.core}）；只訓 delivery {nd/1e6:.2f}M")
    print(f"  tag 由 **fact index** 決定，與被問哪一格、答案、值內容統計獨立")
    print(f"  ⚠️ 這只測**身份保存**，不測語意 binding（那是 B 臂 address-tag）\n")

    # 正交性自檢：tag 必須真的兩兩正交，否則「身份」本身就是糊的
    gm = codes @ codes.T
    off = (gm - torch.eye(len(codes), device=DEVICE)).abs().max().item()
    print(f"  ---- tag 自檢：max|GramMatrix − I| = {off:.2e}")
    assert off < 1e-5, "tag 不正交"

    print("  ---- smoke：零初始化下 learned 必須 == none（no-op），且梯度非零")
    for j in a.js:
        r = random.Random(a.seed + 7)
        X, Y, Z, M = batch(tok, r, 32, [j], codes.cpu())
        X, Y, Z, M = X.to(DEVICE), Y.to(DEVICE), Z.to(DEVICE), M.to(DEVICE)
        with torch.no_grad():
            l_none = F.cross_entropy(
                m(X, num_loops=loops).logits[:, :-1].reshape(-1, cfg.vocab_size).float(),
                Y[:, 1:].reshape(-1), ignore_index=-100).item()
        lg = m(X, kv_override=mk_fn(dl, Z, M, nl), num_loops=loops).logits
        ls = F.cross_entropy(lg[:, :-1].reshape(-1, cfg.vocab_size).float(),
                             Y[:, 1:].reshape(-1), ignore_index=-100)
        dl.zero_grad(set_to_none=True); ls.backward()
        gn = torch.nn.utils.clip_grad_norm_(dl.parameters(), 1e9).item()
        print(f"    j={j}  none={l_none:.4f}  learned={ls.item():.4f}  "
              f"Δ={abs(ls.item()-l_none):.2e}  |grad|={gn:.4f}")
        assert abs(ls.item() - l_none) < 1e-4, "零初始化下不是 no-op —— 注入路徑接錯"
        assert gn > 1e-8, f"j={j} 梯度為零"
    dl.zero_grad(set_to_none=True)
    print()

    opt = torch.optim.AdamW(dl.parameters(), lr=a.lr, weight_decay=0.01)
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps, pct_start=0.05)
    rng = random.Random(a.seed)
    ccpu = codes.cpu()
    dl.train(); t0 = time.time()
    for step in range(1, a.steps + 1):
        X, Y, Z, M = batch(tok, rng, a.bs, a.js, ccpu)
        X, Y, Z, M = X.to(DEVICE), Y.to(DEVICE), Z.to(DEVICE), M.to(DEVICE)
        s0 = time.time()
        lg = m(X, kv_override=mk_fn(dl, Z, M, nl), num_loops=loops).logits
        loss = F.cross_entropy(lg[:, :-1].reshape(-1, lg.size(-1)).float(),
                               Y[:, 1:].reshape(-1), ignore_index=-100)
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(dl.parameters(), 1.0)
        opt.step(); sch.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time()-s0)*(1/a.throttle-1))
        if step % 250 == 0:
            print(f"  step {step:5d}/{a.steps} loss={loss.item():.4f} "
                  f"{step/(time.time()-t0):.2f} it/s  {(time.time()-t0)/60:.0f} min", flush=True)

    dl.eval()
    res = evaluate(m, tok, dl, loops, nl, a.eval_js, a.n_eval, a.seed + 99, ccpu)
    print("\n  判讀（BRIDGE_PREREG_tag.md §2）：")
    print("    n_del=1 <95% → INVALID；n_del=2 與 4 的 CI 都排除且高於 1/n → **A-PASS**")
    print("    任一格 CI 含 1/n → **A-FAIL**（仍在亂挑）")
    print("    ⚠️ n_delivered=1 的高分不算數 —— 只注一條時 binding 平凡")
    torch.save({"delivery": dl.state_dict(), "loops": loops, "core": a.core,
                "nfreq": a.nfreq, "mode": "kv", "tag_dim": TAG_DIM,
                "codes": codes.cpu()},
               os.path.join(HERE, f"bridge_delivery{a.tag}.pth"))
    json.dump({"prereg": "BRIDGE_PREREG_tag.md", "arm": "physical-tag",
               "tag_dim": TAG_DIM, "steps": a.steps, "nfreq": a.nfreq,
               "delivery_params_M": nd/1e6, "seed": a.seed,
               "acc_by_ncarrier": {str(k): v for k, v in res.items()}},
              open(os.path.join(HERE, f"results_bridge_delivery{a.tag}.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  -> bridge_delivery{a.tag}.pth / results_bridge_delivery{a.tag}.json")


if __name__ == "__main__":
    main()
