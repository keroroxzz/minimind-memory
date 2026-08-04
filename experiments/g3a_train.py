"""G3a：**write formation**（oracle retrieval、missing=0）。

G2c 判定 open-set 的 missing calibration 不可轉移，所以閉環暫時走不通 ——
但 write 元件本身可以獨立取得證據（Codex）。G3a 把 write 隔離出來問一件事：

    writer 能不能從一個**事件**形成 latent，讓**凍結的、獨立訓練過的**
    `zdelta` 交付介面消費，而下游凍結的 core 仍答得對？

這正是「可分離、可抽換的記憶模組」這個主張的直接測試 ——
writer 從沒看過 `zdelta`，`zdelta` 也從沒看過 writer。

**隔離掉的東西：** 檢索（oracle 指定該步要用哪一條）、missing（p=0）、
交付（`zdelta` 凍結）、core（凍結）。**唯一可學的是 writer。**

**事件**是 `| f3 = 3 1 0 2 4 定` —— 刻意不含 chain、不含 state：
寫入是 per-entry 的，不可偷看下游要問什麼，否則測到的是「看題目寫答案」。

**泛化軸是 permutation**（key 身分的泛化是 G2c 的事，已判 fail）：
train/val 用**不交的置換集合**，val 的 95→24 個置換 writer 從未寫過。

四個條件一起報，缺一不可判讀：
  oracle    `perm_to_latent`，零學習 —— **天花板**
  writer    學到的 —— 主條件
  zero      全零 latent —— **地板**
  shuffled  writer 讀**別條 entry** 的事件 —— 證明它真的在讀事件，
            而不是靠 carrier 位置或任何與事件無關的東西
"""
import argparse
import hashlib
import itertools
import json
import os
import random
import sys
import time
from collections import defaultdict

import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_oracle_inline import value_positions
from g1_teacher_kv import greedy_override
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from model.memory_module import LATENT_DIM, PERM_N, perm_to_latent
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_ZD = "g1_G1a_zdelta_d9a603e4e2.pth"
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]
WRITE_KEYS = R.KEYS                      # f0..f3，與 G1 相同；身分泛化不是本階段的軸


class Writer(nn.Module):
    """事件 → latent。**唯一可學的組件。**

    輸入是凍結 core 在 define view 的 value span 上的 hidden（5 × hidden），
    輸出 25 維 latent —— 必須落在 `zdelta` 能消費的那個空間裡，
    而 writer 從未見過 `zdelta`。
    """

    def __init__(self, hidden, width=256):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(PERM_N * hidden, width), nn.GELU(), nn.Linear(width, LATENT_DIM))

    def forward(self, h):                       # (B, 5, H) -> (B, 25)
        return self.net(h.flatten(-2))


def make_sample(k, rng, perm_pool):
    """與 `R.make_canonical` 同構，但置換**只從指定的池抽** —— 泛化軸在這裡。"""
    present = sorted(rng.sample(range(len(WRITE_KEYS)), 2))
    defs, seen = {}, set()
    for i in present:
        while True:
            q = list(perm_pool[rng.randrange(len(perm_pool))])
            if tuple(q) not in seen:
                seen.add(tuple(q)); defs[WRITE_KEYS[i]] = q; break
    order = [WRITE_KEYS[i] for i in present]; rng.shuffle(order)
    state = list(range(PERM_N)); rng.shuffle(state)
    chain = [WRITE_KEYS[rng.choice(present)] for _ in range(k)]
    st = list(state)
    for g in chain:
        st = [st[defs[g][i]] for i in range(PERM_N)]
    return R.CanonicalSample(k=k, defs=defs, present=order, state=state,
                             chain=chain, answer=st)


@torch.no_grad()
def precompute_events(model, tok, perms, bs=64):
    """所有 (key, perm) 事件在凍結 core 上的 define-view hidden。

    core 凍結 ⇒ 這些 hidden 是常數，可以一次算完；梯度只進 writer。
    """
    items = [(kk, tuple(p)) for kk in WRITE_KEYS for p in perms]
    out, enc, spans = {}, {}, {}
    for kk, p in items:
        view, sp = R.define_view_value_span(tok, kk, list(p))
        enc[(kk, p)] = tok(tok.bos_token + view, add_special_tokens=False).input_ids
        spans[(kk, p)] = sp
    by_len = defaultdict(list)
    for key, v in enc.items():
        by_len[len(v)].append(key)
    for _, group in by_len.items():
        for c in range(0, len(group), bs):
            chunk = group[c:c + bs]
            ids = torch.tensor([enc[key] for key in chunk]).to(DEVICE)
            h, _, _, _ = model.model(ids, num_loops=ARCH["num_loops"])
            for j, key in enumerate(chunk):
                out[key] = h[j, spans[key]].clone()          # (5, H)
    return out


def chain_events(s):
    """該樣本每一步要用的事件 key —— **oracle 檢索**：直接給對的那一條。"""
    return [(g, tuple(s.defs[g])) for g in s.chain]


def latents_for(cond, s, ev, writer, rng=None, all_keys=None):
    """依條件產生該樣本的 (k, 25) latent。"""
    ce = chain_events(s)
    if cond == "oracle":
        return torch.stack([perm_to_latent(list(p)) for _, p in ce]).to(DEVICE)
    if cond == "zero":
        return torch.zeros(len(ce), LATENT_DIM, device=DEVICE)
    if cond == "shuffled":
        # 讀**別條 entry** 的事件 —— 若這個條件也高分，代表 latent 內容不重要，
        # 分數其實來自 carrier 位置或別的與事件無關的東西。
        ce = [all_keys[rng.randrange(len(all_keys))] for _ in ce]
    return writer(torch.stack([ev[key] for key in ce]))


def deliver_kwargs(delivery, latents, pos):
    box = {"i": 0}

    def fn(xk, xv):
        i = box["i"]; box["i"] = (i + 1) % NL
        kk, vv = delivery(i, latents, xk[:, pos], xv[:, pos])
        return pos, kk, vv
    return {"kv_override": fn}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--train-per-k", type=int, default=4000)
    ap.add_argument("--val-per-k", type=int, default=100)
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        a.steps, a.train_per_k, a.val_per_k = 600, 600, 40

    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    blob = torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")
    m.load_state_dict(blob["model"], strict=False)
    dl = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                       os.path.join(HERE, "g1_renderer_artifact.json"))
    dl.load_state_dict(blob["delivery"]); dl.eval()
    for p in list(m.parameters()) + list(dl.parameters()):
        p.requires_grad_(False)

    p_tr, p_va = R.perm_splits()
    writer = Writer(BACKBONE["hidden_size"]).to(DEVICE)
    n_w = sum(p.numel() for p in writer.parameters())
    print(f"  core + zdelta **全凍結**；唯一可學的是 writer {n_w/1e6:.3f}M")
    print(f"  置換切分：train {len(p_tr)} / val {len(p_va)}，**完全不交** ——"
          f" val 的置換 writer 從未寫過")
    print(f"  oracle 檢索、missing=0：檢索與棄答**不在本階段的測試範圍內**")

    ev_tr = precompute_events(m, tok, p_tr)
    ev_va = precompute_events(m, tok, p_va)
    ev_all = {**ev_tr, **ev_va}
    print(f"  事件 hidden 預算完成：train {len(ev_tr)} / val {len(ev_va)} 個 (key, perm)")

    rng = random.Random(a.seed)
    tr = [make_sample(k, rng, p_tr) for k in range(1, a.max_k + 1)
          for _ in range(a.train_per_k)]
    vrng = random.Random(a.seed + 999)
    seen = {s.delivery_id for s in tr}
    va = []
    for k in range(1, a.max_k + 1):
        got = 0
        while got < a.val_per_k:
            s = make_sample(k, vrng, p_va)
            if s.delivery_id not in seen:
                va.append(s); seen.add(s.delivery_id); got += 1
    print(f"  train {len(tr)} / val {len(va)}（val 的置換全部未見）\n")

    # token 化：同一個 k 的 latent render 結構固定，位置可共用
    X, Y, pos_by_k, by_k = {}, {}, {}, defaultdict(list)
    for i, s in enumerate(tr):
        by_k[s.k].append(i)
    for k in sorted(by_k):
        s0 = tr[by_k[k][0]]
        _, b_ids, pos = value_positions(tok, s0)
        pos_by_k[k] = pos.to(DEVICE)
    for k in sorted(by_k):
        ids, lbl = [], []
        for i in by_k[k]:
            s = tr[i]
            p_, ans = R.render_latent(s)
            full = tok(tok.bos_token + p_ + ans + tok.eos_token,
                       add_special_tokens=False).input_ids
            plen = len(tok(tok.bos_token + p_, add_special_tokens=False).input_ids)
            y = list(full); y[:plen] = [-100] * plen
            ids.append(full); lbl.append(y)
        X[k] = torch.tensor(ids); Y[k] = torch.tensor(lbl)

    opt = torch.optim.AdamW(writer.parameters(), lr=a.lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps,
                                                pct_start=0.05)
    ks = sorted(by_k)
    print(f"{'=' * 58}\n  G3a write formation — {a.steps} 步（throttle {a.throttle:.0%}）"
          f"\n{'=' * 58}", flush=True)
    writer.train(); t0 = time.time()
    g = torch.Generator().manual_seed(a.seed)
    for step in range(1, a.steps + 1):
        kk = ks[step % len(ks)]
        idx = torch.randint(len(by_k[kk]), (a.batch_size,), generator=g).tolist()
        x, y = X[kk][idx].to(DEVICE), Y[kk][idx].to(DEVICE)
        ce = [chain_events(tr[by_k[kk][i]]) for i in idx]
        h = torch.stack([torch.stack([ev_tr[e] for e in row]) for row in ce])  # (B,k,5,H)
        s0 = time.time()
        lat = writer(h.flatten(0, 1)).view(len(idx), kk, LATENT_DIM)
        logits = m(x, **deliver_kwargs(dl, lat, pos_by_k[kk])).logits
        loss = F.cross_entropy(logits[:, :-1].reshape(-1, logits.size(-1)).float(),
                               y[:, 1:].reshape(-1), ignore_index=-100)
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(writer.parameters(), 1.0)
        opt.step(); sched.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time() - s0) * (1 / a.throttle - 1))
        if step % 200 == 0:
            print(f"  step {step:5d}/{a.steps} loss={loss.item():.4f} "
                  f"{step/(time.time()-t0):.1f} it/s", flush=True)

    # ---- 四條件評測 ----
    writer.eval()
    conds = ("oracle", "writer", "zero", "shuffled")
    res = {c: defaultdict(lambda: [0, 0]) for c in conds}
    lat_err = []
    srng = random.Random(a.seed + 7)
    ev_keys = list(ev_all)
    with torch.no_grad():
        for s in va:
            _, b_ids, pos = value_positions(tok, s)
            pos = pos.to(DEVICE)
            ans = R.render_L0(s)[1]
            for c in conds:
                lat = latents_for(c, s, ev_all, writer, srng, ev_keys).unsqueeze(0)
                if c == "writer":
                    tgt = torch.stack([perm_to_latent(list(p))
                                       for _, p in chain_events(s)]).to(DEVICE)
                    lat_err.append((lat[0] - tgt).abs().max().item())
                got = greedy_override(m, tok, b_ids,
                                      _mk_fn(dl, lat, pos), ARCH["num_loops"])
                r = res[c][s.k]
                r[1] += 1; r[0] += (got == ans)

    print(f"\n  val = **未見置換**（{len(p_va)} 個，writer 從未寫過）")
    print(f"  {'條件':<10s} " + "  ".join(f"k={k}" for k in sorted(res['oracle'])) + "     整體")
    out = {}
    for c in conds:
        d = res[c]
        cells = "  ".join(f"{d[k][0]/d[k][1]:5.1%}" for k in sorted(d))
        ov = sum(v[0] for v in d.values()) / sum(v[1] for v in d.values())
        out[c] = ov
        print(f"  {c:<10s} {cells}    {ov:6.1%}")
    print(f"\n  oracle   = perm_to_latent，零學習 —— **天花板**")
    print(f"  zero     = 全零 latent —— **地板**")
    print(f"  shuffled = writer 讀別條 entry 的事件 —— 若也高分則結果無效")
    print(f"\n  writer 產出的 latent 與 perm_to_latent 的 max|diff| 中位數："
          f"{sorted(lat_err)[len(lat_err)//2]:.3f}"
          f"（writer **沒有**被監督去複製這個編碼，只有下游答案 loss）")
    print(f"  {(time.time()-t0)/60:.1f} min")

    fp = {"stage": "G3a", "steps": a.steps, "lr": a.lr, "bs": a.batch_size,
          "seed": a.seed, "writer_params": n_w, "core_zdelta": CORE_ZD,
          "n_perm_train": len(p_tr), "n_perm_val": len(p_va), "smoke": a.smoke,
          "val_checksum": R.delivery_checksum(va)}
    h = hashlib.sha256(json.dumps(fp, sort_keys=True, default=str).encode()).hexdigest()[:10]
    json.dump({**fp, "overall": out,
               "per_k": {c: {str(k): v for k, v in res[c].items()} for c in conds}},
              open(os.path.join(HERE, f"results_g3a{'_smoke' if a.smoke else ''}.json"),
                   "w"), indent=2, ensure_ascii=False)
    torch.save(writer.state_dict(), os.path.join(HERE, f"g3a_writer_{h}.pth"))
    print(f"  -> results_g3a{'_smoke' if a.smoke else ''}.json")


def _mk_fn(delivery, lat, pos):
    box = {"i": 0}

    def fn(xk, xv):
        i = box["i"]; box["i"] = (i + 1) % NL
        kk, vv = delivery(i, lat, xk[:, pos], xv[:, pos])
        return pos, kk, vv
    return fn


if __name__ == "__main__":
    main()
