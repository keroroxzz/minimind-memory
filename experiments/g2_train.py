"""G2a：retrieve（store 與 write 凍結）。

只訓 query projector 與 support head。core 與 `zdelta` delivery **全程凍結**，
address bank 固定正交、**不可訓練**。

指標分五項報（`design_c_layer.md` §4.26）：
  1. ordered exact retrieval   2. per-step recall   3. hit/miss
  4. `executor | retrieval correct`（**實際 retriever 全對的子集**，不可用 oracle 代替）
  5. end-to-end

⚠️ 這條路線證明的是 **closed-world symbolic retrieval 的管線**，
   **不證明** semantic query 或對未見 key 的泛化 —— 那是 G2b。
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
from collections import defaultdict

import torch
import torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_oracle_inline import value_positions
from g1_teacher_kv import greedy_override
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from model.memory_module import ADDR_DIM, Retriever, address_vector
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_ZD = "g1_G1a_zdelta_d9a603e4e2.pth"
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]


def key_cue(tok, emb, sym):
    """query 的輸入 = 該 key 的**凍結 token embedding**（不需 core 前向）。"""
    ids = torch.tensor(tok(sym, add_special_tokens=False).input_ids, device=DEVICE)
    with torch.no_grad():
        return emb(ids).mean(0)


def make_item(s, rng, p_missing, tok, emb, cue_cache):
    pool, lat, chain, tgt, hit, dropped = R.build_pool(s, rng, missing=rng.random() < p_missing)
    cues = torch.stack([cue_cache[g] for g in chain])                 # (k, H)
    addrs = torch.stack([address_vector(g) for g in pool]).to(DEVICE)  # (8, A)
    lats = torch.stack([lat[g] for g in pool]).to(DEVICE)              # (8, 25)
    return dict(pool=pool, cues=cues, addrs=addrs, lats=lats,
                tgt=torch.tensor(tgt, device=DEVICE), hit=torch.tensor(hit, device=DEVICE),
                sample=s, dropped=dropped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--train-per-k", type=int, default=5000)
    ap.add_argument("--val-per-k", type=int, default=100)
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--p-missing", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    if a.smoke:
        a.steps, a.train_per_k, a.val_per_k = 800, 800, 60

    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    cfg = MiniMindConfig(**BACKBONE, **ARCH)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    blob = torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")
    m.load_state_dict(blob["model"], strict=False)
    dl = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                       os.path.join(HERE, "g1_renderer_artifact.json"))
    dl.load_state_dict(blob["delivery"]); dl.eval()
    for p in list(m.parameters()) + list(dl.parameters()):
        p.requires_grad_(False)
    emb = m.model.embed_tokens
    cue_cache = {k: key_cue(tok, emb, k) for k in R.ALL_KEYS}

    ret = Retriever(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE)
    n_ret = sum(p.numel() for p in ret.parameters())
    print(f"  core+zdelta 凍結；**只訓 retriever** {n_ret/1e6:.3f}M"
          f"   address 固定正交、不可訓")

    tr = R.build_dataset(a.max_k, a.train_per_k, a.seed)
    va = R.build_dataset(a.max_k, a.val_per_k, a.seed + 999,
                         exclude={s.delivery_id for s in tr})
    print(f"  train {len(tr)} / val {len(va)}   {R.delivery_checksum(tr)} / "
          f"{R.delivery_checksum(va)}")
    by_k = defaultdict(list)
    for i, s in enumerate(tr):
        by_k[s.k].append(i)
    ks = sorted(by_k)

    opt = torch.optim.AdamW(ret.parameters(), lr=a.lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps, pct_start=0.05)
    print(f"\n{'=' * 58}\n  G2a retrieve — {a.steps} 步（throttle {a.throttle:.0%}）\n{'=' * 58}",
          flush=True)

    rng = random.Random(a.seed)
    ret.train(); t0 = time.time()
    for step in range(1, a.steps + 1):
        kk = ks[step % len(ks)]
        idx = [by_k[kk][rng.randrange(len(by_k[kk]))] for _ in range(a.batch_size)]
        items = [make_item(tr[i], rng, a.p_missing, tok, emb, cue_cache) for i in idx]
        cues = torch.stack([it["cues"] for it in items])
        addrs = torch.stack([it["addrs"] for it in items])
        tgt = torch.stack([it["tgt"] for it in items])
        hit = torch.stack([it["hit"] for it in items]).float()
        s0 = time.time()
        logits, sup = ret(cues, addrs)
        # 有 hit 的位置才算 CE；hit/miss **直接監督**（§4.12/§4.20：只靠答案梯度不夠）
        mask = tgt >= 0
        ce = F.cross_entropy(logits[mask], tgt[mask]) if mask.any() else logits.sum() * 0
        bce = F.binary_cross_entropy_with_logits(sup, hit)
        loss = ce + bce
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(ret.parameters(), 1.0)
        opt.step(); sched.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time() - s0) * (1 / a.throttle - 1))
        if step % 200 == 0:
            print(f"  step {step:5d}/{a.steps} ce={ce.item():.4f} bce={bce.item():.4f} "
                  f"{step/(time.time()-t0):.1f} it/s", flush=True)

    # ---- 校準 threshold：**在 val 上校一次就固定**，不可每次挑（Codex）----
    ret.eval()
    cal_rng = random.Random(a.seed + 7)
    sups, hits = [], []
    with torch.no_grad():
        for s in va:
            it = make_item(s, cal_rng, a.p_missing, tok, emb, cue_cache)
            _, sup = ret(it["cues"].unsqueeze(0), it["addrs"].unsqueeze(0))
            sups.append(sup[0]); hits.append(it["hit"])
    sups = torch.cat(sups); hits = torch.cat(hits)
    # 取**分離區間的中點**，不是「掃描到的第一個最佳值」——
    # 後者會停在掃描邊界（實測 -6.00，而 miss 的最高分是 -6.90），
    # 對分布偏移不穩健。實測 margin +22.4，中點有最大的兩側餘裕。
    lo, hi = float(sups.min()) - 1, float(sups.max()) + 1
    grid = torch.linspace(lo, hi, 601)
    accs = torch.tensor([((sups > t) == hits).float().mean().item() for t in grid])
    best_acc = float(accs.max())
    good = grid[accs >= best_acc - 1e-9]
    best_t = float((good.min() + good.max()) / 2)
    print(f"\n  support threshold 在 val 校準 = {best_t:+.2f}（hit/miss acc {best_acc:.1%}；"
          f"最佳區間 [{good.min():+.2f}, {good.max():+.2f}]，取中點），之後固定")

    # ---- 五個指標 ----
    ev_rng = random.Random(a.seed + 99)
    stat = defaultdict(lambda: defaultdict(int))
    exec_given_correct = [0, 0]
    with torch.no_grad():
        for s in va:
            it = make_item(s, ev_rng, a.p_missing, tok, emb, cue_cache)
            logits, sup = ret(it["cues"].unsqueeze(0), it["addrs"].unsqueeze(0))
            pred = logits[0].argmax(-1)
            pred_hit = sup[0] > best_t
            k = s.k
            stat[k]["n"] += 1
            gold_hit = it["hit"]
            step_ok = ((pred == it["tgt"]) & gold_hit) | (~gold_hit & ~pred_hit)
            stat[k]["step_ok"] += int(step_ok.sum()); stat[k]["steps"] += k
            stat[k]["hitacc"] += int((pred_hit == gold_hit).sum())
            ordered_ok = bool(step_ok.all())
            stat[k]["ordered"] += int(ordered_ok)
            if not bool(gold_hit.all()):
                stat[k]["miss_items"] += 1
                stat[k]["miss_abstain"] += int((~pred_hit[~gold_hit]).all())
                continue
            # end-to-end：用 retriever 取回的 latent 交付
            lat = it["lats"][pred].unsqueeze(0)
            _, b_ids, pos = value_positions(tok, s)
            pos = pos.to(DEVICE); box = {"i": 0}

            def fn(xk, xv, _l=lat, _p=pos):
                i = box["i"]; box["i"] = (i + 1) % NL
                kk_, vv_ = dl(i, _l, xk[:, _p], xv[:, _p])
                return _p, kk_, vv_

            g = greedy_override(m, tok, b_ids, fn, ARCH["num_loops"])
            good = (g == R.render_L0(s)[1])
            stat[k]["e2e"] += int(good); stat[k]["e2e_n"] += 1
            if ordered_ok:
                exec_given_correct[1] += 1; exec_given_correct[0] += int(good)

    print(f"\n  {'k':>3s} {'ordered':>9s} {'per-step':>9s} {'hit/miss':>9s} {'end2end':>9s}")
    tot = defaultdict(int)
    for k in sorted(stat):
        d = stat[k]
        for key in ("ordered", "n", "step_ok", "steps", "hitacc", "e2e", "e2e_n"):
            tot[key] += d[key]
        print(f"  {k:>3d} {d['ordered']/d['n']:8.1%} {d['step_ok']/d['steps']:8.1%} "
              f"{d['hitacc']/d['steps']:8.1%} "
              f"{(d['e2e']/d['e2e_n'] if d['e2e_n'] else float('nan')):8.1%}")
    print(f"  整體 {tot['ordered']/tot['n']:7.1%} {tot['step_ok']/tot['steps']:8.1%} "
          f"{tot['hitacc']/tot['steps']:8.1%} {tot['e2e']/max(tot['e2e_n'],1):8.1%}")
    egc = exec_given_correct[0] / max(exec_given_correct[1], 1)
    print(f"\n  executor | retrieval correct = {egc:.1%}  "
          f"(n={exec_given_correct[1]})   ← 實際 retriever 全對的子集")
    print(f"  oracle-retrieval ceiling（render gate）= 99.0%")
    print(f"  {(time.time()-t0)/60:.1f} min")

    fp = {"stage": "G2a", "steps": a.steps, "lr": a.lr, "bs": a.batch_size, "seed": a.seed,
          "p_missing": a.p_missing, "pool": R.POOL_SIZE, "retriever_params": n_ret,
          "threshold": best_t, "core_zdelta": CORE_ZD, "smoke": a.smoke,
          "val_checksum": R.delivery_checksum(va)}
    h = hashlib.sha256(json.dumps(fp, sort_keys=True, default=str).encode()).hexdigest()[:10]
    out = os.path.join(HERE, f"results_g2a{'_smoke' if a.smoke else ''}.json")
    json.dump({**fp, "per_k": {str(k): dict(v) for k, v in stat.items()},
               "executor_given_correct": egc, "egc_n": exec_given_correct[1]},
              open(out, "w"), indent=2, ensure_ascii=False)
    torch.save(ret.state_dict(), os.path.join(HERE, f"g2a_retriever_{h}.pth"))
    print(f"  -> {os.path.basename(out)}")


if __name__ == "__main__":
    main()
