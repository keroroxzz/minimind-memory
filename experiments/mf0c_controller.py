"""`MF0-C` controller —— **三-seed campaign（Codex [184] 授權，一次跑完）**。

規格全部取自 `MF0C_prereg.json`；**本檔不決定任何規格數字**。

- `h ∈ FP16^16`，每 session reset，runtime state **恰 32 bytes**；每步後立即 quantize。
- `u_t = onehot(goal:3) || onehot(entity:16) || onehot(attr:12)` = **31 維**。
- priority `a_t = b + w_u·u_t + w_h·h_t`（**單一 affine head**，零初始化）。
- **arrival score**：到達時算出、隨 slot 保存；**驅逐時不重算舊 record**。
- train：`|S|<B` 確定性收入；`|S|=B` 對 `C=S∪{new}` 依 `softmax(-a/τ)` **恰驅逐一個**
  （驅逐 `new` ＝ 拒收），`τ=1.0`。
- eval：**確定性** online top-B，同分以 canonical-key hash 打破。
- REINFORCE ＋ **同 session 內** LOO baseline（`G=8` sessions × `M=4` rollouts）。
"""
import hashlib, json, os, statistics, random
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import mf0c_data as D

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "MF0C_prereg.json")))
CL, SL = P["controller_compute_lock"], P["sampling_law"]
DEV = "cuda" if torch.cuda.is_available() else "cpu"
B, E, NA, NC, HD, TAU = 8, 16, 12, 3, 16, 1.0
# ---- CI-1（Codex [185]）：兩臂唯一差異是一個**冗餘的 derived bit** --------
#   `+` : q = 1[cat(attr) == goal]              ← 真的 conjunction
#   `-` : q = 1[cat(attr) == (goal+1) mod 3]    ← 錯位的 decoy
# 兩者都**由既有輸入完全決定**、維度皆 32、邊際皆 1/3。
# 這是**顯式提供 conjunction 的 inductive-bias intervention，不是資訊增加**，
# 因此**永遠不可**稱 natural／emergent formation。
ARM = None                      # None = 原始 MF0-C（31 維，逐位元不變）
IN = NC + E + NA + (0 if ARM is None else 1)
G_SESS, M_ROLL, UPD = 8, 4, 20000
IDX_SEED = 2026081391
CAT_OF = D.CAT_OF


class Ctrl(nn.Module):
    def __init__(self):
        super().__init__()
        self.cell = nn.GRUCell(IN, HD)
        self.head = nn.Linear(IN + HD, 1)
        nn.init.zeros_(self.head.weight); nn.init.zeros_(self.head.bias)

    def step(self, u, h, ablate=False):
        """`h` **以 float16 儲存**（runtime state 恰 32 bytes）。

        `ablate=True` 走 `GRUCell(u, 0)` 後隨即丟棄 —— **禁止跨步 h**，
        且**不是**把 head 歸零（那會混入另一種 per-event capacity 差異）。
        """
        h_in = torch.zeros_like(h, dtype=torch.float32) if ablate else h.float()
        hn = self.cell(u, h_in)
        hn16 = hn.half()                                  # **每步立即 quantize**
        a = self.head(torch.cat([u, hn16.float()], -1)).squeeze(-1)
        return a, (h if ablate else hn16)


def key_hash(e, a):
    return int.from_bytes(hashlib.blake2b(bytes((int(e), int(a))),
                                          digest_size=8).digest(), "big")


KH = torch.tensor([[key_hash(e, a) % (1 << 52) for a in range(NA)]
                   for e in range(E)], dtype=torch.float64)


def act_seed(cseed, upd, grp, rep):
    d = hashlib.blake2b(b"MF0C-action-v1" + b"".join(
        int(x).to_bytes(8, "big") for x in (cseed, upd, grp, rep)),
        digest_size=8).digest()
    return int.from_bytes(d, "big") % (1 << 63)


def sessions_tensor(rows):
    ev = torch.tensor([[[e, a] for e, a, _ in r["events"]] for r in rows])
    gl = torch.tensor([r["goal"] for r in rows])
    return ev, gl


def rollout(m, ev, gl, stochastic, gen=None, ablate=False):
    """回傳 (kept_mask, sum_logp)。`ev`:(Bn,64,2)  `gl`:(Bn,)"""
    Bn, T = ev.shape[0], ev.shape[1]
    h = torch.zeros(Bn, HD, device=DEV, dtype=torch.float16)
    assert h.dtype == torch.float16 and h.shape[1] == HD, "state 必須是 FP16^16（32 bytes）"
    slot_a = torch.full((Bn, B), -1e30, device=DEV)
    slot_i = torch.full((Bn, B), -1, dtype=torch.long, device=DEV)
    slot_t = torch.zeros(Bn, B, dtype=torch.float64, device=DEV)   # tie-break
    logp = torch.zeros(Bn, device=DEV)
    gl1 = F.one_hot(gl, NC).float().to(DEV)
    for t in range(T):
        e, a = ev[:, t, 0].to(DEV), ev[:, t, 1].to(DEV)
        u = torch.cat([gl1, F.one_hot(e, E).float(), F.one_hot(a, NA).float()], -1)
        if ARM is not None:
            cat = torch.tensor(CAT_OF, device=DEV)[a]
            tgt = gl.to(DEV) if ARM == "+" else (gl.to(DEV) + 1) % NC
            u = torch.cat([u, (cat == tgt).float().unsqueeze(1)], -1)
        assert u.shape[1] == IN, f"controller input 必須恰 {IN} 維"
        sc, h = m.step(u, h, ablate)
        assert h.dtype == torch.float16, "兩步之間不得保留 FP32 state"
        tie = KH.to(DEV)[e, a]
        if t < B:
            slot_a[:, t] = sc; slot_i[:, t] = t; slot_t[:, t] = tie
            continue
        cand_a = torch.cat([slot_a, sc.unsqueeze(1)], 1)           # (Bn, B+1)
        if stochastic:
            p = F.softmax(-cand_a / TAU, -1)
            j = torch.multinomial(p, 1, generator=gen).squeeze(1)
            logp = logp + torch.log(p.gather(1, j.unsqueeze(1)).squeeze(1) + 1e-12)
        else:                                                      # 確定性：踢最小
            cand_t = torch.cat([slot_t, tie.unsqueeze(1).double()], 1)
            key = cand_a.double() * 1e18 - cand_t                  # 分數優先、同分比 hash
            j = key.argmin(1)
        keep_new = j != B
        idx = j.clamp(max=B - 1)
        r = torch.arange(Bn, device=DEV)
        slot_a[r[keep_new], idx[keep_new]] = sc[keep_new]
        slot_i[r[keep_new], idx[keep_new]] = t
        slot_t[r[keep_new], idx[keep_new]] = tie[keep_new].double()
    return slot_i, logp


def utility(rows, slot_i):
    out = []
    for k, r in enumerate(rows):
        kept = {(r["events"][i][0], r["events"][i][1])
                for i in slot_i[k].tolist() if i >= 0}
        out.append(sum(tuple(q) in kept for q in r["queries"]) / len(r["queries"]))
    return out


def evaluate(m, rows, ablate=False):
    ev, gl = sessions_tensor(rows)
    with torch.no_grad():
        si, _ = rollout(m, ev, gl, stochastic=False, ablate=ablate)
    return utility(rows, si)


def boot_ci(d, n=10000, seed=0):
    rng = random.Random(seed)
    ms = sorted(statistics.fmean(rng.choices(d, k=len(d))) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n)]


def main():
    v2 = json.load(open(os.path.join(HERE, "mf0c_artifact_v2.json")))
    train = v2["W1|train"]
    old = json.load(open(os.path.join(HERE, "mf0c_artifact.json")))
    rep_x = json.load(open(os.path.join(HERE, "mf0c_eval_replicas.json")))
    reps = {"r0": {w: old[f"{w}|eval"] for w in ("W0", "W1")}}
    for t in ("r1", "r2"):
        reps[t] = {w: rep_x[t][f"{w}|eval"] for w in ("W0", "W1")}

    ir = np.random.RandomState(IDX_SEED)
    sched = ir.randint(0, len(train), size=(UPD, G_SESS))
    sched_hash = hashlib.sha256(sched.tobytes()).hexdigest()[:16]
    T_r = {k: float(v.rstrip("p")) / 100 for k, v in P["primary_gate"]["T_r"].items()
           if k.startswith("r")}
    print("  MF0-C controller campaign（Codex [184] 授權，一次跑完）")
    print(f"  index schedule seed={IDX_SEED} hash={sched_hash}；"
          f"G={G_SESS}×M={M_ROLL}，updates={UPD}")
    print(f"  T_r = {T_r}\n")

    out, fails = {}, []
    for cseed in CL["seeds"]:
        torch.manual_seed(cseed)
        m = Ctrl().to(DEV)
        opt = torch.optim.AdamW(m.parameters(), lr=3e-4, betas=(0.9, 0.999),
                                weight_decay=0.0)
        for upd in range(UPD):
            rows = [train[i] for i in sched[upd]]
            rows_m = [r for r in rows for _ in range(M_ROLL)]
            ev, gl = sessions_tensor(rows_m)
            gen = torch.Generator(device=DEV)
            gen.manual_seed(act_seed(cseed, upd, 0, 0) % (1 << 31))
            si, lp = rollout(m, ev, gl, stochastic=True, gen=gen)
            R = torch.tensor(utility(rows_m, si), device=DEV).view(G_SESS, M_ROLL)
            b = (R.sum(1, keepdim=True) - R) / (M_ROLL - 1)        # **同 session 內** LOO
            adv = (R - b).detach().view(-1)
            loss = -(adv * lp).mean()
            opt.zero_grad(set_to_none=True); loss.backward()
            nn.utils.clip_grad_norm_(m.parameters(), 1.0)
            opt.step()
            if (upd + 1) % 4000 == 0:
                print(f"    seed {cseed} upd {upd+1}/{UPD} R={float(R.mean()):.3f}")

        r = {}
        for t in ("r0", "r1", "r2"):
            u_l = evaluate(m, reps[t]["W1"])
            ref = {}
            for name in ("goal_only", "focus_only"):
                import mf0c_h_audit as HA
                u_r = [HA.utility(s, HA.run_online(s["events"],
                                                   HA.scores_for(s, name)))
                       for s in reps[t]["W1"]]
                d = [x - y for x, y in zip(u_l, u_r)]
                lo, hi = boot_ci(d)
                ok = statistics.fmean(d) >= T_r[t] and lo > 0
                if not ok:
                    fails.append(f"seed{cseed}/{t}/{name}")
                ref[name] = {"mean_d": statistics.fmean(d), "ci": [lo, hi],
                             "T_r": T_r[t], "pass": ok}
            u_w0 = evaluate(m, reps[t]["W0"])
            u_h0 = evaluate(m, reps[t]["W1"], ablate=True)     # h=0 state-ablation
            r[t] = {"U_learned_W1": statistics.fmean(u_l),
                    "U_learned_W0": statistics.fmean(u_w0),
                    "U_h0_ablation_W1": statistics.fmean(u_h0), "vs": ref}
            print(f"    seed {cseed} {t}  W1 {statistics.fmean(u_l):.1%}  "
                  + "  ".join(f"{k} Δ{v['mean_d']:+.1%}[{v['ci'][0]:+.1%},"
                              f"{v['ci'][1]:+.1%}]{'✓' if v['pass'] else '✗'}"
                              for k, v in ref.items())
                  + f"  | W0 {statistics.fmean(u_w0):.1%}"
                  f"  h0 {statistics.fmean(u_h0):.1%}")
        out[str(cseed)] = r

    print()
    print(f"  → **MF0-C {'PASS' if not fails else 'FAIL'}**"
          + ("" if not fails else f"（{len(fails)} 格：{fails[:6]}）"))
    json.dump({"index_schedule_hash": sched_hash, "T_r": T_r,
               "seeds": out, "fails": fails, "pass": not fails},
              open(os.path.join(HERE, "results_mf0c_controller.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_mf0c_controller.json")


if __name__ == "__main__":
    main()
