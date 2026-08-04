"""G2c-cal-v2：**只重估 threshold**，模型一律凍結（§4.29 預先登記）。

G2c-v1 的結論是 identity retrieval 過、open-set abstention calibration 不過，
而診斷顯示 score 分得開（oracle 96–98%）、落差全在 threshold。
v2 只問一件事：**換一個更大、全新的 calibration 身分集合，threshold 能不能轉移？**

寫死的規則（跑之前定，跑之後不改）：
  - cal2 / test2 各 40 個身分，同一 generator 固定 seed IID 抽出，
    **與 train / 舊 cal / 舊 test 全不交**，數量事前定死、不因 cosine 分布重生。
  - encoder、retriever、support features **全部凍結**，不重訓、不改結構。
  - threshold 規則（非對稱代價：寫錯資料的代價 >> 多問一次）：
        在 cal2 上取 halluc 的**單側 95% Clopper–Pearson 上界 ≤ HALLUC_CAP**
        的所有 threshold —— **用上界不用點估計**，否則 5% 只是小樣本剛好少錯幾題；
        非空 → 其中**最小化 false_abstain**，同分取**較高（較保守）**的 threshold；
        **空集合 → 判 protocol infeasible / v2 fail，不加樣本、不放寬上限**。
  - **utility gate**（否則「永遠棄答」自動過）：test2 的 PASS 條件是
        **halluc 單側 95% 上界 ≤ 5% 且 false_abstain ≤ 5%**，
        另照報 R_abstain 與整體 hit/miss 的 CI。
  - test2 **只跑一次**，看過之後無論成敗停止 —— 不換 threshold、不擴 cal、不開第三版。
  - 5% 是 **research gate，不宣稱 production-safe**。

⚠️ 舊 test（f38..f47）在此**完全不使用** —— 它已被查看，拿它做 confirmatory
   就是在已知答案的集合上宣稱結果。
"""
import argparse
import glob
import json
import math
import os
import random
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_train import ARCH, BACKBONE, DEVICE
from g2_train import CORE_ZD, key_span_embeddings
from model.memory_module import ADDR_DIM, Retriever, TiedAddressEncoder
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
HALLUC_CAP = 0.05          # 事前定死。空集合則判 fail，**不放寬**。


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def cp_upper(k, n, alpha=0.05):
    """halluc 的**單側 95% Clopper–Pearson 上界**。

    用上界而非點估計，5% 才有統計含義（Codex）：n 很小時「剛好 0 次幻覺」
    完全不代表風險 ≤5%。零錯時上界 = 1-alpha^(1/n)，要壓到 5% 需要 n ≥ 59。
    以 P(X ≤ k | n, p) = alpha 對 p 二分求解，避免額外相依。
    """
    if n == 0:
        return 1.0
    if k >= n:
        return 1.0

    def cdf(p):
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))

    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


@torch.no_grad()
def collect(ret, enc, span, ds, keys, seed, p_missing):
    """回傳每個樣本的 (support 分數, gold hit, 是否 missing 題)。"""
    sp_e, sp_m = span
    rng = random.Random(seed)
    rows = []
    for s in ds:
        pool, lat, chain, tgt, hit, dropped = R.build_pool(
            s, rng, missing=rng.random() < p_missing, keys=keys)
        addrs = enc(torch.stack([sp_e[g] for g in pool]),
                    torch.stack([sp_m[g] for g in pool])).unsqueeze(0)
        q = enc(torch.stack([sp_e[g] for g in chain]),
                torch.stack([sp_m[g] for g in chain])).unsqueeze(0)
        _, sup = ret(None, addrs, query=q)
        rows.append((sup[0].cpu(), torch.tensor(hit)))
    return rows


def rates(rows, thr):
    """在給定 threshold 下算三個率 —— 與 g2_train 的 R4 拆解定義一致。"""
    miss_n = ab = ans_n = fa = 0
    for sup, hit in rows:
        pred = sup > thr
        if not bool(hit.all()):
            miss_n += 1
            ab += int(bool((~pred[~hit]).all()))
        else:
            ans_n += 1
            fa += int(bool((~pred).any()))
    return {"miss_n": miss_n, "R_abstain": ab, "halluc": miss_n - ab,
            "ans_n": ans_n, "false_abstain": fa}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=None)
    ap.add_argument("--results", default="results_g2c_seed42.json")
    ap.add_argument("--per-k", type=int, default=200)
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--p-missing", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    ck = a.ckpt or sorted(glob.glob(os.path.join(HERE, "g2c_retriever_*.pth")),
                          key=os.path.getmtime)[-1]

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    m.load_state_dict(torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")["model"],
                      strict=False)
    blob = torch.load(ck, map_location="cpu")
    ret = Retriever(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    enc = TiedAddressEncoder(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    ret.load_state_dict(blob["retriever"]); enc.load_state_dict(blob["encoder"])
    for p in list(ret.parameters()) + list(enc.parameters()):
        p.requires_grad_(False)

    span = key_span_embeddings(tok, m.model.embed_tokens, R.KEYS_CAL2 + R.KEYS_TEST2)
    cal2 = R.build_dataset(a.max_k, a.per_k, a.seed + 31337, keys=R.KEYS_CAL2)
    test2 = R.build_dataset(a.max_k, a.per_k, a.seed + 74747,
                            exclude={s.delivery_id for s in cal2}, keys=R.KEYS_TEST2)

    # ---- 身分與 checksum **先存**，再算 threshold ----
    pre = {"ckpt": os.path.basename(ck), "halluc_cap": HALLUC_CAP,
           "keys_cal2": R.KEYS_CAL2, "keys_test2": R.KEYS_TEST2,
           "cal2_checksum": R.delivery_checksum(cal2),
           "test2_checksum": R.delivery_checksum(test2),
           "n_cal2": len(cal2), "n_test2": len(test2),
           "rule": "cal2 上 halluc<=cap 的 threshold 中最小化 false_abstain；空集合判 fail"}
    json.dump(pre, open(os.path.join(HERE, "g2c_cal_v2_preregistered.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  {os.path.basename(ck)}   模型全凍結，**只重估 threshold**")
    print(f"  cal2 {len(cal2)}（{len(R.KEYS_CAL2)} 身分，{pre['cal2_checksum']}）"
          f" / test2 {len(test2)}（{len(R.KEYS_TEST2)} 身分，{pre['test2_checksum']}）")
    print(f"  舊 test（f38..f47）**完全不使用** —— 已被查看，不可作 confirmatory")
    print(f"  身分與 checksum 已存 g2c_cal_v2_preregistered.json（在算 threshold 之前）\n")

    rows_cal = collect(ret, enc, span, cal2, R.KEYS_CAL2, a.seed + 5, a.p_missing)
    sc = torch.cat([r[0] for r in rows_cal]).tolist()
    # 候選 threshold = 相鄰 score 的中點 —— 每個候選代表一個**預測不變的區間**，
    # 不是任意網格點（Codex）。
    uniq = sorted(set(sc))
    cand = ([uniq[0] - 1.0] + [(x + y) / 2 for x, y in zip(uniq[:-1], uniq[1:])]
            + [uniq[-1] + 1.0])

    n_miss = rates(rows_cal, cand[0])["miss_n"]
    floor_ub = cp_upper(0, n_miss)
    print(f"  cal2 的 missing 樣本 n={n_miss}；即使**零次幻覺**，"
          f"單側 95% 上界也是 {floor_ub:.1%}")
    if floor_ub > HALLUC_CAP:
        print(f"  ❌ **v2 FAIL（protocol infeasible）** —— cal2 的 missing 樣本太少，"
              f"連零錯的 95% 上界都 > {HALLUC_CAP:.0%}。"
              f"\n     依預先登記，**不加樣本、不放寬上限**，停止。")
        json.dump({**pre, "verdict": "FAIL_protocol_infeasible",
                   "n_miss_cal2": n_miss, "zero_error_ub": floor_ub},
                  open(os.path.join(HERE, "results_g2c_cal_v2.json"), "w"),
                  indent=2, ensure_ascii=False)
        return

    feasible = []
    for t in cand:
        r = rates(rows_cal, t)
        ub = cp_upper(r["halluc"], r["miss_n"])
        if ub <= HALLUC_CAP:
            feasible.append((r["false_abstain"] / max(r["ans_n"], 1), t, ub))
    if not feasible:
        print(f"  ❌ **v2 FAIL** —— cal2 上沒有任何 threshold 能把 halluc 的"
              f"單側 95% 上界壓到 ≤ {HALLUC_CAP:.0%}。**不放寬上限**，停止調參。")
        json.dump({**pre, "verdict": "FAIL_no_feasible_threshold", "n_miss_cal2": n_miss},
                  open(os.path.join(HERE, "results_g2c_cal_v2.json"), "w"),
                  indent=2, ensure_ascii=False)
        return
    best_fa = min(f for f, _, _ in feasible)
    ties = sorted(t for f, t, _ in feasible if f <= best_fa + 1e-12)
    thr = ties[-1]                # 同分取**較高（較保守）**的 threshold
    rc = rates(rows_cal, thr)
    rc_ub = cp_upper(rc["halluc"], rc["miss_n"])
    print(f"  cal2 選出 threshold = {thr:+.3f}"
          f"（可行且 false_abstain 最小的候選 {len(ties)} 個，取最高／最保守）")
    print(f"    cal2 上：halluc {rc['halluc']}/{rc['miss_n']} = "
          f"{rc['halluc']/max(rc['miss_n'],1):.1%}，95% 上界 {rc_ub:.1%} "
          f"(cap {HALLUC_CAP:.0%})   false_abstain "
          f"{rc['false_abstain']/max(rc['ans_n'],1):.1%}")
    pre = {**pre, "n_candidates": len(cand), "n_feasible": len(feasible),
           "threshold": thr, "cal2_halluc_ub": rc_ub}
    json.dump(pre, open(os.path.join(HERE, "g2c_cal_v2_preregistered.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"    threshold 已落 artifact（**在開 test2 之前**）")

    # ---- test2 **只跑一次** ----
    rows_te = collect(ret, enc, span, test2, R.KEYS_TEST2, a.seed + 6, a.p_missing)
    rt = rates(rows_te, thr)
    mn, an = max(rt["miss_n"], 1), max(rt["ans_n"], 1)
    ra, ha, fa = rt["R_abstain"] / mn, rt["halluc"] / mn, rt["false_abstain"] / an
    ci_ra, ci_ha, ci_fa = (wilson(rt["R_abstain"], rt["miss_n"]),
                           wilson(rt["halluc"], rt["miss_n"]),
                           wilson(rt["false_abstain"], rt["ans_n"]))
    ha_ub = cp_upper(rt["halluc"], rt["miss_n"])
    hm_k = sum(int(((s > thr) == h).sum()) for s, h in rows_te)
    hm_n = sum(int(h.numel()) for _, h in rows_te)
    print(f"\n  **test2（只跑一次，{len(R.KEYS_TEST2)} 個全新未查看身分）**")
    print(f"    R_abstain     {ra:6.1%}  [{ci_ra[0]:.1%}, {ci_ra[1]:.1%}]  (n={rt['miss_n']})")
    print(f"    halluc        {ha:6.1%}  [{ci_ha[0]:.1%}, {ci_ha[1]:.1%}]   "
          f"**單側 95% 上界 {ha_ub:.1%}**（gate ≤ {HALLUC_CAP:.0%}）")
    print(f"    false_abstain {fa:6.1%}  [{ci_fa[0]:.1%}, {ci_fa[1]:.1%}]  (n={rt['ans_n']})"
          f"   （utility gate ≤ {HALLUC_CAP:.0%}）")
    print(f"    hit/miss      {hm_k/max(hm_n,1):6.1%}  "
          f"[{wilson(hm_k, hm_n)[0]:.1%}, {wilson(hm_k, hm_n)[1]:.1%}]  (n={hm_n} steps)")
    print(f"\n  對照 G2c-v1（舊 test，10 身分）：R_abstain 81.5% / halluc 28.8% "
          f"/ false_abstain 37.6%")
    # 兩道 gate 都要過 —— utility gate 存在的理由：否則「永遠棄答」自動過
    ok_risk, ok_util = ha_ub <= HALLUC_CAP, fa <= HALLUC_CAP
    verdict = ("PASS" if (ok_risk and ok_util) else
               "FAIL_risk_gate" if not ok_risk else "FAIL_utility_gate")
    print(f"\n  裁決：risk gate（halluc 上界 ≤ {HALLUC_CAP:.0%}）"
          f"{'✅' if ok_risk else '❌'}   "
          f"utility gate（false_abstain ≤ {HALLUC_CAP:.0%}）{'✅' if ok_util else '❌'}"
          f"   →  **{verdict}**")
    print(f"  5% 是 **research gate，不宣稱 production-safe**。")
    if verdict != "PASS":
        print(f"  → 判**校準不可轉移**，停止調參：不換 threshold、不擴 cal、不開第三版。")
    json.dump({**pre, "cal2": rc, "test2": rt, "verdict": verdict,
               "test2_rates": {"R_abstain": ra, "halluc": ha, "halluc_ub95": ha_ub,
                               "false_abstain": fa, "hitmiss": hm_k / max(hm_n, 1)},
               "test2_ci": {"R_abstain": ci_ra, "halluc": ci_ha, "false_abstain": ci_fa,
                            "hitmiss": wilson(hm_k, hm_n)}},
              open(os.path.join(HERE, "results_g2c_cal_v2.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  -> results_g2c_cal_v2.json")


if __name__ == "__main__":
    main()
