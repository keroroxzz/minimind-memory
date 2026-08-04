"""G3c：**storage fault injection** —— 一次性的 bounded 工程驗證，不是研究線。

G3b 的 harness 第一版意外造出「index 有 entry、內容缺失」的情境，得到 **halluc 100%**。
那只是 bug 暴露的診斷案例，不是模型結果 —— 但它指出一個真系統會發生的失效模式
（部分寫入失敗、GC 競態），而且**架構上這首先是儲存／索引一致性問題**：
不該要求 learned support 從相似度去猜內容存不存在（Codex）。

所以這裡先把契約釘死，再故意破壞它：

    **契約**  `address visible ⇔ committed content readable`（原子性）
    **要求**  讀到 dangling 時，controller 必須 **fail-closed 成 missing/abstain**
    **封板**  故障注入下 `halluc == 0` 即結束，**不延伸**

四種注入（每種都對照「未注入」）：
  `none`          基準
  `dangling`      address 在 bank 裡，但 store 讀不到內容
  `torn_commit`   commit 中途失敗 —— entry 存在但 latent 形狀/內容不完整
  `stale`         讀到舊版本（version 落後）

⚠️ **完全不訓練、不重校 threshold。** 這關測的是 controller 的
   fail-closed 行為，不是任何學到的元件。
"""
import argparse
import json
import os
import random
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
import g3a_train as G3
import g3b_closure as G3B
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from g2_train import core_hidden_cue
from g3a_train import CORE_ZD, Writer, precompute_events
from model.memory_module import (ADDR_DIM, LATENT_DIM, PERM_N, LatentStore, MemoryEntry,
                                 latent_to_perm, perm_to_latent,
                                 Retriever, address_vector)
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
FAULTS = ("none", "dangling", "torn_commit", "stale")


class ConsistencyError(Exception):
    """契約破裂。controller **必須**接住它並 fail-closed，不得放行。"""


def checked_read(store, symbol, visible):
    """契約檢查：`address visible ⇔ committed content readable`。

    回傳 latent；任何一項不成立就 raise，由 controller 轉成 abstain。
    這是**儲存層的責任**，不是 support head 的。
    """
    e = store.read([symbol])[0]
    if visible and e is None:
        raise ConsistencyError(f"dangling address：{symbol} 在 bank 裡但 store 讀不到")
    if e is None:
        raise ConsistencyError(f"{symbol} 不可見且不可讀（呼叫端不該走到這裡）")
    if e.address != symbol:
        raise ConsistencyError(f"address↔content 綁定破裂：{e.address} != {symbol}")
    if e.latent.shape != (LATENT_DIM,):
        raise ConsistencyError(f"torn commit：{symbol} 的 latent 形狀 {tuple(e.latent.shape)}")
    if not torch.isfinite(e.latent).all():
        raise ConsistencyError(f"torn commit：{symbol} 的 latent 含非有限值")
    if e.metadata.get("epoch") != store._contract_epoch:
        raise ConsistencyError(f"stale snapshot：{symbol} 的版本落後")
    return e.latent


def inject(store, pool, formed, fault, rng, targets):
    """把故障打進 store。回傳被破壞的符號（bank 仍宣稱它可見）。

    ⚠️ victim **只從這題真的會被讀到的 entry 裡挑**（`targets` = chain 用到的 key）。
       打在 distractor 上的故障本來就無害 —— 讓它佔掉 75% 的樣本只是浪費統計功效，
       並不會讓結論更保守。
    """
    if fault == "none":
        return None
    victim = targets[rng.randrange(len(targets))]
    if fault == "dangling":
        del store._entries[victim]                      # 內容消失，address 仍在 bank
    elif fault == "torn_commit":
        e = store._entries[victim]
        store._entries[victim] = MemoryEntry(
            e.address, e.latent[:LATENT_DIM // 2].clone(), e.metadata, e.version)
    elif fault == "stale":
        # ⚠️ 必須注入**發散的舊值**，不能只把 epoch 改舊。
        #    只改 metadata 的話，無防護對照仍會 100% 答對 —— 那只證明版本檢查
        #    有接線，沒證明它擋下了任何危害。這裡放一個**不同的置換**，
        #    模擬「舊值仍可讀」。（本設計沒有 overwrite 功能，這是故障注入，
        #    不是在測 overwrite 語意 —— 那仍留在後續階段。）
        e = store._entries[victim]
        old = list(range(PERM_N))
        while old == latent_to_perm(e.latent):
            rng.shuffle(old)
        md = dict(e.metadata); md["epoch"] = store._contract_epoch - 1
        store._entries[victim] = MemoryEntry(
            e.address, perm_to_latent(old).to(e.latent.device), md, e.version)
    return victim


@torch.no_grad()
def run(m, tok, dl, writer, ret, ev, s, pool, pool_perm, omitted, thr, fault, rng,
        fail_closed):
    store = LatentStore()
    store._contract_epoch = 1
    formed = {}
    for x in pool:
        z = writer(ev[(x, tuple(pool_perm[x]))].unsqueeze(0))[0]
        formed[x] = z
        store.commit([MemoryEntry(x, z, {"key": x, "epoch": 1})])
    victim = inject(store, pool, formed, fault, rng, list(dict.fromkeys(s.chain)))

    addrs = torch.stack([address_vector(x) for x in pool]).to(DEVICE)
    cues = core_hidden_cue(m, tok, s)
    logits, sup = ret(cues.unsqueeze(0), addrs.unsqueeze(0))
    pred = logits[0].argmax(-1).tolist()
    pred_hit = (sup[0] > thr).tolist()

    tgt = [pool.index(g) if g != omitted else -1 for g in s.chain]
    lat, aborted = [], False
    for j, g in enumerate(s.chain):
        if not pred_hit[j]:
            lat.append(torch.zeros(LATENT_DIM, device=DEVICE)); continue
        sym = pool[pred[j]]
        try:
            lat.append(checked_read(store, sym, visible=True).to(DEVICE))
        except ConsistencyError:
            if fail_closed:
                aborted = True                          # **fail-closed → abstain**
                break
            # 對照組（無防護）：**直接用讀到的東西硬答** —— 這才是真實的
            # 無防護行為。torn 的補零到長度、讀不到的才給零向量。
            e = store.read([sym])[0]
            z = torch.zeros(LATENT_DIM, device=DEVICE)
            if e is not None:
                n = min(e.latent.numel(), LATENT_DIM)
                z[:n] = e.latent.reshape(-1)[:n].to(DEVICE)
            lat.append(z)
    answered = not aborted and all(pred_hit)
    corrupted = victim is not None and victim in [pool[p] for p, h in zip(pred, pred_hit) if h]
    if not answered:
        return dict(answered=False, correct=None, corrupted=corrupted, aborted=aborted)
    from g1_oracle_inline import value_positions
    from g1_teacher_kv import greedy_override
    _, b_ids, pos = value_positions(tok, s)
    got = greedy_override(m, tok, b_ids,
                          G3._mk_fn(dl, torch.stack(lat).unsqueeze(0), pos.to(DEVICE)),
                          ARCH["num_loops"])
    return dict(answered=True, correct=(got == R.render_L0(s)[1]), corrupted=corrupted,
                aborted=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--writer-ckpt", default="g3a_writer_6820855741.pth")
    ap.add_argument("--g2b-results", default="results_g2b.json")
    ap.add_argument("--per-k", type=int, default=60)
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--seed", type=int, default=777)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    blob = torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")
    m.load_state_dict(blob["model"], strict=False)
    dl = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                       os.path.join(HERE, "g1_renderer_artifact.json"))
    dl.load_state_dict(blob["delivery"]); dl.eval()
    writer = Writer(BACKBONE["hidden_size"], out_param="rowsoftmax").to(DEVICE).eval()
    writer.load_state_dict(torch.load(os.path.join(HERE, a.writer_ckpt), map_location="cpu"))
    thr = json.load(open(os.path.join(HERE, a.g2b_results)))["threshold"]
    ck = sorted([f for f in os.listdir(HERE) if f.startswith("g2b_retriever_")],
                key=lambda f: os.path.getmtime(os.path.join(HERE, f)))[-1]
    ret = Retriever(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    sd = torch.load(os.path.join(HERE, ck), map_location="cpu")
    ret.load_state_dict(sd["retriever"] if "retriever" in sd else sd)
    for p in list(m.parameters()) + list(dl.parameters()) + \
            list(writer.parameters()) + list(ret.parameters()):
        p.requires_grad_(False)

    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    query_keys = [k for k in R.KEYS if k in two]
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(query_keys) | set(two))
    ev = precompute_events(m, tok, perms)

    print(f"  **不訓練、不重校**（threshold {thr:+.3f} 沿 G2b）")
    print(f"  契約：`address visible ⇔ committed content readable` + atomic commit")
    print(f"  要求：讀到不一致時 controller **fail-closed 成 abstain**")
    print(f"  封板條件：**故障注入下 halluc == 0**\n")

    rng0 = random.Random(a.seed)
    eps = [G3B.make_episode(k, rng0, query_keys, two, perms, a.pool, 0.0)
           for k in range(1, a.max_k + 1) for _ in range(a.per_k)]
    print(f"  episodes {len(eps)}（**p_omit=0**：這關只測故障，不混入漏寫）\n")
    print(f"  {'注入':<14s} {'fail-closed':<12s} {'作答率':>8s} {'答對|作答':>10s} "
          f"{'**halluc**':>12s}")
    out = {}
    for fc in (True, False):
        for fault in FAULTS:
            rng = random.Random(a.seed + 1)
            ans = [0, 0]; cor = [0, 0]; hal = [0, 0]
            for s, pool, pp, _ in eps:
                r = run(m, tok, dl, writer, ret, ev, s, pool, pp, None, thr, fault, rng, fc)
                ans[1] += 1; ans[0] += int(r["answered"])
                if r["answered"]:
                    cor[1] += 1; cor[0] += int(r["correct"])
                if r["corrupted"]:            # 被破壞的 entry 真的被讀到了
                    # ⚠️ halluc 的定義是「**讀到損壞內容卻仍然作答**」，
                    #    不是「作答且答錯」—— 碰巧答對一樣是不安全的行為。
                    #    契約破裂時唯一可接受的動作是 abstain。
                    hal[1] += 1
                    hal[0] += int(r["answered"])
            key = f"{fault}|{'fail-closed' if fc else 'no-guard'}"
            out[key] = {"answered": ans[0] / ans[1], "correct": cor[0] / max(cor[1], 1),
                        "halluc": hal[0] / max(hal[1], 1), "halluc_n": hal[1]}
            print(f"  {fault:<14s} {'是' if fc else '**否（對照）**':<12s} "
                  f"{ans[0]/ans[1]:7.1%} {cor[0]/max(cor[1],1):9.1%} "
                  f"{hal[0]/max(hal[1],1):11.1%} (n={hal[1]})")
        print()

    bad = {k: v for k, v in out.items()
           if k.endswith("fail-closed") and v["halluc"] > 0}
    print(f"  裁決：fail-closed 下故障注入的 halluc "
          f"{'全為 0 → **契約成立，封板**' if not bad else f'**未歸零：{bad}**'}")
    print(f"  對照組（no-guard）的 halluc 若明顯 > 0，代表 guard 確實在做事，"
          f"\n  而不是故障根本沒被觸發。")
    json.dump({"cells": out, "threshold": thr, "faults": list(FAULTS),
               "query_keys": query_keys, "pool": a.pool, "seed": a.seed,
               "verdict": "SEALED" if not bad else "FAIL"},
              open(os.path.join(HERE, "results_g3c.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g3c.json")


if __name__ == "__main__":
    main()
