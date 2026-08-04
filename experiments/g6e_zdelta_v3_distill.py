"""G6e：**`zdelta-v3-distill`** —— 全位置 functional alignment（text-teacher KL）。

§4.47 的機制：**任務 margin 沒縮小**（`m_text` 平坦，這是**負控制**），
惡化的是**交付造成的 margin 位移 `Δm`**，且**負尾巴比中位數惡化得快**。

因為 `m_text` 對交付參數是常數，`m_text + Δm = m_delivery` ——
**所以目標直接就是「把交付後的 margin 推離 0」**，這是 decision-relevant 量，
**不是** hidden L2（`j=0` 相對偏移 0.42 卻 100% 正確，差異大量落在 task-null 方向）。

### 事前鎖死（跑之前）

- **teacher = 同 episode、同輸出位置的 frozen text-carry**；
  對 delivery logits 做 **stop-gradient KL**（答案位置 + EOS），**再保留原 task CE**。
  **不蒸餾 scalar margin**（Codex）：`max_wrong` 的身分會切換，
  且只保留一個競爭者 —— KL 對齊的是**完整的 decision geometry**，
  又不回到 hidden-space matching。
- **`T = 1`、`λ_KL = 1` 事前鎖定**（無調參預設）；**不得**看到 `j=3` 之後換權重。
- ⚠️ 這**不再宣稱 tail-aware training** —— 準確說是
  「**全位置 functional alignment，以 held-out tail risk 裁決**」。
- **train 覆蓋與 v2 完全相同**（`j≤2`）—— 這樣 v2 vs v3 的差別**只有 loss**。
- **分級評測**：
  **`j=3` = primary extrapolation**；**`j=4`／`j=5` = locked stress**。
  三者**全程不得**用來 early-stop、選 loss 或調超參。
- 每個 `j` 都配**同 episodes 的 text carry**；若 text baseline 自身
  **跌破 95%**，該 `j` 標為 **executor-censored**，**不得**用來裁 delivery。
- **primary gate**：`j=3` 不劣於 text 超過 **5pp**；`j=4/5` **不比 v1 惡化**；
  且 train-horizon 與 no-regression anchors 全保留。

⚠️ `zdelta-v3` 是新 checkpoint；**v1/v2 的裁決不改寫**。core 全程凍結。
⚠️ 「壓尾巴」與「保持多步可讀」目前是**同一目標的兩種描述**，
   **不是**兩個已分辨的機制（Codex）。
"""
import argparse, json, os, random, sys, time
import torch, torch.nn.functional as F
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import g1_renderer as R, g3a_train as G3, g3b_closure as G3B, g3d_scale_audit as G3D
import g5b_segmented_reasoning as G5B, g6a_zdelta_v2 as G6A
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from model.memory_module import ADDR_DIM, PERM_N, perm_to_latent

HERE = os.path.dirname(os.path.abspath(__file__)); IDENT = list(range(PERM_N))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]
T_KL, LAM = 1.0, 1.0            # **事前鎖定**，不得調
TRAIN = [(0, "allph"), (0, "mixed"), (1, "allph"), (1, "mixed"), (2, "allph")]
PRIMARY = [(3, "allph"), (3, "mixed")]                   # primary extrapolation
STRESS = [(4, "allph"), (4, "mixed"), (5, "allph"), (5, "mixed")]   # locked stress


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--bs", type=int, default=24)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=313131)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    if a.smoke: a.steps = 400

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl1, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                      "results_g2b.json")
    mk = lambda: make_delivery("zdelta", BACKBONE["num_hidden_layers"],
                               ARCH["num_loops"],
                               os.path.join(HERE, "g1_renderer_artifact.json"))
    dl2 = mk(); dl2.load_state_dict(torch.load(os.path.join(HERE, "zdelta_v2.pth"),
                                               map_location="cpu")); dl2.eval()
    dl = mk(); dl.load_state_dict(dl1.state_dict())       # v3 從 v1 起手
    for p in m.parameters(): p.requires_grad_(False)
    for p in dl.parameters(): p.requires_grad_(True)
    _, perms = R.perm_splits()
    print(f"  **只訓 `zdelta-v3`**；core 凍結。train 覆蓋與 v2 **完全相同** → 差別只有 loss")
    print(f"  **text-teacher stop-grad KL**（T={T_KL}, λ={LAM} 事前鎖定），全位置都有梯度")
    print(f"  primary = {PRIMARY}；**locked stress** = {STRESS}（不得用來調任何東西）\n")

    opt = torch.optim.AdamW(dl.parameters(), lr=a.lr, weight_decay=0.01)
    sch = torch.optim.lr_scheduler.OneCycleLR(opt, a.lr, total_steps=a.steps, pct_start=0.05)
    rng = random.Random(a.seed); dl.train(); t0 = time.time()
    for step in range(1, a.steps + 1):
        j, cfg = TRAIN[step % len(TRAIN)]
        items = [G6A.make_item(rng, perms, j, cfg) for _ in range(a.bs)]
        X, Y, pos = G6A.batch(tok, items)
        lat = torch.stack([torch.stack([perm_to_latent(v) for v in d])
                           for _, d, _ in items]).to(DEVICE)
        s0 = time.time(); box = {"i": 0}
        def fn(xk, xv, _l=lat, _p=pos.to(DEVICE)):
            i = box["i"]; box["i"] = (i + 1) % NL
            k, v = dl(i, _l, xk[:, _p], xv[:, _p]); return _p, k, v
        lg = m(X.to(DEVICE), kv_override=fn).logits
        Yd = Y.to(DEVICE)
        ce = F.cross_entropy(lg[:, :-1].reshape(-1, lg.size(-1)).float(),
                             Yd[:, 1:].reshape(-1), ignore_index=-100)
        # ---- text-teacher 的 stop-gradient KL（全位置都有梯度）----
        with torch.no_grad():
            tX, tY, _ = G6A.batch(tok, [([v for v in d] + [s_ for s_ in sl
                                                           if s_ is not None], d, g)
                                        for sl, d, g in items])
            tlg = m(tX.to(DEVICE)).logits
        sh, tgt = lg[:, :-1].float(), Yd[:, 1:]
        msk = tgt != -100
        if msk.any():
            th = tlg[:, :-1].float()[msk]           # teacher（凍結、stop-grad）
            hinge = F.kl_div(F.log_softmax(sh[msk] / T_KL, -1),
                             F.softmax(th / T_KL, -1),
                             reduction="batchmean") * (T_KL ** 2)
        else:
            hinge = ce * 0
        loss = ce + LAM * hinge
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(dl.parameters(), 1.0)
        opt.step(); sch.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time()-s0)*(1/a.throttle-1))
        if step <= 24 or step % 300 == 0:
            print(f"  step {step:4d} [j={j} {cfg}] ce={ce.item():.4f} "
                  f"KL={hinge.item():.4f} {step/(time.time()-t0):.1f} it/s", flush=True)

    dl.eval(); n = 100 if not a.smoke else 40
    print(f"\n  {'格子':<14s} {'v1':>7s} {'v2':>7s} {'**v3**':>7s} {'text':>7s} "
          f"{'v3−text':>8s} {'標記':>14s}")
    out = {}
    for j, cfg in TRAIN + PRIMARY + STRESS:
        acc = {}
        for nm, D in (("v1", dl1), ("v2", dl2), ("v3", dl)):
            ok = 0
            for i in range(n):
                r = random.Random(hash((j, cfg, i, a.seed)) & 0xffffffff)
                slots, deliv, gold = G6A.make_item(r, perms, j, cfg)
                lt = torch.stack([perm_to_latent(v) for v in deliv]).to(DEVICE)
                ok += int(G5B.call_core(m, tok, IDENT, slots, D, lt) == gold)
            acc[nm] = ok / n
        ok = 0
        for i in range(n):
            r = random.Random(hash((j, cfg, i, a.seed)) & 0xffffffff)
            slots, deliv, gold = G6A.make_item(r, perms, j, cfg)
            ps = list(deliv) + [s for s in slots if s is not None]
            ok += int(G5B.call_core(m, tok, IDENT, ps) == gold)
        acc["text"] = ok / n
        cens = acc["text"] < 0.95
        tag = ("**censored**" if cens else
               ("PRIMARY" if (j, cfg) in PRIMARY else
                ("stress" if (j, cfg) in STRESS else "train")))
        print(f"  j={j} {cfg:<9s} {acc['v1']:6.1%} {acc['v2']:6.1%} {acc['v3']:6.1%} "
              f"{acc['text']:6.1%} {(acc['v3']-acc['text'])*100:+7.1f}pp {tag:>14s}")
        out[f"j{j}_{cfg}"] = {**acc, "censored": cens, "tier": tag}
    # ---- primary gate ----
    ok3 = all(out[f"j{j}_{c}"]["censored"] or
              (out[f"j{j}_{c}"]["text"] - out[f"j{j}_{c}"]["v3"]) <= 0.05
              for j, c in PRIMARY)
    okS = all(out[f"j{j}_{c}"]["censored"] or
              out[f"j{j}_{c}"]["v3"] >= out[f"j{j}_{c}"]["v1"] for j, c in STRESS)
    okT = all(out[f"j{j}_{c}"]["v3"] >= 0.99 for j, c in TRAIN)
    print(f"\n  gate：primary j=3 不劣於 text 逾 5pp {'✅' if ok3 else '❌'}   "
          f"stress j=4/5 不比 v1 惡化 {'✅' if okS else '❌'}   "
          f"train no-regression {'✅' if okT else '❌'}")
    print(f"  → **{'PASS' if (ok3 and okS and okT) else 'FAIL'}**")
    print(f"  ⚠️ `text` 跌破 95% 的格子標為 **executor-censored**，不用來裁 delivery。")
    torch.save(dl.state_dict(), os.path.join(HERE, "zdelta_v3_distill.pth"))
    json.dump({"T": T_KL, "lambda": LAM, "train": TRAIN, "primary": PRIMARY, "stress": STRESS,
               "steps": a.steps, "seed": a.seed, "acc": out, "smoke": a.smoke,
               "verdict": "PASS" if (ok3 and okS and okT) else "FAIL"},
              open(os.path.join(HERE, f"results_g6e{'_smoke' if a.smoke else ''}.json"),
                   "w"), indent=2, ensure_ascii=False)
    print(f"  -> zdelta_v3_distill.pth / results_g6e.json")

main()
