"""G6d：**`zdelta-v3`** —— functional, tail-aware, cross-horizon distillation。

§4.47 的機制：**任務 margin 沒縮小**（`m_text` 平坦，這是**負控制**），
惡化的是**交付造成的 margin 位移 `Δm`**，且**負尾巴比中位數惡化得快**。

因為 `m_text` 對交付參數是常數，`m_text + Δm = m_delivery` ——
**所以目標直接就是「把交付後的 margin 推離 0」**，這是 decision-relevant 量，
**不是** hidden L2（`j=0` 相對偏移 0.42 卻 100% 正確，差異大量落在 task-null 方向）。

### 事前鎖死（跑之前）

- **尾部目標數學化**，不在 batch 中動態挑「最差幾題」：
  逐位置 hinge `max(0, ε − m_delivery)`，**`ε = 8.0` 事前鎖定**
  （`m_text` 的最低值約 9.6，所以 ε=8 是要求交付把**尾巴**抬到文字最低值附近，
  而不是要求追平中位數 14.3）。hinge 本身只在尾巴啟動，**天生 tail-aware**。
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
EPS = 8.0                       # **事前鎖定**的 hinge 門檻
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
    print(f"  尾部目標**事前鎖定**：逐位置 hinge `max(0, {EPS} − m_delivery)`")
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
        # ---- tail-aware margin hinge（ε 事前鎖定；hinge 只在尾巴啟動）----
        sh, tgt = lg[:, :-1].float(), Yd[:, 1:]
        msk = tgt != -100
        if msk.any():
            sel = sh[msk]; lab = tgt[msk]
            corr = sel.gather(-1, lab.unsqueeze(-1)).squeeze(-1)
            sel2 = sel.clone(); sel2.scatter_(-1, lab.unsqueeze(-1), -1e9)
            marg = corr - sel2.max(-1).values
            hinge = F.relu(EPS - marg).mean()
        else:
            hinge = ce * 0
        loss = ce + hinge
        opt.zero_grad(set_to_none=True); loss.backward()
        torch.nn.utils.clip_grad_norm_(dl.parameters(), 1.0)
        opt.step(); sch.step()
        if a.throttle < 1.0 and DEVICE == "cuda":
            torch.cuda.synchronize(); time.sleep((time.time()-s0)*(1/a.throttle-1))
        if step % 300 == 0:
            print(f"  step {step:5d}/{a.steps} ce={ce.item():.4f} "
                  f"hinge={hinge.item():.4f} {step/(time.time()-t0):.1f} it/s", flush=True)

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
    torch.save(dl.state_dict(), os.path.join(HERE, "zdelta_v3.pth"))
    json.dump({"eps": EPS, "train": TRAIN, "primary": PRIMARY, "stress": STRESS,
               "steps": a.steps, "seed": a.seed, "acc": out, "smoke": a.smoke,
               "verdict": "PASS" if (ok3 and okS and okT) else "FAIL"},
              open(os.path.join(HERE, f"results_g6d{'_smoke' if a.smoke else ''}.json"),
                   "w"), indent=2, ensure_ascii=False)
    print(f"  -> zdelta_v3.pth / results_g6d.json")

main()
