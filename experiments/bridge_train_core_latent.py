"""latent-only core 訓練 —— **文字裡沒有事實,記憶走 prepended carrier**。

`BRIDGE_PREREG_latent.md` 是規格,本檔只執行。舊 `bridge_core.pth` 不動。

訓練組成(prereg §5):
- `L0` 與 `latent` **各半**(§4.50 的一級變因規定:core 格式必須兩種都涵蓋)。
- **不訓練「空文字＋無 memory」** —— 那等於教模型在沒有依據時仍輸出答案(訓練幻覺)。
  該 render 只作**評估地板**。abstention 是獨立軸(R4),本版不開。
- 同一 batch 內 `n_fact` 固定(`memory_carriers` 要求 batch 內 carrier 數一致),
  batch 之間仍在 2..4 隨機。
- carrier 順序**每題隨機打亂**,否則模型可用「第 k 個」取代 address。

⚠️ `use_engram` 預設是 True,每個旗標顯式釘死(CLAUDE.md 記過的坑)。
⚠️ carrier 佔用序列位置(RoPE 位移),但**不佔文字 token**。不得宣稱省 context。
"""
import argparse
import json
import os
import random
import sys
import time

import torch
import torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_latent_schema as S
import bridge_renderer as B
from bridge_train_core import ARCH, DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def build_batch(tok, rng, bs, js, latent_p=0.5, nf_min=2, nf_max=4, l0_nf_max=None,
                same_name_p=0.0):
    """回傳 (X, Y, Z, use_latent)。`Z` 是 (bs, n_car, LAT_DIM) 或 None。

    ⚠️ `n_fact` **每個 batch 固定**：`memory_carriers` 是單一張量,
       batch 內 carrier 數必須一致;padding 出來的 carrier 無法被 mask 掉
       （模型內部對 carrier 一律補 attention_mask=1）,會變成真的干擾項。
    """
    # ⚠️ **先決定 mode，再依 mode 抽 n_fact**（Codex [141] 選項 (b)）。
    #    容量實驗把 `n_fact` 擴到 16 時，`L0` 分支會變質成近乎不可解的任務
    #    （文字裡塞 16 條事實再找出被問那條），半數訓練預算被它吃掉、梯度被它主導
    #    —— §4.56 的 gate FAIL 就是這樣來的。
    #    `l0_nf_max` 讓 **L0 只在小 n 出現**（format anchor），latent 才覆蓋完整範圍。
    #    **兩者的 n 分布因此不同，這是刻意的訓練支持差異，必須明寫**，
    #    且 L0 **不得**用來當大 n 的 ceiling。
    #    ⚠️ L0 的下界必須獨立於 `nf_min`：fixed-load 診斷會用 `nf_min=nf_max=8`，
    #       若沿用 `randint(nf_min, min(l0_nf_max, nf_max))` 會得到 randint(8, 4) 直接爆。
    use_latent = rng.random() < latent_p
    if use_latent or l0_nf_max is None:
        n_fact = rng.randint(nf_min, nf_max)
    else:
        n_fact = rng.randint(min(2, l0_nf_max), l0_nf_max)
    rows, maxlen = [], 0
    for _ in range(bs):
        j = js[rng.randrange(len(js))]
        # same_name_p：保證同名的題目比例。自然抽樣在 n=2 只有 4.6%，
        # 而 n=2 正是主 gate 格 —— 太稀疏教不動，所以顯式提高覆蓋率。
        # **這是刻意的分布選擇，必須明寫**；評估另做 0 vs >=1 的 factorial。
        sn = True if (same_name_p and rng.random() < same_name_p) else False
        ep = B.make_episode(rng, j, n_fact=n_fact, same_name=sn)
        if use_latent:
            p, ans = S.render_latent(ep)
            order = list(range(n_fact))
            rng.shuffle(order)                       # 順序必須隨機
            lat, _ = S.episode_latents2(ep, order)
        else:
            p, ans = S.render_l0(ep)
            lat = None
        full = tok(tok.bos_token + p + " " + ans + tok.eos_token,
                   add_special_tokens=False).input_ids
        plen = len(tok(tok.bos_token + p, add_special_tokens=False).input_ids)
        y = list(full); y[:plen] = [-100] * plen
        rows.append((full, y, lat, bool(sn)))
        maxlen = max(maxlen, len(full))
    pad = tok.pad_token_id or 0
    X = torch.tensor([f + [pad] * (maxlen - len(f)) for f, _, _, _ in rows])
    Y = torch.tensor([y + [-100] * (maxlen - len(y)) for _, y, _, _ in rows])
    Z = torch.stack([r[2] for r in rows]) if use_latent else None
    H = torch.tensor([r[3] for r in rows])          # 逐樣本的 stratum 標記（hard=True）
    return X, Y, Z, use_latent, H


def forward_loss(m, proj, X, Y, Z, cfg, H=None, hard_w=1.0):
    """carrier 前綴會讓 logits 左邊多出 n_car 格 —— **Y 必須跟著左補 -100**。

    `hard_w`：hard stratum 的**固定** per-example 權重（Codex [146]）。
    **正規化到批內平均 1** —— 否則介入會同時改變總梯度尺度與有效 lr，
    那就不是單一變因了。`hard_w=1.0` 時**逐位元等同未加權路徑**。
    """
    kw = {}
    n_car = 0
    if Z is not None:
        mc = proj(Z)
        n_car = mc.shape[1]
        kw["memory_carriers"] = mc
    logits = m(X, **kw).logits
    if n_car:
        Y = torch.cat([Y.new_full((Y.shape[0], n_car), -100), Y], dim=1)
    assert logits.shape[1] == Y.shape[1], (logits.shape, Y.shape)
    if H is None or hard_w == 1.0:
        return F.cross_entropy(logits[:, :-1].reshape(-1, logits.size(-1)).float(),
                               Y[:, 1:].reshape(-1), ignore_index=-100)
    # 逐樣本加權：先算每個樣本的平均 CE，再依 stratum 加權並正規化到平均 1
    tgt = Y[:, 1:]
    per = F.cross_entropy(logits[:, :-1].reshape(-1, logits.size(-1)).float(),
                          tgt.reshape(-1), ignore_index=-100,
                          reduction="none").view(tgt.shape)
    msk = (tgt != -100).float()
    per_ex = (per * msk).sum(1) / msk.sum(1).clamp(min=1)
    w = torch.where(H.to(per_ex.device), torch.full_like(per_ex, hard_w),
                    torch.ones_like(per_ex))
    w = w / w.mean().clamp(min=1e-8)                # **正規化到平均 1**
    return (per_ex * w).mean()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=24000)
    ap.add_argument("--bs", type=int, default=48)
    ap.add_argument("--lr", type=float, default=6e-4)
    ap.add_argument("--js", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--seed", type=int, default=20260806)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--nf-min", type=int, default=2)
    ap.add_argument("--nf-max", type=int, default=4,
                    help="容量實驗用 16（上限：NAMES 只有 16 個且同題名字互異）")
    ap.add_argument("--hard-w", type=float, default=1.0,
                    help="hard stratum 的固定權重（正規化到平均 1）；1.0 = 未加權，逐位元等同")
    ap.add_argument("--calibrate-hard-w", action="store_true",
                    help="fresh-init calibration（Codex [147] 的 (b\u2032)）："
                         "正式更新前用**本 run 自己的初始權重**量一次兩 stratum 的梯度範數，"
                         "w_h = G_e/G_h，立即鎖死、reset 回同一初始狀態再訓練")
    ap.add_argument("--ckpt-every", type=int, default=0,
                    help=">0 時每 N 步另存一個**帶步數的** checkpoint（保留中途狀態）")
    ap.add_argument("--init-from", default=None,
                    help="從既有 checkpoint 續訓（機制探針：測『要多少步才學會』）")
    ap.add_argument("--same-name-p", type=float, default=0.0,
                    help="保證同名的題目比例（同實體多屬性覆蓋率）；0 = 歷史行為")
    ap.add_argument("--l0-nf-max", type=int, default=None,
                    help="L0 分支的 n_fact 上限（容量實驗用 4）；None = 與 latent 相同")
    ap.add_argument("--out", default="bridge_core_latent.pth")
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    cfg = MiniMindConfig(vocab_size=len(tok), **ARCH)
    m = MiniMindForCausalLM(cfg).to(DEVICE)
    if a.init_from:
        _b = torch.load(os.path.join(HERE, a.init_from), map_location="cpu")
        m.load_state_dict(_b["model"])
        print(f"  ** 從 {a.init_from}（step={_b['step']}）續訓 —— 這是機制探針，"
              f"不是乾淨的單變因 run **")
    proj = S.CarrierProj(ARCH["hidden_size"]).to(DEVICE)
    if a.init_from:
        # ⚠️ **續訓必須沿用 checkpoint 當初的 carrier scale。**
        #    `fit_scale` 是用「當下的 embedding」算的；載入已訓練的 core 之後
        #    embedding 已經變了，重算會得到不同尺度（實測 0.026 → 0.087，3.3 倍）。
        #    那不是續訓，是**換了輸入尺度** —— 探針會顯示「微調沒用」，
        #    但真正原因是尺度被我自己改掉了。
        sc = float(_b["carrier_scale"])
        proj.scale.fill_(sc)
    else:
        sc = S.fit_scale(proj, m.model.embed_tokens.weight)
    n = sum(p.numel() for p in m.parameters())
    npj = sum(p.numel() for p in proj.parameters())

    print(f"  latent-only core {n/1e6:.2f}M   num_loops={ARCH['num_loops']}")
    print(f"  prereg: BRIDGE_PREREG_latent.md")
    print(f"  schema z' = [addr({S.ADDR_DIM}) + value(1) + attr(3)] = {S.LAT_DIM} 維（**含身份**）")
    print(f"  carrier 投影：**固定未訓練**（可訓練參數 {npj}），scale={sc:.3f}")
    print(f"  same_name_p={a.same_name_p}（同實體多屬性覆蓋率；0 = 歷史行為）\n  n_fact：latent {a.nf_min}..{a.nf_max}   L0 {min(2, a.l0_nf_max) if a.l0_nf_max else a.nf_min}..{a.l0_nf_max or a.nf_max}（format anchor）\n  訓練 j={a.js}；**L0 與 latent 各半**；latent 的文字裡沒有事實、沒有 placeholder")
    print(f"  use_engram={ARCH['use_engram']}（顯式釘死）\n")

    # ---- smoke：三個必須成立的不變量 -------------------------------------
    r = random.Random(a.seed + 7)
    X, Y, Z, ul, H = build_batch(tok, r, 8, a.js, latent_p=1.0,
                              nf_min=a.nf_min, nf_max=a.nf_max,
                              l0_nf_max=a.l0_nf_max, same_name_p=a.same_name_p)
    print("  ---- smoke")
    print(f"    latent render 範例：{tok.decode(X[0], skip_special_tokens=True)!r}")
    assert "是 多少" in tok.decode(X[0]), "問句不見了"
    assert "|" not in tok.decode(X[0], skip_special_tokens=True), \
        "latent render 的文字裡不該有事實區"
    assert Z.shape[-1] == S.LAT_DIM
    print(f"    Z shape={tuple(Z.shape)}（batch 內 carrier 數一致）")
    # 身份必須可分辨：同值同屬性、不同名字的 latent 不得相同
    z1, z2 = S.fact_latent2("dan", 1, 47), S.fact_latent2("anna", 1, 47)
    d = float((z1 - z2).norm())
    print(f"    ‖z'(dan b=47) − z'(anna b=47)‖ = {d:.3f}（舊 schema 是 0.000）")
    assert d > 0.5, "身份沒有進 latent —— 又回到舊 schema 的碰撞"
    # 不給 memory_carriers 時必須逐位元與純文字路徑相同
    with torch.no_grad():
        Xd = X.to(DEVICE)
        l1 = m(Xd).logits
        l2 = m(Xd, memory_carriers=None).logits
    print(f"    memory_carriers=None 的 bit-compat：Δ={float((l1-l2).abs().max()):.2e}")
    assert float((l1 - l2).abs().max()) == 0.0
    print()

    # ---- (b′) fresh-init calibration ------------------------------------
    # ⚠️ 校準值必須來自**本 run 自己的隨機初始化起點**。
    #    舊 checkpoint 量到的 6.82× 是**微調起點**的失衡，只作動機，
    #    **不得冒充校準值**（Codex [147]）。
    #    量完立刻 reset 回同一 state 並鎖死權重，全程不看後續 loss/accuracy 調整。
    if a.calibrate_hard_w:
        _snap = {k: v.detach().clone() for k, v in m.state_dict().items()}
        gnorm = {}
        for _sn, _lab in ((False, "easy"), (True, "hard")):
            _r = random.Random(a.seed + 1234)          # **固定的 calibration batch**
            _acc = []
            for _ in range(6):
                _X, _Y, _Z, _, _H = build_batch(tok, _r, a.bs, a.js, latent_p=1.0,
                                                nf_min=2, nf_max=2,
                                                same_name_p=(1.0 if _sn else 0.0))
                _l = forward_loss(m, proj, _X.to(DEVICE), _Y.to(DEVICE),
                                  _Z.to(DEVICE), cfg)
                m.zero_grad(set_to_none=True); _l.backward()
                _acc.append(float(torch.sqrt(sum((p.grad ** 2).sum()
                                                 for p in m.parameters()
                                                 if p.grad is not None))))
            gnorm[_lab] = sum(_acc) / len(_acc)
        m.load_state_dict(_snap); m.zero_grad(set_to_none=True)   # **reset**
        a.hard_w = gnorm["easy"] / max(gnorm["hard"], 1e-8)
        print(f"  ---- fresh-init calibration（正式更新前，已 reset 回同一初始狀態）")
        print(f"    G_easy={gnorm['easy']:.3f}  G_hard={gnorm['hard']:.3f}  "
              f"→ **w_hard = {a.hard_w:.4f}（鎖死，全程不再調整）**\n")

    opt = torch.optim.AdamW(
        [{"params": [p for k, p in m.named_parameters()
                     if p.dim() >= 2 and "embed" not in k and "norm" not in k],
          "weight_decay": 0.01},
         {"params": [p for k, p in m.named_parameters()
                     if p.dim() < 2 or "embed" in k or "norm" in k],
          "weight_decay": 0.0}], lr=a.lr, betas=(0.9, 0.95))
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps, pct_start=0.03)
    scaler = torch.amp.GradScaler("cuda", enabled=DEVICE == "cuda")
    rng = random.Random(a.seed)
    m.train(); t0 = time.time()
    ema = {}
    for step in range(1, a.steps + 1):
        X, Y, Z, ul, H = build_batch(tok, rng, a.bs, a.js,
                                  nf_min=a.nf_min, nf_max=a.nf_max,
                              l0_nf_max=a.l0_nf_max, same_name_p=a.same_name_p)
        X, Y = X.to(DEVICE), Y.to(DEVICE)
        Z = Z.to(DEVICE) if Z is not None else None
        s0 = time.time()
        with torch.amp.autocast("cuda", dtype=torch.bfloat16, enabled=DEVICE == "cuda"):
            loss = forward_loss(m, proj, X, Y, Z, cfg, H, a.hard_w)
        opt.zero_grad(set_to_none=True)
        scaler.scale(loss).backward()
        scaler.unscale_(opt); torch.nn.utils.clip_grad_norm_(m.parameters(), 1.0)
        scaler.step(opt); scaler.update(); sch.step()
        k = "latent" if ul else "L0"
        ema[k] = loss.item() if k not in ema else 0.98 * ema[k] + 0.02 * loss.item()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time() - s0) * (1 / a.throttle - 1))
        if step % 500 == 0:
            print(f"  step {step:6d}/{a.steps}  L0={ema.get('L0', float('nan')):.4f}  "
                  f"latent={ema.get('latent', float('nan')):.4f}  "
                  f"{step/(time.time()-t0):.1f} it/s  {(time.time()-t0)/60:.0f} min", flush=True)
        if a.ckpt_every and step % a.ckpt_every == 0:
            # ⚠️ 保留**中途** checkpoint。上一輪每 4000 步覆寫同一個檔，
            #    導致無法回頭量「訓練中途各 stratum 的梯度質量」——那是必要的證據。
            torch.save({"model": m.state_dict(), "arch": ARCH, "step": step,
                        "vocab": len(tok), "lat_dim": S.LAT_DIM,
                        "addr_dim": S.ADDR_DIM, "carrier_scale": float(proj.scale)},
                       os.path.join(HERE, a.out.replace(".pth", f"_s{step}.pth")))
        if step % 4000 == 0 or step == a.steps:
            torch.save({"model": m.state_dict(), "arch": ARCH, "step": step,
                        "vocab": len(tok), "lat_dim": S.LAT_DIM,
                        "addr_dim": S.ADDR_DIM, "carrier_scale": float(proj.scale)},
                       os.path.join(HERE, a.out))
    print(f"  -> {a.out}")
    json.dump({"prereg": "BRIDGE_PREREG_latent.md", "steps": a.steps,
               "lat_dim": S.LAT_DIM, "addr_dim": S.ADDR_DIM,
               "carrier_scale": float(proj.scale), "ema": ema},
              open(os.path.join(HERE, "results_bridge_core_latent.json"), "w"),
              indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
