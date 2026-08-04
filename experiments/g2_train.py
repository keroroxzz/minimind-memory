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
from model.memory_module import (ADDR_DIM, Retriever, TiedAddressEncoder, address_vector)
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_ZD = "g1_G1a_zdelta_d9a603e4e2.pth"
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]


def key_cue(tok, emb, sym):
    """query 的輸入 = 該 key 的**凍結 token embedding**（不需 core 前向）。"""
    ids = torch.tensor(tok(sym, add_special_tokens=False).input_ids, device=DEVICE)
    with torch.no_grad():
        return emb(ids).mean(0)


@torch.no_grad()
def precompute_write_hidden(model, tok, keys, bs=64):
    """每個 key 的 **write-view hidden**（不含 context）—— address 的輸入。

    write view 刻意不含 context：address 只依賴 key 身分，
    同一 key 在不同題目裡才會得到一致的 address。
    """
    # ⚠️ write view 的長度**不一致** —— 這個 6400 vocab 對雙位數的切法不同
    #    （`f46`/`f47` 是 7 個 token，其餘 6 個）。CLAUDE.md 記過同一個陷阱。
    #    按長度分組批次，取各自的最後一個 token。
    # ⚠️ 取 **key 自己最後一個 token** 的 hidden，不是序列的最後一個 ——
    #    `| f41 存` 的最後一個 token 是「存」，對所有 key 都相同，
    #    表示會被它主導：實測兩兩 |cos| 中位數 0.995、最大 1.000，
    #    不同 key 幾乎無法區分，G2c 會因此失敗且看起來像「泛化不了」。
    out = {}
    by_len = defaultdict(list)
    enc, kpos = {}, {}
    head = len(tok(tok.bos_token + "| ", add_special_tokens=False).input_ids)
    for k in keys:
        enc[k] = tok(tok.bos_token + R.render_write_view(k),
                     add_special_tokens=False).input_ids
        kpos[k] = head + len(tok(k, add_special_tokens=False).input_ids) - 1
    for k, v in enc.items():
        by_len[len(v)].append(k)
    for n, group in by_len.items():
        for c in range(0, len(group), bs):
            chunk = group[c:c + bs]
            ids = torch.tensor([enc[k] for k in chunk]).to(DEVICE)
            h, _, _, _ = model.model(ids, num_loops=ARCH["num_loops"])
            for j, k in enumerate(chunk):
                out[k] = h[j, kpos[k]].clone()
    return out


@torch.no_grad()
def precompute_core_cues(model, tok, samples, bs=64):
    """把 core_hidden 的 cue **預先算好**。

    cue 只依賴樣本本身（retrieval view），**與 pool/missing 無關**，
    所以不必每個 step 重算。先前每 step 跑 64 次 core 前向，慢到不可用。
    同 k 的 retrieval view 結構相同 → 可直接批次。
    """
    out, by_k = {}, defaultdict(list)
    for i, s in enumerate(samples):
        by_k[s.k].append(i)
    for k, idxs in by_k.items():
        _, pos = R.retrieval_view_key_positions(tok, samples[idxs[0]])
        pos = pos.to(DEVICE)
        for c in range(0, len(idxs), bs):
            chunk = idxs[c:c + bs]
            ids = torch.stack([torch.tensor(
                tok(tok.bos_token + R.render_retrieval_view(samples[i]),
                    add_special_tokens=False).input_ids) for i in chunk]).to(DEVICE)
            h, _, _, _ = model.model(ids, num_loops=ARCH["num_loops"])
            sel = h[:, pos]
            for j, i in enumerate(chunk):
                out[i] = sel[j].clone()
    return out


@torch.no_grad()
def core_hidden_cue(model, tok, s):
    """G2b：query 來自**凍結 core** 在 retrieval view 上、每個 key 位置的 hidden。

    retrieval view 是**獨立的 out-of-band 序列**（`| x=STATE | f3 f0 ... 求?`），
    delivery 的 prompt 仍是 5 個 dot、完全不動。
    不能取 carrier span 的 hidden —— 那裡全是 dot，沒有 key 資訊。
    """
    view, pos = R.retrieval_view_key_positions(tok, s)
    ids = tok(tok.bos_token + view, add_special_tokens=False,
              return_tensors="pt").input_ids.to(DEVICE)
    h, _, _, _ = model.model(ids, num_loops=ARCH["num_loops"])
    return h[0, pos.to(DEVICE)]                                       # (k, H)


@torch.no_grad()
def key_span_embeddings(tok, emb, keys, max_span=None):
    """每個 key 的 **canonical span token embedding**（含前導空白）+ padding mask。

    G2c 的 tied encoder 兩側都吃這個 —— 同一份 span、同一套切法。
    用 embedding 而非 write-view hidden 是量測結果（`g2c_identity_gate.py`）：
    hidden 的身分方向條件數 1.65e-4，比 embedding 的 1.09e-1 差 660 倍。
    """
    from model.memory_module import TiedAddressEncoder as _T
    max_span = max_span or _T.MAX_SPAN
    E, M = {}, {}
    for k in keys:
        ids = R.canonical_key_ids(tok, k)
        assert len(ids) <= max_span, f"{k} 的 span {len(ids)} 超過 MAX_SPAN {max_span}"
        e = emb(torch.tensor(ids, device=DEVICE))
        pad = max_span - len(ids)
        E[k] = torch.cat([e, e.new_zeros(pad, e.size(-1))]) if pad else e
        M[k] = torch.tensor([True] * len(ids) + [False] * pad, device=DEVICE)
    return E, M


def make_item(s, rng, p_missing, tok, emb, cue_cache, model=None, qsrc="key_emb",
              precomp=None, idx=None, keys=None, span=None):
    pool, lat, chain, tgt, hit, dropped = R.build_pool(
        s, rng, missing=rng.random() < p_missing, keys=keys)
    if span is not None:
        # G2c：address 與 query 都留成 span，交給 tied encoder 在圖內算（可回傳梯度）
        sp_e, sp_m = span
        return dict(pool=pool,
                    pool_span=torch.stack([sp_e[g] for g in pool]),
                    pool_mask=torch.stack([sp_m[g] for g in pool]),
                    q_span=torch.stack([sp_e[g] for g in chain]),
                    q_mask=torch.stack([sp_m[g] for g in chain]),
                    lats=torch.stack([lat[g] for g in pool]).to(DEVICE),
                    tgt=torch.tensor(tgt, device=DEVICE),
                    hit=torch.tensor(hit, device=DEVICE), sample=s, dropped=dropped)
    if qsrc == "core_hidden":
        cues = precomp[idx] if precomp is not None and idx in precomp \
            else core_hidden_cue(model, tok, s)
    else:
        cues = torch.stack([cue_cache[g] for g in chain])             # (k, H)
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
    ap.add_argument("--stage", default="G2ab", choices=["G2ab", "G2c"],
                    help="G2c：tied encoder 產生 address，train/cal/test 的 key 身分不交")
    ap.add_argument("--query-source", default="key_emb", choices=["key_emb", "core_hidden"],
                    help="key_emb=G2a（凍結 token embedding）；core_hidden=G2b（凍結 core 的 hidden）")
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
    g2c = a.stage == "G2c"
    enc = span = None
    if g2c:
        # G2c：address **不再是固定正交 bank**，而是 tied encoder 從 canonical span 算出。
        # 於是 distractor 的 address 彼此非正交（實測跨組 max|cos| 0.964），
        # missing 判定要在這種條件下仍站得住 —— 那才是 G2c 真正新增的證據。
        enc = TiedAddressEncoder(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE)
        span = key_span_embeddings(tok, emb, R.ALL_KEYS)
    trainable = list(ret.parameters()) + (list(enc.parameters()) if g2c else [])
    n_ret = sum(p.numel() for p in trainable)
    print(f"  core+zdelta 凍結；**只訓 {'retriever + tied encoder' if g2c else 'retriever'}** "
          f"{n_ret/1e6:.3f}M"
          f"   address {'由 tied encoder 產生（非正交）' if g2c else '固定正交、不可訓'}")
    print(f"  query source = {a.query_source}"
          f"{'（G2a：凍結 token embedding）' if a.query_source == 'key_emb' else '（G2b：凍結 core 在 out-of-band retrieval view 的 hidden）'}")
    if g2c:
        print(f"  G2c：train/cal/test 的 **key 身分完全不交** "
              f"({len(R.KEYS_TRAIN)}/{len(R.KEYS_CAL)}/{len(R.KEYS_TEST)})；"
              f"query 與 address 走**同一個 encoder、同一份 canonical span**")
        print(f"  措辭預鎖：自匹配本身容易；G2c 新增的是**未見身分的共享 address 建構**"
              f"與**非正交 distractor 下的 missing calibration**，不是 semantic retrieval")

    # ⚠️ **三分**：train / calibration / **untouched test**（Codex）。
    #    先前 threshold 在 val 上掃描、又在同一批 val 報 hit/miss 100%，
    #    那是 calibration → evaluation 重用，support 的 100% 不算數。
    # G2c 再加一層：**key 身分**也三分且不交，才測得到未見身分的泛化。
    kt, kc, kv = (R.KEYS_TRAIN, R.KEYS_CAL, R.KEYS_TEST) if g2c else (None, None, None)
    tr = R.build_dataset(a.max_k, a.train_per_k, a.seed, keys=kt)
    seen = {s.delivery_id for s in tr}
    cal = R.build_dataset(a.max_k, a.val_per_k, a.seed + 999, exclude=seen, keys=kc)
    seen |= {s.delivery_id for s in cal}
    te = R.build_dataset(a.max_k, a.val_per_k, a.seed + 1777, exclude=seen, keys=kv)
    assert not ({s.delivery_id for s in cal} & {s.delivery_id for s in te})
    if g2c:
        used = lambda ds: {g for s in ds for g in s.chain}
        assert not (used(tr) & used(te)) and not (used(cal) & used(te)), "key 身分洩漏"
        print(f"  key 身分不交已驗：train∩test = ∅、cal∩test = ∅")
    print(f"  train {len(tr)} / calib {len(cal)} / test {len(te)}   "
          f"{R.delivery_checksum(tr)} / {R.delivery_checksum(cal)} / {R.delivery_checksum(te)}")
    by_k = defaultdict(list)
    for i, s in enumerate(tr):
        by_k[s.k].append(i)
    ks = sorted(by_k)

    pre_tr = pre_cal = pre_te = None
    if a.query_source == "core_hidden":
        t_pre = time.time()
        pre_tr = precompute_core_cues(m, tok, tr)
        pre_cal = precompute_core_cues(m, tok, cal)
        pre_te = precompute_core_cues(m, tok, te)
        print(f"  core cue 預算完成（{len(pre_tr)}+{len(pre_cal)}+{len(pre_te)} 筆，"
              f"{time.time()-t_pre:.0f}s）—— cue 與 pool 無關，故只算一次")

    def run(items):
        """一批 item → (logits, support)。G2c 走 tied 路徑，query 與 address 同源。"""
        if g2c:
            addrs = enc(torch.stack([it["pool_span"] for it in items]).flatten(0, 1),
                        torch.stack([it["pool_mask"] for it in items]).flatten(0, 1))
            addrs = addrs.view(len(items), R.POOL_SIZE, -1)
            q = enc(torch.stack([it["q_span"] for it in items]).flatten(0, 1),
                    torch.stack([it["q_mask"] for it in items]).flatten(0, 1))
            q = q.view(len(items), items[0]["q_span"].size(0), -1)
            return ret(None, addrs, query=q)
        return ret(torch.stack([it["cues"] for it in items]),
                   torch.stack([it["addrs"] for it in items]))

    opt = torch.optim.AdamW(trainable, lr=a.lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps, pct_start=0.05)
    print(f"\n{'=' * 58}\n  G2a retrieve — {a.steps} 步（throttle {a.throttle:.0%}）\n{'=' * 58}",
          flush=True)

    rng = random.Random(a.seed)
    ret.train(); t0 = time.time()
    for step in range(1, a.steps + 1):
        kk = ks[step % len(ks)]
        idx = [by_k[kk][rng.randrange(len(by_k[kk]))] for _ in range(a.batch_size)]
        items = [make_item(tr[i], rng, a.p_missing, tok, emb, cue_cache, m, a.query_source,
                           pre_tr, i, keys=kt, span=span) for i in idx]
        tgt = torch.stack([it["tgt"] for it in items])
        hit = torch.stack([it["hit"] for it in items]).float()
        s0 = time.time()
        logits, sup = run(items)
        # 有 hit 的位置才算 CE；hit/miss **直接監督**（§4.12/§4.20：只靠答案梯度不夠）
        mask = tgt >= 0
        ce = F.cross_entropy(logits[mask], tgt[mask]) if mask.any() else logits.sum() * 0
        bce = F.binary_cross_entropy_with_logits(sup, hit)
        loss = ce + bce
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(trainable, 1.0)
        opt.step(); sched.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time() - s0) * (1 / a.throttle - 1))
        if step % 200 == 0:
            print(f"  step {step:5d}/{a.steps} ce={ce.item():.4f} bce={bce.item():.4f} "
                  f"{step/(time.time()-t0):.1f} it/s", flush=True)

    # ---- 校準 threshold：**在 val 上校一次就固定**，不可每次挑（Codex）----
    ret.eval()
    if g2c:
        # encoder 訓完 **freeze**，之後才 commit test 的 address（Codex）。
        enc.eval()
        for p in enc.parameters():
            p.requires_grad_(False)
    cal_rng = random.Random(a.seed + 7)
    sups, hits = [], []
    with torch.no_grad():
        for ci, s in enumerate(cal):
            it = make_item(s, cal_rng, a.p_missing, tok, emb, cue_cache, m, a.query_source,
                           pre_cal, ci, keys=kc, span=span)
            _, sup = run([it])
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
    imp = []
    stat = defaultdict(lambda: defaultdict(int))
    exec_given_correct = [0, 0]
    ans_stat = defaultdict(int)          # R4 式拆解，不給混合數字
    with torch.no_grad():
        for ti, s in enumerate(te):      # **untouched test**
            it = make_item(s, ev_rng, a.p_missing, tok, emb, cue_cache, m, a.query_source,
                           pre_te, ti, keys=kv, span=span)
            logits, sup = run([it])
            pred = logits[0].argmax(-1)
            if g2c:
                # nearest-impostor cosine：與**非目標** address 的最大相似度。
                # address 不再正交（跨組 max|cos| 0.964），這才是 missing 判定的真實難度。
                cos = logits[0] / ret.log_temp.exp()
                for j in range(cos.size(0)):
                    row = cos[j].clone()
                    if it["tgt"][j] >= 0:
                        row[it["tgt"][j]] = -2.0          # 排掉自己
                    imp.append((float(row.max()),
                                bool((sup[0][j] > best_t).item() == bool(it["hit"][j]))))
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
                # missing 題的「ordered」定義為**正確拒絕**該 step／整題
                stat[k]["miss_items"] += 1
                ans_stat["miss_n"] += 1
                ok_rej = bool((~pred_hit[~gold_hit]).all())
                stat[k]["miss_abstain"] += int(ok_rej)
                ans_stat["R_abstain"] += int(ok_rej)
                ans_stat["halluc"] += int(not ok_rej)
                continue
            ans_stat["ans_n"] += 1
            ans_stat["false_abstain"] += int((~pred_hit).any())
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
            ans_stat["A_ans"] += int(good)
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
    an, mn = max(ans_stat["ans_n"], 1), max(ans_stat["miss_n"], 1)
    print(f"\n  R4 式拆解（**untouched test**，不給混合數字）：")
    print(f"    A_ans         資料齊全時答對     {ans_stat['A_ans']/an:6.1%}  (n={ans_stat['ans_n']})")
    print(f"    R_abstain     缺資料時正確拒絕   {ans_stat['R_abstain']/mn:6.1%}  (n={ans_stat['miss_n']})")
    print(f"    false_abstain 資料齊全卻拒絕     {ans_stat['false_abstain']/an:6.1%}")
    print(f"    halluc        缺資料卻照樣取回   {ans_stat['halluc']/mn:6.1%}")
    egc = exec_given_correct[0] / max(exec_given_correct[1], 1)
    print(f"\n  executor | retrieval correct = {egc:.1%}  "
          f"(n={exec_given_correct[1]})   ← 實際 retriever 全對的子集")
    print(f"  oracle-retrieval ceiling（render gate）= 99.0%")

    bins_out = None
    if g2c:
        # ---- unseen 身分的 address 品質（Codex：訓完在 untouched test 報）----
        with torch.no_grad():
            sp_e, sp_m = span
            A = enc(torch.stack([sp_e[k] for k in R.KEYS_TEST]),
                    torch.stack([sp_m[k] for k in R.KEYS_TEST]))
            C = (A @ A.T - torch.eye(len(A), device=A.device)).abs()
        print(f"\n  未見身分的 address（test {len(R.KEYS_TEST)} 個 key，encoder 已凍結）：")
        print(f"    兩兩 |cos|  min {C.min():.3f}  中位數 {C.median():.3f}  **max {C.max():.3f}**")
        print(f"    輸入端基準（emb 位置加權，g2c_identity_artifact）跨組 max 0.964")
        print(f"    ⚠️ learned encoder 若把未見 key 壓成碰撞，算**模型 fail**，不重抽 key")
        # 按 nearest-impostor cosine 分箱報 hit/miss —— 難的箱才是結論所在
        edges = [-1.0, 0.3, 0.6, 0.8, 0.9, 1.01]
        bins_out = []
        print(f"\n    {'nearest-impostor cos':>22s} {'n':>6s} {'hit/miss acc':>13s}")
        for lo_, hi_ in zip(edges[:-1], edges[1:]):
            sel = [ok for c, ok in imp if lo_ <= c < hi_]
            bins_out.append({"lo": lo_, "hi": hi_, "n": len(sel),
                             "acc": (sum(sel) / len(sel)) if sel else None})
            if sel:
                print(f"    {f'[{lo_:+.2f}, {hi_:+.2f})':>22s} {len(sel):>6d} "
                      f"{sum(sel)/len(sel):12.1%}")
        print(f"    imp margin：min {min(c for c, _ in imp):+.3f}  "
              f"max {max(c for c, _ in imp):+.3f}")
    print(f"  {(time.time()-t0)/60:.1f} min")

    fp = {"stage": "G2c" if g2c else ("G2b" if a.query_source == "core_hidden" else "G2a"),
          "keys_train": len(R.KEYS_TRAIN) if g2c else None,
          "keys_test": R.KEYS_TEST if g2c else None,
          "query_source": a.query_source, "steps": a.steps, "lr": a.lr, "bs": a.batch_size, "seed": a.seed,
          "p_missing": a.p_missing, "pool": R.POOL_SIZE, "retriever_params": n_ret,
          "threshold": best_t, "core_zdelta": CORE_ZD, "smoke": a.smoke,
          "train_checksum": R.delivery_checksum(tr),
          "calib_checksum": R.delivery_checksum(cal),
          "test_checksum": R.delivery_checksum(te)}
    h = hashlib.sha256(json.dumps(fp, sort_keys=True, default=str).encode()).hexdigest()[:10]
    tag = "g2c" if g2c else ("g2b" if a.query_source == "core_hidden" else "g2a")
    out = os.path.join(HERE, f"results_{tag}{'_smoke' if a.smoke else ''}.json")
    json.dump({**fp, "per_k": {str(k): dict(v) for k, v in stat.items()},
               "r4": dict(ans_stat), "impostor_bins": bins_out,
               "executor_given_correct": egc, "egc_n": exec_given_correct[1]},
              open(out, "w"), indent=2, ensure_ascii=False)
    torch.save({"retriever": ret.state_dict(),
                **({"encoder": enc.state_dict()} if g2c else {})},
               os.path.join(HERE, f"{tag}_retriever_{h}.pth"))
    print(f"  -> {os.path.basename(out)}")


if __name__ == "__main__":
    main()
