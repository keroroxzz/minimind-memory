"""G2c pool scaling：8 → 16 → 32，**encoder 與 threshold 全部凍結**。

Codex 的條件：pool=8 過原 gate 後才做 scaling，且 scaling **只加 distractor** ——
query 身分、split、threshold 一律沿用 pool=8 訓練時決定的那一份，
不重訓、不重校。否則測到的是「重新調參後仍可行」，不是「同一套參數擴不擴得動」。

distractor 從 `KEYS_DISTRACTOR`（f48..f159）補，那些身分**從未當過 query target、
也從未進 train/cal** —— 加大 pool 純粹是加大干擾，不引入新的可學訊號。

判讀：ordered/per-step 掉的是**分離度不夠**；`halluc` 升高、`R_abstain` 掉的是
**threshold 撐不住更多 near-impostor**。兩者要分開報，不可只給整體數字。
"""
import argparse
import json
import os
import random
import sys
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_train import ARCH, BACKBONE, DEVICE
from g2_train import CORE_ZD, key_span_embeddings
from model.memory_module import ADDR_DIM, Retriever, TiedAddressEncoder
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True, help="g2c_retriever_*.pth（含 encoder）")
    ap.add_argument("--results", required=True, help="results_g2c_seed*.json（取 threshold）")
    ap.add_argument("--pools", type=int, nargs="+", default=[8, 16, 32])
    ap.add_argument("--val-per-k", type=int, default=100)
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--p-missing", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    res = json.load(open(os.path.join(HERE, a.results)))
    thr = res["threshold"]
    assert res["stage"] == "G2c", f"只能拿 G2c 的 checkpoint 做 scaling（拿到 {res['stage']}）"
    print(f"  threshold = {thr:+.3f}（沿用 pool={res['pool']} 訓練時的 calibration，**不重校**）")

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    m.load_state_dict(torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")["model"],
                      strict=False)
    blob = torch.load(os.path.join(HERE, a.ckpt), map_location="cpu")
    ret = Retriever(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    enc = TiedAddressEncoder(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    ret.load_state_dict(blob["retriever"]); enc.load_state_dict(blob["encoder"])
    for p in list(ret.parameters()) + list(enc.parameters()):
        p.requires_grad_(False)

    universe = R.KEYS_TEST + R.KEYS_DISTRACTOR
    span = key_span_embeddings(tok, m.model.embed_tokens, universe)
    sp_e, sp_m = span

    # test 樣本與正式跑用同一組（同 seed、同排除鏈）—— 只有 pool 大小在變
    tr = R.build_dataset(a.max_k, res.get("train_per_k", 5000), a.seed, keys=R.KEYS_TRAIN)
    seen = {s.delivery_id for s in tr}
    cal = R.build_dataset(a.max_k, a.val_per_k, a.seed + 999, exclude=seen, keys=R.KEYS_CAL)
    seen |= {s.delivery_id for s in cal}
    te = R.build_dataset(a.max_k, a.val_per_k, a.seed + 1777, exclude=seen, keys=R.KEYS_TEST)
    print(f"  test {len(te)}（checksum {R.delivery_checksum(te)}，與正式跑同一批）"
          f"   distractor 身分池 {len(R.KEYS_DISTRACTOR)}（全未見、從不當 target）\n")

    print(f"  {'pool':>5s} {'ordered':>9s} {'per-step':>9s} {'hit/miss':>9s} "
          f"{'R_abstain':>10s} {'halluc':>8s} {'false_abst':>11s} {'imp cos max':>12s}")
    out = {}
    for n_pool in a.pools:
        rng = random.Random(a.seed + 99)          # 每個 pool 大小用同一顆 rng 種子
        st = defaultdict(int)
        imp_max = -2.0
        with torch.no_grad():
            for s in te:
                pool, lat, chain, tgt, hit, dropped = R.build_pool(
                    s, rng, missing=rng.random() < a.p_missing,
                    keys=universe if n_pool > len(R.KEYS_TEST) else R.KEYS_TEST,
                    pool_size=n_pool)
                # ⚠️ required key 一律來自 KEYS_TEST（build_dataset 決定），
                #    universe 只擴大 distractor 的來源。
                addrs = enc(torch.stack([sp_e[g] for g in pool]),
                            torch.stack([sp_m[g] for g in pool])).unsqueeze(0)
                q = enc(torch.stack([sp_e[g] for g in chain]),
                        torch.stack([sp_m[g] for g in chain])).unsqueeze(0)
                logits, sup = ret(None, addrs, query=q)
                pred = logits[0].argmax(-1)
                pred_hit = sup[0] > thr
                tgt_t = torch.tensor(tgt, device=DEVICE)
                hit_t = torch.tensor(hit, device=DEVICE)
                cos = logits[0] / ret.log_temp.exp()
                for j in range(cos.size(0)):
                    row = cos[j].clone()
                    if tgt_t[j] >= 0:
                        row[tgt_t[j]] = -2.0
                    imp_max = max(imp_max, float(row.max()))
                step_ok = ((pred == tgt_t) & hit_t) | (~hit_t & ~pred_hit)
                st["step_ok"] += int(step_ok.sum()); st["steps"] += s.k
                st["hitacc"] += int((pred_hit == hit_t).sum())
                st["ordered"] += int(step_ok.all()); st["n"] += 1
                if not bool(hit_t.all()):
                    st["miss_n"] += 1
                    ok = bool((~pred_hit[~hit_t]).all())
                    st["R_abstain"] += int(ok); st["halluc"] += int(not ok)
                else:
                    st["ans_n"] += 1
                    st["false_abstain"] += int((~pred_hit).any())
        mn, an = max(st["miss_n"], 1), max(st["ans_n"], 1)
        print(f"  {n_pool:>5d} {st['ordered']/st['n']:8.1%} {st['step_ok']/st['steps']:8.1%} "
              f"{st['hitacc']/st['steps']:8.1%} {st['R_abstain']/mn:9.1%} "
              f"{st['halluc']/mn:7.1%} {st['false_abstain']/an:10.1%} {imp_max:11.3f}")
        out[n_pool] = {**dict(st), "imp_cos_max": imp_max}

    print(f"\n  chance（per-step top-1）：" +
          "  ".join(f"pool{n}={1/n:.1%}" for n in a.pools))
    print(f"  ⚠️ encoder 與 threshold **全程凍結**，沒有為任何 pool 大小重調")
    dst = os.path.join(HERE, "results_g2c_scale.json")
    json.dump({"ckpt": a.ckpt, "threshold": thr, "pools": {str(k): v for k, v in out.items()},
               "test_checksum": R.delivery_checksum(te),
               "n_distractor_identities": len(R.KEYS_DISTRACTOR)},
              open(dst, "w"), indent=2, ensure_ascii=False)
    print(f"  -> {os.path.basename(dst)}")


if __name__ == "__main__":
    main()
