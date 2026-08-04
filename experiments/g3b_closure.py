"""G3b：**closed-world component closure** —— 全凍結、零訓練的整合驗證。

三個元件各自通過了：

    delivery  G1  `zdelta`                     99.0%
    retrieve  G2b 固定 key 的 retriever/support 100%
    write     G3a v2 結構化 writer              99.0%（未見置換）

G3b 只問一件事：**把它們串起來，還成立嗎？** 所以 **primary 不再訓練任何東西**
（Codex）—— 全部載入凍結權重，用全新的固定 episodes 直接串

    event → writer → store.commit → retrieve → store.read → delivery → answer

⚠️ **address 仍由已知的 key 規則提供**（`address_vector`）——
   **write-address formation 尚未測試**，這一版不宣稱它。
⚠️ 通過只稱 **closed-world component closure**，
   **不稱** open-set，**也不稱**持久學習閉環。
⚠️ 初版**刻意排除 overwrite / reconsolidation**：每個 key 最多寫一次、
   每個 episode 清空 store。更新與 stale snapshot 留給 G3c，不在這關混入。

**key universe 限定在 canonical span = 2 token 的 key**：G3a 的 writer 對未見
key 完全泛化，但只在 2-token 上（3-token 把 value span 從 5 推到 6，掉到 58–96%）。
那是**已知且已量化的 writer 限制**，讓它混進整合測試只會把失敗歸因搞糊。
"""
import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
import g3a_train as G3
from g1_oracle_inline import value_positions
from g1_teacher_kv import greedy_override
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from g2_train import core_hidden_cue
from g3a_train import CORE_ZD, Writer, precompute_events
from model.memory_module import (ADDR_DIM, LATENT_DIM, PERM_N, LatentStore, MemoryEntry,
                                 Retriever, address_vector, perm_to_latent)
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]


# 由呼叫端設定：這些 key 的內容一律用 **oracle latent** commit，不經 learned writer。
# 用途是把「已知的 writer span OOD」擋在 pool-size 這一軸之外（Codex）——
# 它們的 address 仍是固定正交的，而且從不該被選中（下方會回報）。
ORACLE_CONTENT_KEYS = set()


def two_token_keys(tok, keys):
    return [k for k in keys if len(R.canonical_key_ids(tok, k)) == 2]


def make_episode(k, rng, query_keys, distractors, perms, pool_size, p_omit):
    """一個 episode：樣本 + pool + 每個 pool key 的置換 + 是否漏寫。

    `omitted` 是**沒有被寫進 store 的 required key** —— missing 由
    **實際的 store membership** 驅動，不是靠 pool 少一條（基數恆定）。
    """
    present = sorted(rng.sample(range(len(query_keys)), 2))
    defs, seen = {}, set()
    for i in present:
        while True:
            q = list(perms[rng.randrange(len(perms))])
            if tuple(q) not in seen:
                seen.add(tuple(q)); defs[query_keys[i]] = q; break
    order = [query_keys[i] for i in present]; rng.shuffle(order)
    state = list(range(PERM_N)); rng.shuffle(state)
    chain = [query_keys[rng.choice(present)] for _ in range(k)]
    st = list(state)
    for g in chain:
        st = [st[defs[g][i]] for i in range(PERM_N)]
    s = R.CanonicalSample(k=k, defs=defs, present=order, state=state,
                          chain=chain, answer=st)

    # ⚠️ **pool 由 store 的實際內容決定**，不是先建 pool 再漏寫。
    #    retriever 拿到的 address bank 就是 store 裡有什麼；沒寫進去的 key
    #    根本不會出現在 bank 裡。先建 pool 再漏寫會在 bank 留下一個
    #    **沒有內容的懸空 address**——那是另一個情境（dangling address），
    #    G2b 沒訓練過，也不是這關要測的東西。
    required = list(dict.fromkeys(chain))
    omitted = required[rng.randrange(len(required))] if rng.random() < p_omit else None
    written = [x for x in required if x != omitted]
    others = [x for x in distractors if x not in required]
    rng.shuffle(others)
    pool = written + others[:pool_size - len(written)]    # 基數恆為 pool_size
    assert len(pool) == pool_size and omitted not in pool
    rng.shuffle(pool)
    pool_perm = dict(defs)
    for x in pool:
        if x not in pool_perm:
            q = list(perms[rng.randrange(len(perms))]); pool_perm[x] = q
    return s, pool, pool_perm, omitted


@torch.no_grad()
def run_episode(m, tok, dl, writer, ret, ev, s, pool, pool_perm, omitted, thr,
                use_writer, use_retrieval, roundtrip, checks):
    """跑完一個 episode，回傳每一環的正確性。"""
    # ---- write：每個 key 最多寫一次；omitted 的那個**根本不寫** ----
    store = LatentStore()
    formed = {}
    for x in pool:                       # pool 已經是「實際寫進去的那些」
        p = pool_perm[x]
        z = (writer(ev[(x, tuple(p))].unsqueeze(0))[0]
             if (use_writer and x not in ORACLE_CONTENT_KEYS)
             else perm_to_latent(p).to(DEVICE))
        formed[x] = z
        # store 以**符號**為 address（LatentStore 的 dict key）；
        # `address_vector` 只給 retriever 的 address bank 用，兩者不可混。
        store.commit([MemoryEntry(x, z, {"key": x})])
    assert len(store) == len(pool)

    # ---- store roundtrip 逐位 assert（Codex）----
    if checks is not None:
        for x, z in formed.items():
            back = store.read([x])[0]
            checks["n"] += 1
            checks["shape"] += int(back.latent.shape == z.shape)
            checks["maxdiff"] = max(
                checks["maxdiff"],
                float((back.latent.to(z.device) - z).abs().max()))
            checks["detached"] += int(not back.latent.requires_grad)
            checks["addr"] += int(back.address == x)

    # ---- formation exact（**unconditional**，不以檢索正確為條件）----
    form_ok = []
    for g in s.chain:
        if g == omitted:
            form_ok.append(None)                     # 沒寫就沒有 formation 可言
        else:
            form_ok.append(bool((formed[g].view(PERM_N, PERM_N).argmax(-1).cpu()
                                 == torch.tensor(pool_perm[g])).all()))

    # ---- retrieve ----
    addrs = torch.stack([address_vector(x) for x in pool]).to(DEVICE)
    tgt = [pool.index(g) if g != omitted else -1 for g in s.chain]
    if use_retrieval:
        cues = core_hidden_cue(m, tok, s)
        logits, sup = ret(cues.unsqueeze(0), addrs.unsqueeze(0))
        pred = logits[0].argmax(-1).tolist()
        pred_hit = (sup[0] > thr).tolist()
    else:
        pred = [t if t >= 0 else 0 for t in tgt]
        pred_hit = [t >= 0 for t in tgt]
    addr_ok = [(p == t and h) or (t < 0 and not h)
               for p, t, h in zip(pred, tgt, pred_hit)]

    # ---- 取回的內容忠實度（**以 address 正確為條件**）----
    content_ok, lat_steps = [], []
    for j, g in enumerate(s.chain):
        if not addr_ok[j] or tgt[j] < 0:
            lat_steps.append(torch.zeros(LATENT_DIM, device=DEVICE))
            content_ok.append(None)
            continue
        if roundtrip:
            e = store.read([pool[pred[j]]])[0]
            assert e is not None and e.address == pool[pred[j]], "address↔content 綁定破裂"
            z = e.latent.to(DEVICE)
        else:
            z = formed[pool[pred[j]]]                # 繞過 store，直接交付
        lat_steps.append(z)
        content_ok.append(bool((z.view(PERM_N, PERM_N).argmax(-1).cpu()
                                == torch.tensor(pool_perm[g])).all()))

    # ---- deliver + answer ----
    if omitted is not None:
        # 漏寫時**不作答**才是正確行為；answer 的正確性只在有作答時才有意義
        return dict(form_ok=form_ok, addr_ok=addr_ok, content_ok=content_ok,
                    abstained=bool(any(not h for h in pred_hit)), e2e=None)
    if any(not h for h in pred_hit):
        return dict(form_ok=form_ok, addr_ok=addr_ok, content_ok=content_ok,
                    abstained=True, e2e=False)
    lat = torch.stack(lat_steps).unsqueeze(0)
    _, b_ids, pos = value_positions(tok, s)
    pos = pos.to(DEVICE)
    got = greedy_override(m, tok, b_ids, G3._mk_fn(dl, lat, pos), ARCH["num_loops"])
    return dict(form_ok=form_ok, addr_ok=addr_ok, content_ok=content_ok,
                abstained=False, e2e=(got == R.render_L0(s)[1]),
                picked=[pool[p] for p, h in zip(pred, pred_hit) if h])


def rate(k, n):
    return f"{k/n:6.1%} (n={n})" if n else "     — (n=0)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--writer-ckpt", default="g3a_writer_6820855741.pth")
    ap.add_argument("--retriever-ckpt", default=None)
    ap.add_argument("--g2b-results", default="results_g2b.json")
    ap.add_argument("--per-k", type=int, default=100)
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--p-omit", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--stress", action="store_true",
                    help="3-token span-shift stress stratum（**exploratory/diagnostic**，"
                         "不影響 primary gate；query key 改用 3-token）")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    blob = torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")
    m.load_state_dict(blob["model"], strict=False)
    dl = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                       os.path.join(HERE, "g1_renderer_artifact.json"))
    dl.load_state_dict(blob["delivery"]); dl.eval()
    writer = Writer(BACKBONE["hidden_size"], out_param="rowsoftmax").to(DEVICE).eval()
    writer.load_state_dict(torch.load(os.path.join(HERE, a.writer_ckpt),
                                      map_location="cpu"))
    g2b = json.load(open(os.path.join(HERE, a.g2b_results)))
    thr = g2b["threshold"]
    ck = a.retriever_ckpt or sorted(
        [f for f in os.listdir(HERE) if f.startswith("g2b_retriever_")],
        key=lambda f: os.path.getmtime(os.path.join(HERE, f)))[-1]
    ret = Retriever(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    sd = torch.load(os.path.join(HERE, ck), map_location="cpu")
    ret.load_state_dict(sd["retriever"] if "retriever" in sd else sd)
    for p in list(m.parameters()) + list(dl.parameters()) + \
            list(writer.parameters()) + list(ret.parameters()):
        p.requires_grad_(False)

    # ---- key universe：**「canonical 2-token」∩「G2b 實際訓練過的身分」**（Codex）----
    # G2b 的 query 只在 R.KEYS = f0..f3 上訓練過（G2ab 走 keys=None → make_canonical
    # 用 R.KEYS）；distractor 則來自 ALL_KEYS。不能只因 writer 對 f4..f11 泛化
    # 就把 retriever 沒學過的 key 算進 closed-world。
    # ⚠️ address bank 只有 ADDR_DIM=32 個正交向量，所以 f32 以上**產不出 address**——
    #    G2b 當初實際看過的 distractor 也就只有 f0..f31。這是交集條件的一部分，
    #    不是實作細節：拿 f32+ 當 distractor 會直接超出已訓練的 address 空間。
    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = two_token_keys(tok, addressable)
    query_keys = [k for k in R.KEYS if k in two]
    distractors = two
    if a.stress:
        # ⚠️ **exploratory / diagnostic，不是 confirmatory**：
        #    3-token 的 query key 同時讓 **writer**（value span 從 5 移到 6）
        #    與 **retriever**（G2b 只在 f0..f3 上訓練過 query）離開分布 ——
        #    這兩個混淆**無法在本設計裡分離**。formation 一欄可單獨歸因 writer，
        #    end-to-end 不行。
        query_keys = [k for k in addressable if k not in two]
        assert len(query_keys) >= 2, f"可定址的 3-token key 只有 {len(query_keys)} 個"
    if not a.stress:
        assert query_keys == R.KEYS, "G2b 訓練過的 query key 必須全是 2-token"
    # 漏寫後仍要能補滿 pool，且**不得靠基數洩漏** missing
    assert len(distractors) - 2 >= a.pool - 1, "distractor 不足以在漏寫後補滿 pool"
    _, perms = R.perm_splits()                      # **未見置換**（G3a 的 val）
    G3.WRITE_KEYS = sorted(set(query_keys) | set(distractors))
    ev = precompute_events(m, tok, perms)

    print(f"  **全凍結、零訓練**：core + zdelta + writer({a.writer_ckpt[:20]}) "
          f"+ retriever({ck[:20]})")
    print(f"  threshold {thr:+.3f} 沿 G2b 凍結、**不重校**")
    if a.stress:
        print(f"  ⚠️ **STRESS STRATUM（exploratory/diagnostic，不影響 primary gate）**")
        print(f"  query key = {query_keys}（**3-token**）—— writer 與 retriever "
              f"**同時**離開分布，兩個混淆無法分離；只有 formation 一欄可單獨歸因 writer")
    else:
        print(f"  **query key = {query_keys}**（2-token ∩ G2b 訓練過的身分）；")
    print(f"  distractor = 2-token {len(distractors)} 個；pool={a.pool}")
    print(f"  置換 = G3a 的 **val split（{len(perms)} 個未見置換）**")
    print(f"  address 仍由已知 key 規則提供 —— **write-address formation 未測**")
    print(f"  每 key 最多寫一次、每 episode 清 store；**排除 overwrite/reconsolidation**\n")

    rng = random.Random(a.seed)
    eps = [make_episode(k, rng, query_keys, distractors, perms, a.pool, a.p_omit)
           for k in range(1, a.max_k + 1) for _ in range(a.per_k)]
    n_omit = sum(1 for e in eps if e[3] is not None)
    # pool 基數恆定 —— 漏寫**不得**表現成 pool 少一條
    assert all(len(p) == a.pool for _, p, _, _ in eps), "pool 基數不恆定，missing 會被基數洩漏"
    ident = {"query_keys": query_keys, "distractors": distractors,
             "n_perm": len(perms), "pool": a.pool, "p_omit": a.p_omit, "seed": a.seed}
    import hashlib as _h
    ident["checksum"] = _h.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:12]
    json.dump(ident, open(os.path.join(HERE, "g3b_identity_prereg.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  episodes {len(eps)}（其中漏寫 {n_omit}，{n_omit/len(eps):.1%}）"
          f"   身分已凍結 checksum {ident['checksum']}\n")

    # ---- 2×2 介入 + roundtrip 對照（同一批 episodes）----
    cells = [("oracle writer", "oracle retr", False, False, True),
             ("oracle writer", "learned retr", False, True, True),
             ("**learned writer**", "oracle retr", True, False, True),
             ("**learned writer**", "**learned retr**", True, True, True),
             ("learned writer", "learned retr（**直送、不經 store**）", True, True, False)]
    checks = {"n": 0, "shape": 0, "maxdiff": 0.0, "detached": 0, "addr": 0}
    out = {}
    print(f"  {'writer':<20s} {'retrieval':<34s} {'end-to-end':>12s} {'棄答正確':>10s}")
    for wname, rname, uw, ur, rt in cells:
        e2e = [0, 0]; ab = [0, 0]
        for s, pool, pp, om in eps:
            r = run_episode(m, tok, dl, writer, ret, ev, s, pool, pp, om, thr,
                            uw, ur, rt, checks if (uw and ur and rt) else None)
            if om is not None:
                ab[1] += 1; ab[0] += int(r["abstained"])
            else:
                e2e[1] += 1; e2e[0] += int(bool(r["e2e"]))
        print(f"  {wname:<20s} {rname:<34s} {e2e[0]/max(e2e[1],1):11.1%} "
              f"{ab[0]/max(ab[1],1):9.1%}")
        out[f"{wname}|{rname}"] = {"e2e": e2e[0] / max(e2e[1], 1), "e2e_n": e2e[1],
                                   "abstain": ab[0] / max(ab[1], 1), "abstain_n": ab[1]}

    # ---- 完整指標鏈（primary 條件：learned × learned × roundtrip）----
    f_ok = [0, 0]; a_ok = [0, 0]; c_ok = [0, 0]; x_ok = [0, 0]; e_ok = [0, 0]
    r4 = defaultdict(int)
    for s, pool, pp, om in eps:
        r = run_episode(m, tok, dl, writer, ret, ev, s, pool, pp, om, thr,
                        True, True, True, None)
        for v in r["form_ok"]:
            if v is not None:
                f_ok[1] += 1; f_ok[0] += int(v)
        for v in r["addr_ok"]:
            a_ok[1] += 1; a_ok[0] += int(v)
        for v in r["content_ok"]:
            if v is not None:
                c_ok[1] += 1; c_ok[0] += int(v)
        if om is not None:
            r4["miss_n"] += 1
            r4["R_abstain"] += int(r["abstained"]); r4["halluc"] += int(not r["abstained"])
            continue
        r4["ans_n"] += 1
        r4["false_abstain"] += int(r["abstained"])
        e_ok[1] += 1; e_ok[0] += int(bool(r["e2e"]))
        if all(r["addr_ok"]) and all(v for v in r["content_ok"] if v is not None):
            x_ok[1] += 1; x_ok[0] += int(bool(r["e2e"]))

    print(f"\n  **指標鏈**（learned writer × learned retrieval × store roundtrip）")
    print(f"    1. writer formation exact（**unconditional**）  {rate(*f_ok)}")
    print(f"    2. address retrieval exact                     {rate(*a_ok)}")
    print(f"    3. retrieved-content fidelity | address 正確    {rate(*c_ok)}")
    print(f"    4. executor | address + content 皆正確          {rate(*x_ok)}")
    print(f"    5. end-to-end                                  {rate(*e_ok)}")
    mn, an = max(r4["miss_n"], 1), max(r4["ans_n"], 1)
    print(f"\n  R4 拆解（漏寫由**實際 store membership** 驅動）：")
    print(f"    R_abstain     {r4['R_abstain']/mn:6.1%} (n={r4['miss_n']})")
    print(f"    halluc        {r4['halluc']/mn:6.1%}")
    print(f"    false_abstain {r4['false_abstain']/an:6.1%} (n={r4['ans_n']})")
    print(f"\n  store roundtrip 逐位 assert：{checks['n']} 次   "
          f"shape 一致 {checks['shape']}/{checks['n']}   "
          f"max|diff| {checks['maxdiff']:.2e}   detach {checks['detached']}/{checks['n']}   "
          f"address↔content 綁定 {checks['addr']}/{checks['n']}")
    print(f"\n  ⚠️ 這關的宣稱是 **in-distribution 2-token closed-world component closure**；"
          f"\n     3-token 的 span-shift robustness 由 --stress 另外報，兩者互不抵銷。"
          f"\n     不稱 open-set，也不稱持久學習閉環。")
    json.dump({"cells": out, "chain": {"formation": f_ok, "address": a_ok,
                                       "content": c_ok, "executor": x_ok, "e2e": e_ok},
               "r4": dict(r4), "store_checks": checks, "threshold": thr,
               "identity": ident, "pool": a.pool, "seed": a.seed,
               "scope": ("3-token span-shift stress (exploratory)" if a.stress
                         else "in-distribution 2-token closed-world component closure")},
              open(os.path.join(HERE, "results_g3b_stress.json" if a.stress
                                else "results_g3b.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g3b.json")


if __name__ == "__main__":
    main()
