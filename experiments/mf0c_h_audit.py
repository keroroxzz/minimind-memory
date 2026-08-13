"""`MF0-C` **H audit** —— **不訓練**（Codex [183] 於 reference smoke 通過後授權一次）。

    H_g = U(π_content*) − U(goal-only)
    H_f = U(π_content*) − U(focus-only)

對 `r0/r1/r2` **各自**計算，配對 bootstrap 95% CI。

**near-threshold 規則（事前鎖死，無灰區）**：
以**未四捨五入**值判；**六個 CI upper 中任一 `<= 0.050000`** 即
`causally non-discriminating`、**不訓練**。只有**全部六個** `> 0.050000` 才准 controller train。
**不得**加樣本／pool／換 CI／改 reference。

另報（Codex [181]）：`U(P0) − U(random)` 的配對 CI、`P0` score 的 mean/SD 與 tie-rate。
"""
import json, os, random, statistics
from fractions import Fraction

import numpy as np
import torch
import torch.nn.functional as F

import mf0c_data as D
import mf0c_ref as R
from mf0c_p0_train import P0, W as P0_W, N_ENT, N_ATTR as P0_NA, N_CAT as P0_NC

HERE = os.path.dirname(os.path.abspath(__file__))
B, E = 8, 16
CAT_OF = D.CAT_OF


def key_hash(e, a):
    """事前固定的 canonical-key hash —— 唯一的 tie-break。"""
    import hashlib
    return int.from_bytes(hashlib.blake2b(bytes((e, a)), digest_size=8).digest(), "big")


def run_online(events, scores):
    """線上、恰 `B` 個 slot 的**確定性** top-B；`scores[t]` 是**到達時**算出的 arrival score。

    ⚠️ 驅逐時**不重算**舊 record 的分數（Codex [182]）。
    """
    S = []                                   # [(score, tiebreak, (e,a))]
    for t, (e, a, _) in enumerate(events):
        item = (scores[t], -key_hash(e, a), (e, a))
        if len(S) < B:
            S.append(item)
            continue
        j = min(range(B), key=lambda i: (S[i][0], S[i][1]))
        if (item[0], item[1]) > (S[j][0], S[j][1]):
            S[j] = item
    return {x[2] for x in S}


def utility(sess, kept):
    return sum(tuple(q) in kept for q in sess["queries"]) / len(sess["queries"])


def scores_for(sess, policy, p0=None, dev="cpu"):
    ev = sess["events"]
    goal = sess["goal"]
    if policy == "recency":
        return [Fraction(t) for t in range(len(ev))]
    if policy == "random":
        rr = random.Random(key_hash(0, 0) ^ hash(tuple(x[0] for x in ev)) % (1 << 30))
        return [Fraction(rr.randrange(1 << 30)) for _ in ev]
    if policy == "goal_only":
        return [Fraction(1 if CAT_OF[a] == goal else 0) for e, a, _ in ev]
    if policy in ("focus_only", "pi_content"):
        out, c = [], [0] * E
        for e, a, _ in ev:
            c[e] += 1                               # **含當前 event**
            w = R.posterior_weights(c)
            tot = sum(w)
            p = Fraction(w[e], tot) if tot else Fraction(0)
            out.append(p if policy == "focus_only"
                       else (p if CAT_OF[a] == goal else Fraction(0)))
        return out
    if policy == "P0":
        ctx = torch.zeros(len(ev), P0_W, 2, dtype=torch.long)
        clen = torch.zeros(len(ev), dtype=torch.long)
        for t in range(len(ev)):
            lo = max(0, t - P0_W)
            c = [(e, a) for e, a, _ in ev[lo:t]]
            clen[t] = len(c)
            if c:
                ctx[t, :len(c)] = torch.tensor(c)
        with torch.no_grad():
            le, la = p0(ctx.to(dev), clen.to(dev),
                        torch.full((len(ev),), goal).to(dev))
            lpe = F.log_softmax(le, -1)
            lpa = F.log_softmax(la, -1)
        tgt = torch.tensor([[e, a] for e, a, _ in ev])
        s = -0.5 * (lpe[range(len(ev)), tgt[:, 0]] + lpa[range(len(ev)), tgt[:, 1]])
        return [Fraction(float(x)).limit_denominator(10**9) for x in s.cpu()]
    raise ValueError(policy)


def oracle_keep(sess):
    from collections import Counter
    cnt = Counter(tuple(q) for q in sess["queries"])
    recs = [(e, a) for e, a, _ in sess["events"]]
    return set(sorted(recs, key=lambda k: (-cnt[k], -key_hash(*k)))[:B])


def boot_ci(d, n=10000, seed=0):
    rng = random.Random(seed)
    ms = sorted(statistics.fmean(rng.choices(d, k=len(d))) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n)]


def main():
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    blob = torch.load(os.path.join(HERE, "mf0c_p0.pth"), map_location=dev)
    p0 = P0().to(dev); p0.load_state_dict(blob["model"]); p0.eval()

    old = json.load(open(os.path.join(HERE, "mf0c_artifact.json")))
    reps_extra = json.load(open(os.path.join(HERE, "mf0c_eval_replicas.json")))
    reps = {"r0": {w: old[f"{w}|eval"] for w in ("W0", "W1")}}
    for t in ("r1", "r2"):
        reps[t] = {w: reps_extra[t][f"{w}|eval"] for w in ("W0", "W1")}

    print("  MF0-C H audit（**不訓練**；Codex [183] 授權一次）")
    print("  規則：六個 CI upper 中**任一 <= 0.050000** 即 causally non-discriminating\n")
    POL = ("random", "recency", "goal_only", "focus_only", "pi_content", "P0")
    res, uppers = {}, []
    print(f"  {'rep':>4s} {'world':>5s} " + " ".join(f"{p:>10s}" for p in POL)
          + f" {'oracle':>10s}")
    for t in ("r0", "r1", "r2"):
        for world in ("W1", "W0"):
            sess = reps[t][world]
            U = {p: [] for p in POL}
            U["oracle"] = []
            for s in sess:
                for p in POL:
                    U[p].append(utility(s, run_online(s["events"],
                                                      scores_for(s, p, p0, dev))))
                U["oracle"].append(utility(s, oracle_keep(s)))
            print(f"  {t:>4s} {world:>5s} "
                  + " ".join(f"{statistics.fmean(U[p]):>9.1%}" for p in POL)
                  + f" {statistics.fmean(U['oracle']):>9.1%}")
            r = {p: statistics.fmean(U[p]) for p in list(POL) + ["oracle"]}
            if world == "W1":
                for tag, base in (("H_g", "goal_only"), ("H_f", "focus_only")):
                    d = [x - y for x, y in zip(U["pi_content"], U[base])]
                    lo, hi = boot_ci(d)
                    r[tag] = {"mean": statistics.fmean(d), "ci": [lo, hi]}
                    uppers.append(hi)
            dp0 = [x - y for x, y in zip(U["P0"], U["random"])]
            r["P0_minus_random"] = {"mean": statistics.fmean(dp0),
                                    "ci": list(boot_ci(dp0))}
            res[f"{t}|{world}"] = r

    print(f"\n  {'rep':>4s} {'H_g mean':>10s} {'H_g CI':>18s} "
          f"{'H_f mean':>10s} {'H_f CI':>18s}")
    for t in ("r0", "r1", "r2"):
        r = res[f"{t}|W1"]
        print(f"  {t:>4s} {r['H_g']['mean']:>9.1%} "
              f"[{r['H_g']['ci'][0]:>6.1%},{r['H_g']['ci'][1]:>6.1%}] "
              f"{r['H_f']['mean']:>9.1%} "
              f"[{r['H_f']['ci'][0]:>6.1%},{r['H_f']['ci'][1]:>6.1%}]")

    nondisc = any(u <= 0.050000 for u in uppers)
    print(f"\n  六個 CI upper（未四捨五入）：{[round(u,6) for u in uppers]}")
    print("  → **causally non-discriminating，不訓練 controller**" if nondisc
          else "  → **全部六個 > 0.050000，controller train 有資格**")
    print("\n  P0 描述性（Codex [181]）：U(P0) − U(random) 的配對 CI")
    for k in res:
        d = res[k]["P0_minus_random"]
        print(f"    {k:>8s}  {d['mean']:>7.1%}  [{d['ci'][0]:>6.1%},{d['ci'][1]:>6.1%}]")
    json.dump({"results": res, "uppers": uppers,
               "causally_non_discriminating": nondisc},
              open(os.path.join(HERE, "results_mf0c_h_audit.json"), "w"),
              indent=2, ensure_ascii=False, default=float)
    print("  -> results_mf0c_h_audit.json")


if __name__ == "__main__":
    main()
