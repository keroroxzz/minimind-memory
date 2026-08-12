"""`LKE-1` 三-seed train/eval —— **一次跑完，跑完直接照 prereg 判讀**。

授權：Codex [163]（artifact checksum `c2abbd5eff9d7a0c`）。
所有數字取自 `LKE1_prereg.json`，**本檔不得自行決定任何超參數**。

流程（每個 seed）：

1. 用 frozen train（730 筆）訓練 factorized extractor，4000 步。
2. 用 frozen CAL（**400 個 episode**）選**一次**最低可行 `tau`：
   accept iff `c >= tau`，使 unsafe wrong-existing rate 的單側 95% CP 上界 `<= 1%`。
   **之後不得再碰。** 無可行者 → `REJECT_ALL` sentinel，**不是**數值 1
   （數值 1 在 `c == 1.0` 時仍會 accept —— Codex [166] 抓到的 endpoint 漏洞）。
3. 四個 test cell 各 300 episode（150 present + 150 absent），報全部 primary 欄位。

`unsafe wrong-existing delivery` = 抽錯 key、通過 confidence gate、
而該錯 key **恰在 store 裡** → 交付了別人的記憶。**exact guard 救不了這種**，
因為 `contains(k_hat)` 是真的。這正是本實驗要量的東西。
"""
import json, os, random, sys
import torch
import torch.nn as nn
import torch.nn.functional as F
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
import bridge_store as ST
import lke_data as D
from bridge_latent_gate import gen, wilson
from bridge_g3c_dangling import Injector, cp_upper
from lke1_phase0 import mk_store
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))

# ⚠️ **全拒必須是 sentinel，不能用數值 1 冒充**（Codex [166] 抓到的 endpoint 漏洞）。
#    實作是 `accept iff c >= tau`，所以浮點 `c == 1.0` 在 `tau=1.0` 時仍會被接受 ——
#    `LKE-2R` seed 20260812 的 `T 1/300` 就是這個漏口，不是模型真的滿分自信。
#    `reject_all` 直接 bypass accept／store／injector，語意上不可能有例外。
REJECT_ALL = "reject_all"


def accepts(c, tau):
    """唯一的 accept 判定入口。任何一側都必須走這裡，不得各自寫 `c >= tau`。"""
    return False if tau == REJECT_ALL else c >= tau
P = json.load(open(os.path.join(HERE, "LKE1_prereg.json")))
E, O = P["learner"]["encoder"], P["optimizer"]


class Extractor(nn.Module):
    """factorized：兩個**分離**的 head，不建 48-way 聯合分類器。"""

    def __init__(self, vocab):
        super().__init__()
        d = E["d_model"]
        self.emb = nn.Embedding(vocab, d)
        self.pos = nn.Embedding(E["max_len"], d)
        layer = nn.TransformerEncoderLayer(d, E["heads"], E["ffn"], E["dropout"],
                                           batch_first=True, norm_first=True)
        self.enc = nn.TransformerEncoder(layer, E["layers"])
        self.norm = nn.LayerNorm(d)
        self.h_name = nn.Linear(d, len(B.NAMES))
        self.h_attr = nn.Linear(d, len(B.ATTRS))

    def forward(self, x, mask):
        h = self.emb(x) + self.pos(torch.arange(x.shape[1], device=x.device))
        h = self.enc(h, src_key_padding_mask=~mask)
        h = self.norm((h * mask.unsqueeze(-1)).sum(1) / mask.sum(1, keepdim=True))
        return self.h_name(h), self.h_attr(h)


def encode(tok, qs):
    ids = [tok(q, add_special_tokens=False).input_ids for q in qs]
    L = max(len(i) for i in ids)
    assert L <= E["max_len"], f"query 長度 {L} 超過鎖定的 max_len {E['max_len']}"
    pad = tok.pad_token_id or 0
    x = torch.tensor([i + [pad] * (L - len(i)) for i in ids])
    m = torch.tensor([[1] * len(i) + [0] * (L - len(i)) for i in ids]).bool()
    return x.to(DEVICE), m.to(DEVICE)


def train(tok, art, seed):
    torch.manual_seed(seed)
    rng = random.Random(seed)
    m = Extractor(len(tok)).to(DEVICE)
    decay = [p for n, p in m.named_parameters()
             if p.dim() >= 2 and not any(k in n for k in O["wd_exempt"])]
    nodecay = [p for n, p in m.named_parameters()
               if p.dim() < 2 or any(k in n for k in O["wd_exempt"])]
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": O["weight_decay"]},
                             {"params": nodecay, "weight_decay": 0.0}],
                            lr=O["lr"], betas=tuple(O["betas"]))
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, O["lr"], total_steps=O["steps"],
                                              pct_start=0.05)
    tr = art["train"]
    m.train()
    for step in range(O["steps"]):
        b = [tr[rng.randrange(len(tr))] for _ in range(O["batch_size"])]
        x, msk = encode(tok, [r["q"] for r in b])
        yn = torch.tensor([B.NAMES.index(r["k"][0]) for r in b], device=DEVICE)
        ya = torch.tensor([int(r["k"][1]) for r in b], device=DEVICE)
        ln, la = m(x, msk)
        loss = F.cross_entropy(ln, yn) + F.cross_entropy(la, ya)   # 未加權
        opt.zero_grad(set_to_none=True)
        loss.backward()
        nn.utils.clip_grad_norm_(m.parameters(), O["grad_clip"])
        opt.step(); sch.step()
        if (step + 1) % 1000 == 0:
            print(f"      step {step+1}/{O['steps']}  loss={loss.item():.4f}")
    return m.eval()


@torch.no_grad()
def predict(m, tok, qs):
    """回傳 [(k_hat, confidence)]。`c = max p(name) * max p(attr)`，**argmax tie 取最小 index**。"""
    x, msk = encode(tok, qs)
    ln, la = m(x, msk)
    pn, pa = ln.softmax(-1), la.softmax(-1)
    out = []
    for i in range(len(qs)):
        ni, ai = int(pn[i].argmax()), int(pa[i].argmax())   # torch.argmax 已是最小 index
        out.append(((B.NAMES[ni], ai), float(pn[i, ni] * pa[i, ai])))
    return out


def episodes(entries):
    """每個 query 展成 (present, absent) 兩個 episode —— 50:50 由 artifact 保證。"""
    for e in entries:
        for side in ("present", "absent"):
            yield e, side


def unsafe_count(preds, entries, tau):
    """CAL 的 tau 選擇只需要 extractor ＋ store 查表，**不需要跑 core** ——
    依 Codex [158]，安全違反是「交付」這件事本身，與答案內容無關。"""
    bad = n = 0
    for (e, side), (k_hat, c) in zip(episodes(entries), preds):
        n += 1
        if not accepts(c, tau):
            continue                                   # 被 gate 擋掉，沒有交付
        st = mk_store(e[side])
        if tuple(k_hat) != tuple(e["k"]) and st.contains(*k_hat):
            bad += 1                                   # 交付了別人的記憶
    return bad, n


def pick_tau(m, tok, art):
    """從**事前固定的 grid** 選最低可行 tau。無可行者 → `REJECT_ALL`（真正的全拒）。"""
    cal = art["cal"]
    qs = [e["q"] for e, _ in episodes(cal)]
    preds = predict(m, tok, qs)
    for tau in P["selective_policy"]["tau_grid"]:
        bad, n = unsafe_count(preds, cal, tau)
        if cp_upper(bad, n) <= 0.01:
            return tau, bad, n, cp_upper(bad, n)
    tau = REJECT_ALL                                   # fallback = **真的**全拒
    bad, n = unsafe_count(preds, cal, tau)
    return tau, bad, n, cp_upper(bad, n)


@torch.no_grad()
def run_cell(core, tok, inj, loops, m, entries, tau):
    """回傳這個 cell 的所有 primary 欄位。"""
    qs = [e["q"] for e in entries]
    raw = predict(m, tok, qs)
    r = {"raw_exact": 0, "n_raw": len(entries), "unsafe": 0, "n_ep": 0,
         "present_deliver_ok": 0, "present_deliver_n": 0,
         "false_abstain": 0, "correct_missing_abstain": 0,
         "wrong_absent": 0, "bitwise_same": 0}
    for e, (k_hat, c) in zip(entries, raw):
        exact = tuple(k_hat) == tuple(e["k"])
        r["raw_exact"] += int(exact)
        for side in ("present", "absent"):
            r["n_ep"] += 1
            st = mk_store(e[side])
            if not accepts(c, tau):
                if exact and side == "present":
                    r["false_abstain"] += 1
                continue
            if not st.contains(*k_hat):
                if exact and side == "absent":
                    r["correct_missing_abstain"] += 1
                elif not exact:
                    r["wrong_absent"] += 1
                continue
            if not exact:
                r["unsafe"] += 1                       # **交付了別人的記憶**
                continue
            if side == "absent":
                continue                               # exact 且 absent 不可能 contains
            # exact + present：真正的交付，量答案並與 oracle direct path 逐位元比對
            r["present_deliver_n"] += 1
            dis = (e["dis"][0], int(e["dis"][1]))
            tv = e["present"]["vals"]["|".join(map(str, e["k"]))]
            dv = e["present"]["vals"]["|".join((dis[0], str(dis[1])))]
            z = st.read(*k_hat)
            facts = [(k_hat[0], k_hat[1], tv), (dis[0], dis[1], dv)]
            ep = B.Episode(facts, f"{k_hat[0]} {B.ATTRS[k_hat[1]]} 是 多少",
                           B.val_str(tv), [0], 0)
            mc = inj(torch.stack([z, st.read(*dis)]))
            ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                   add_special_tokens=False).input_ids)
            out = gen(core, tok, ids, loops, mc)
            r["present_deliver_ok"] += int(out == ep.answer)
            # oracle direct path：用 k* 直接走，key/z/delivery/decode 須逐例相同
            k0 = (e["k"][0], int(e["k"][1]))
            z0, st0 = ST.retrieve(st, *k0)
            mc0 = inj(torch.stack([z0, st.read(*dis)]))
            out0 = gen(core, tok, ids, loops, mc0)
            r["bitwise_same"] += int(st0 == "ok" and tuple(k_hat) == k0
                                     and torch.equal(z, z0) and torch.equal(mc, mc0)
                                     and out == out0)
    return r


def main():
    art = D.load()
    assert art["checksum"] == P["artifact"]["checksum"], \
        f"artifact checksum 不是授權的那份：{art['checksum']}"
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core_latent.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    core = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"],
                                              **arch)).to(DEVICE).eval()
    core.load_state_dict(blob["model"])
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])
    inj = Injector(proj)
    loops = arch["num_loops"]

    print(f"  LKE-1 三-seed run   artifact={art['checksum']}（Codex [163] 授權）")
    print(f"  seeds={P['seeds']}   steps={O['steps']}   **一次跑完，不調任何東西**\n")

    out, fails = {}, []
    for seed in P["seeds"]:
        print(f"  ==== seed {seed}")
        m = train(tok, art, seed)
        tau, cbad, cn, cub = pick_tau(m, tok, art)
        print(f"    tau={tau}（CAL unsafe {cbad}/{cn}，CP上界 {cub:.2%}）"
              f"{'  ⚠️ fallback 全拒' if tau >= 1.0 else ''}")
        print(f"    {'cell':>5s} {'raw exact':>11s} {'交付答對':>11s} "
              f"{'false_ab':>9s} {'unsafe':>8s} {'逐位元同':>9s}")
        out[str(seed)] = {"tau": tau, "cal": [cbad, cn, cub], "cells": {}}
        for c, v in art["cells"].items():
            r = run_cell(core, tok, inj, loops, m, v, tau)
            re_ = r["raw_exact"] / r["n_raw"]
            dv = r["present_deliver_ok"] / max(r["present_deliver_n"], 1)
            fa = r["false_abstain"] / (r["n_raw"])
            ok = (re_ >= 0.95 and dv >= 0.95 and fa <= 0.05 and r["unsafe"] == 0
                  and r["bitwise_same"] == r["present_deliver_n"])
            if not ok:
                fails.append(f"seed{seed}/{c}")
            ucol = "{}/{}".format(r["unsafe"], r["n_ep"])
            bcol = "{}/{}".format(r["bitwise_same"], r["present_deliver_n"])
            print(f"    {c:>5s} {re_:>10.1%} {dv:>10.1%} {fa:>8.1%} "
                  f"{ucol:>8s} {bcol:>9s}" + ("" if ok else "  ✗"))
            out[str(seed)]["cells"][c] = r

    print()
    if fails:
        print(f"  → **LKE-1 FAIL**（{', '.join(fails)}）")
        print("    依 prereg：封存 LKE-1，記明歸因；下一個有界問題改做")
        print("    exact-key conflict/overwrite contract，**不另開第二套自然語言配方**。")
    else:
        print("  → **LKE-1 PASS**（三 seed × 四 cell 全過）。")
        print("    只稱「在這個 frozen controlled-language grammar 下，factorized canonical")
        print("    query extraction 與 exact-membership guard 可同時維持 utility 與")
        print("    已量的 wrong-existing safety」；**不稱** semantic／natural／open-set。")
    json.dump({"artifact": art["checksum"], "seeds": out, "fails": fails,
               "pass": not fails},
              open(os.path.join(HERE, "results_lke1.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_lke1.json")


if __name__ == "__main__":
    main()
