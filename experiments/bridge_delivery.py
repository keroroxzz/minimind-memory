"""橋接任務的 delivery —— 把**連續** latent 送進凍結的新 core。

這是四個元件裡**唯一碰到模型內部**的一塊：

    事件 → writer → latent z → store → retriever → **delivery** → core

`K' = K_native + f_slot(z)`、`V' = V_native + g_slot(z)`，在載體位置、
全部 (loop, layer) 槽各做一次。**殘差且零初始化**，所以起手是 no-op ——
G1 的結論：殘差 1.13M 成功、絕對 6.32M 失敗，優化上的恆等初始化是關鍵。

### 與 S₅ 版的關鍵差異

| | S₅ | 這裡 |
|---|---|---|
| latent | 25 維 **one-hot**（離散碼字）| **4 維，第 0 維是連續純量** |
| 鄰近 | 正交，34 與 35 無關 | **0.34 與 0.35 相距 0.01** |
| span | 每值 5 token | 每值 **2** token |
| carrier | 全有或全無 | **隨機混合**（部分顯式、部分載體）|

「連續」正是 §4.50 換任務要補的軸 —— 也是 G7d 那個「argmax 可解碼 ≠ 可消費」
能不能跨任務複製的關鍵：**連續 schema 下「精確碼字」這個概念本身就不存在**。

### 實驗窗口由 gate 決定，不是我挑的

gate 1 量出 `num_loops=4` 時 `L0` 的 ceiling 是 **j=2**（j=2 已 92%、j≥3 崩到隨機）。
所以可用窗口是 **j ∈ {0, 1}**：`j=0` 純消費、`j=1` 融合。
**j≥2 的格子一律 censor**，不得用來裁決記憶。

### 三臂比較（同一批題目）

    L0              值寫在文字裡 —— 天花板
    oracle carrier  oracle inline（零參數）—— core 的格式流利度上限
    learned         這裡訓練的 delivery —— 主條件

primary 是**追平 `L0`**，不是「贏過沒有記憶」。
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
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
SPAN = 2                     # 每個值 2 個 token


class Delivery(nn.Module):
    """`z → (ΔK, ΔV)`，逐 (loop, layer) 槽一組頭。**零初始化 → 起手 no-op。**

    交付以**整序列 latent map**表達：`Z` 是 (B, T, latent_dim)，非載體位置為 0，
    再由 `M` (B, T, 1) 遮掉。這樣不同題目的載體位置不同也能整批前向 ——
    逐題前向會是 batch 倍的成本，在 8000 步的尺度上不可接受。

    同一個值的 2 個 token 拿到**同一個** `z`，位置差異由 core 的位置編碼承擔。
    """

    def __init__(self, latent_dim, n_slots, n_kv, head_dim, width=256, nfreq=0,
                 mode="kv"):
        super().__init__()
        self.n_kv, self.head_dim, self.nfreq, self.mode = n_kv, head_dim, nfreq, mode
        # nfreq>0：對連續維做 Fourier 特徵展開。
        #
        # 為什麼需要：`f` 是平滑 MLP，吃一個純量；但它要打中的目標**極度不平滑**
        # —— `0.34 → "3 4"` 與 `0.35 → "3 5"` 輸入相距 0.01，在 core 的表示空間
        # 裡卻是兩個完全不相鄰的點。這是座標型 MLP 的 spectral bias（與 NeRF 的
        # positional encoding 同一個形狀）。**當初刻意設計的「0.34 與 0.35 有意義
        # 地相近」，正好是讓交付變難的那個性質。**
        #
        # 這一項是**混淆排除**，不是調參：能修好 ⇒ 條件數問題；修不好 ⇒ 才可能
        # 是凍結 core 的消費限制（G7d 那條線）。兩者的結論天差地遠。
        if nfreq:
            self.register_buffer("freqs", 2.0 ** torch.arange(nfreq) * torch.pi)
            latent_dim = latent_dim - 1 + 2 * nfreq
        self.lat = nn.Sequential(nn.Linear(latent_dim, width), nn.GELU(),
                                 nn.Linear(width, width), nn.GELU())
        d = n_kv * head_dim
        self.heads = nn.ModuleList([nn.Linear(width, 2 * d) for _ in range(n_slots)])
        for h in self.heads:                      # 恆等初始化：殘差起手為零
            nn.init.zeros_(h.weight); nn.init.zeros_(h.bias)

    def feat(self, Z):
        if not self.nfreq:
            return Z
        a = Z[..., :1] * self.freqs               # 連續維（值/100）展開
        return torch.cat([a.sin(), a.cos(), Z[..., 1:]], dim=-1)

    def forward(self, slot, Z, M, k_nat, v_nat):
        Bs, T = k_nat.shape[0], k_nat.shape[1]
        d = self.n_kv * self.head_dim
        o = self.heads[slot](self.lat(self.feat(Z))) * M
        ok = o[..., :d].view(Bs, T, self.n_kv, self.head_dim)
        ov = o[..., d:].view(Bs, T, self.n_kv, self.head_dim)
        # mode：分離「定址」與「內容」。
        #
        # 串擾診斷顯示 n_carrier=1 時 100%、每多一條用不到的記憶就崩一截。
        # 假說 H3：載體位置的 **native K 帶著定址資訊**（「這是 ben a 後面的值格」），
        # 同時改寫 K 會把「這是哪一條記憶」洗掉 —— 一條時無所謂，多條時 query
        # 分不出誰是誰。若成立，**只改 V 不動 K** 應該修好多載體的崩塌。
        if self.mode == "v_only":
            ok = torch.zeros_like(ok)
        elif self.mode == "k_only":
            ov = torch.zeros_like(ov)
        return k_nat + ok, v_nat + ov


def mk_fn(dl, Z, M, nl):
    """回傳給 `kv_override` 的 callable；它按 (loop, layer) 順序被呼叫 nl 次。"""
    box = {"i": 0}

    def fn(xk, xv):
        i = box["i"]; box["i"] = (i + 1) % nl
        T = xk.shape[1]
        pos = torch.arange(T, device=xk.device)
        k, v = dl(i, Z[:, :T], M[:, :T], xk, xv)
        return pos, k, v
    return fn


def make_item(tok, rng, j):
    ep = B.make_episode(rng, j)
    mask = B.random_mask(rng, ep, p=0.6, force_used=True)
    ids, pos = B.value_positions(tok, ep, mask)
    lat = torch.tensor(B.episode_latents(ep, mask), dtype=torch.float32)
    # 每個值 2 個 token → 每條 latent 重複 SPAN 次貼到它的位置上
    Z = torch.zeros(len(ids), B.LATENT_DIM)
    M = torch.zeros(len(ids), 1)
    Z[pos] = lat.repeat_interleave(SPAN, dim=0)
    M[pos] = 1.0
    return ep, mask, ids, pos, Z, M


def batch(tok, rng, bs, js):
    items, maxlen = [], 0
    for _ in range(bs):
        ep, mask, ids, pos, Z, M = make_item(tok, rng, js[rng.randrange(len(js))])
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
def greedy(m, tok, ids, loops, dl=None, Z=None, M=None, nl=None, max_new=4):
    """交付只作用在 prompt 那一次前向；答案 token 沒有載體，不再套用。"""
    kw = {}
    if dl is not None:
        kw["kv_override"] = mk_fn(dl, Z.unsqueeze(0).to(DEVICE),
                                  M.unsqueeze(0).to(DEVICE), nl)
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
def evaluate(m, tok, dl, loops, nl, js, n, seed):
    print(f"\n  {'j':>3s} {'L0':>8s} {'oracle carrier':>15s} {'learned':>9s} "
          f"{'learned-L0':>11s}")
    out = {}
    for j in js:
        acc = {}
        for arm in ("L0", "oracle", "learned"):
            rng = random.Random(seed + j)
            ok = 0
            for _ in range(n):
                ep, mask, ids, pos, Z, M = make_item(tok, rng, j)
                if arm == "L0":
                    t = torch.tensor(tok(tok.bos_token + B.render(ep)[0],
                                         add_special_tokens=False).input_ids)
                    got = greedy(m, tok, t, loops)
                elif arm == "oracle":
                    src = torch.tensor(tok(tok.bos_token + B.render(ep, None)[0],
                                           add_special_tokens=False).input_ids)
                    e = m.model.embed_tokens(ids.to(DEVICE)).clone()
                    p = pos.to(DEVICE)
                    e[p] = m.model.embed_tokens(src.to(DEVICE))[p]
                    o = m(ids.unsqueeze(0).to(DEVICE),
                          inputs_embeds=e.unsqueeze(0), use_cache=True, num_loops=loops)
                    pkv, g = o.past_key_values, []
                    for _ in range(4):
                        nx = o.logits[:, -1].argmax(-1, keepdim=True)
                        if nx.item() == tok.eos_token_id:
                            break
                        g.append(nx.item())
                        o = m(nx, past_key_values=pkv, use_cache=True, num_loops=loops)
                        pkv = o.past_key_values
                    got = tok.decode(g, skip_special_tokens=True).strip()
                else:
                    got = greedy(m, tok, ids, loops, dl, Z, M, nl)
                ok += int(got == ep.answer)
            acc[arm] = ok / n
        gap = acc["learned"] - acc["L0"]
        print(f"  {j:>3d} {acc['L0']:>8.1%} {acc['oracle']:>15.1%} "
              f"{acc['learned']:>9.1%} {gap*100:>+10.1f}pp"
              + ("   **censored**（L0<95%）" if acc["L0"] < 0.95 else ""))
        out[j] = acc
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", default="bridge_core.pth")
    ap.add_argument("--steps", type=int, default=8000)
    ap.add_argument("--bs", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--js", type=int, nargs="+", default=[0, 1])
    ap.add_argument("--eval-js", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--n-eval", type=int, default=150)
    ap.add_argument("--seed", type=int, default=424242)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--nfreq", type=int, default=0, help="連續維的 Fourier 頻帶數")
    ap.add_argument("--tag", default="", help="輸出檔名後綴，避免覆蓋前一條件")
    ap.add_argument("--mode", default="kv", choices=["kv", "v_only", "k_only"],
                    help="kv=同時改寫；v_only=只改內容、保留定址；k_only=反向對照")
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, a.core), map_location="cpu")
    arch = dict(blob["arch"])
    gates = json.load(open(os.path.join(HERE, "results_bridge_gates.json")))
    loops = gates["chosen_num_loops"]              # **由 gate 1 決定，不是我挑的**
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    for p in m.parameters():
        p.requires_grad_(False)

    nl = arch["num_hidden_layers"] * arch["num_loops"]
    dl = Delivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                  arch["hidden_size"] // arch["num_attention_heads"],
                  nfreq=a.nfreq, mode=a.mode).to(DEVICE)
    nd = sum(p.numel() for p in dl.parameters())
    print(f"  mode={a.mode}   nfreq={a.nfreq}"
          + ("（Fourier 特徵 —— 混淆排除，不是調參）" if a.nfreq else "（原始純量輸入）"))
    print(f"  core 凍結（{a.core}, step={blob['step']}）；**只訓 delivery** {nd/1e6:.2f}M")
    print(f"  num_loops={loops}（由 gate 1 的 L0 sweep 決定）；訓練 j={a.js}")
    print(f"  latent 是**連續**的（第 0 維 = 值/100）—— 這是換任務要補的軸\n")

    # ---- smoke 診斷：逐 j 印 loss / 梯度 / 樣本數 ----------------------------
    # 已記錄的流程規則：曾經因為抽樣偏斜而差點寫出「KL 從頭到尾是零」的錯誤結論。
    # 零初始化 ⇒ step 0 的 delivery 是嚴格的 no-op，所以 `learned` 必須與
    # `none`（完全不交付）**逐位元相同**；不同就代表注入路徑接錯了。
    print("  ---- smoke：零初始化下 learned 必須 == none（no-op），且梯度非零")
    for j in a.js:
        r = random.Random(a.seed + 7)
        X, Y, Z, M = batch(tok, r, 32, [j])
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
        nsup = int((Y[:, 1:] != -100).sum())
        print(f"    j={j}  none={l_none:.4f}  learned={ls.item():.4f}  "
              f"Δ={abs(ls.item()-l_none):.2e}  |grad|={gn:.4f}  "
              f"carrier位={int(M.sum())}  監督位={nsup}")
        assert abs(ls.item() - l_none) < 1e-4, "零初始化下不是 no-op —— 注入路徑接錯"
        assert gn > 1e-8, f"j={j} 梯度為零 —— delivery 收不到訊號"
    dl.zero_grad(set_to_none=True)
    print()

    opt = torch.optim.AdamW(dl.parameters(), lr=a.lr, weight_decay=0.01)
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps,
                                              pct_start=0.05)
    rng = random.Random(a.seed)
    dl.train(); t0 = time.time()
    for step in range(1, a.steps + 1):
        X, Y, Z, M = batch(tok, rng, a.bs, a.js)
        X, Y = X.to(DEVICE), Y.to(DEVICE)
        Z, M = Z.to(DEVICE), M.to(DEVICE)
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
                  f"{step/(time.time()-t0):.2f} it/s  {(time.time()-t0)/60:.0f} min",
                  flush=True)

    dl.eval()
    res = evaluate(m, tok, dl, loops, nl, a.eval_js, a.n_eval, a.seed + 99)
    print(f"\n  primary 是**追平 L0**，不是贏過沒有記憶。")
    print(f"  j=0 純消費、j=1 融合；**j>=2 的格子 L0<95%，一律 censor**。")
    torch.save({"delivery": dl.state_dict(), "loops": loops, "core": a.core,
                "nfreq": a.nfreq, "mode": a.mode},
               os.path.join(HERE, f"bridge_delivery{a.tag}.pth"))
    json.dump({"core": a.core, "loops": loops, "train_js": a.js, "steps": a.steps,
               "nfreq": a.nfreq, "mode": a.mode, "delivery_params_M": nd/1e6, "seed": a.seed,
               "acc": {str(k): v for k, v in res.items()}},
              open(os.path.join(HERE, f"results_bridge_delivery{a.tag}.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  -> bridge_delivery{a.tag}.pth / results_bridge_delivery{a.tag}.json")


if __name__ == "__main__":
    main()
