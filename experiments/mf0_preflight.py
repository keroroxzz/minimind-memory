"""`MF-0` 的 **W1 headroom preflight** —— **不訓練任何東西**（Codex [175]）。

問一件事：**W1 對 content-aware admission 到底有沒有可測的空間？**

    Δ = U_oracle − U_recency

若 300 個**配對 session** bootstrap 的 95% **上限** `<= 5pp`，
則 **`W1 non-discriminating`** —— 停止該 world 的 gate training，
**不浪費訓練去證明一個預期中的 null**。
⚠️ 這**不等於**「formation 研究不存在」（Codex [175] 的收窄）。

不需要模型：query 是 **atomic exact-key read**，
效用 ＝ 被問到的 record 是否還在容量內。純組合計算。

`frozen surprise` **本檔不算** —— 它需要一個 frozen LM 對事件文字取預測誤差。
**捏一個假的 surprise 比不報更糟**；它的定義留給 prereg，
且**決定性的量 `Δ = oracle − recency` 不需要它**。
"""
import json, os, random, sys
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(HERE)
import ng_o2 as N

# ---- 規格（Codex [174] 鎖定；本檔不得自行更改）----------------------------
N_EVENT, BUDGET, N_QUERY, WINDOW = 64, 8, 16, 4
N_SESSION = 300
SEEDS = (20260812, 20260813, 20260814)

# ---- generator 的自由度：**一次選定並公開，不得看到 Δ 再調** ----------------
# 12 個屬性分成 3 個類別，每類 4 個；session goal ＝ 其中一類。
# 於是 goal-relevant 的比例**由結構決定為 1/3**，不是我挑出來的數字。
N_CAT = 3
CATS = [N.ATTRS[i::N_CAT] for i in range(N_CAT)]
ATTR_CAT = {a: c for c, g in enumerate(CATS) for a in g}


def session(rng, world):
    """一個 session：`N_EVENT` 筆事件 ＋ `N_QUERY` 個 query。

    `W1`：query **只從 goal 類別**的已寫入 record 抽 —— goal 在事件到達時已可見。
    `W0`：query 從**全部**已寫入 record 均勻抽；**goal 仍然存在於表面**，
          但與 query target **獨立**（兩個 world 的表面長得一樣）。
    """
    goal = rng.randrange(N_CAT)
    seen, events = {}, []
    for t in range(N_EVENT):
        ent = N.NAMES[rng.randrange(len(N.NAMES))]
        attr = N.ATTRS[rng.randrange(len(N.ATTRS))]
        k = (ent, attr)
        seen[k] = t                                   # 同 key 重寫則更新時間
        events.append((t, k, ATTR_CAT[attr] == goal))
    recs = list(seen)
    pool = [k for k in recs if ATTR_CAT[k[1]] == goal] if world == "W1" else recs
    if not pool:
        return None
    queries = [pool[rng.randrange(len(pool))] for _ in range(N_QUERY)]
    return {"goal": goal, "events": events, "recs": recs, "queries": queries}


def policy_keep(s, policy, rng):
    """回傳保留下來的 `BUDGET` 個 key。**全部是線上、因果的**（oracle 除外）。"""
    ev = s["events"]
    if policy == "oracle":
        # 事後按 future-use count 選 top-B。**不可部署的上界**，明列。
        from collections import Counter
        c = Counter(s["queries"])
        return set(sorted(s["recs"], key=lambda k: -c[k])[:BUDGET])
    if policy == "recency":
        keep = []
        for _, k, _ in ev:                            # 線上：新的擠掉最舊的
            if k in keep:
                keep.remove(k)
            keep.append(k)
            if len(keep) > BUDGET:
                keep.pop(0)
        return set(keep)
    if policy == "random":
        keep = []
        for _, k, _ in ev:
            if k in keep:
                continue
            if len(keep) < BUDGET:
                keep.append(k)
            elif rng.random() < BUDGET / (len(keep) + 1):
                keep[rng.randrange(BUDGET)] = k       # reservoir
        return set(keep)
    if policy == "goal_oracle_policy":
        # **不是 learned gate** —— 只是「若 policy 能看懂 goal 會如何」的參考點。
        # 它有 goal 但沒有 future-use count，介於 recency 與 oracle 之間。
        keep = []
        for _, k, rel in ev:
            if k in keep:
                keep.remove(k)
            if rel:
                keep.append(k)
            elif len(keep) < BUDGET:
                keep.append(k)
            if len(keep) > BUDGET:
                for i, x in enumerate(keep):          # 優先丟掉非 goal 的
                    if ATTR_CAT[x[1]] != s["goal"]:
                        keep.pop(i); break
                else:
                    keep.pop(0)
        return set(keep)
    raise ValueError(policy)


def utility(s, keep):
    """效用 ＝ 被問到的 record 仍在容量內的比例（其餘為正確 abstain）。"""
    return sum(q in keep for q in s["queries"]) / len(s["queries"])


def boot_ci(diffs, n=10000, seed=0):
    """配對 bootstrap 的 95% CI（配對 ＝ 同一 session 下兩個 policy 的差）。"""
    rng = random.Random(seed)
    ms = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs)))
                for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n)]


def main():
    print("  MF-0 W1 headroom preflight —— **不訓練任何東西**（Codex [175]）")
    print(f"  N={N_EVENT} events／session，B={BUDGET}，Q={N_QUERY}，"
          f"{N_SESSION} sessions／world／seed")
    print(f"  goal-relevant 比例由結構決定為 1/{N_CAT}（12 屬性分 3 類），**非挑選**")
    print("  frozen surprise 本檔不算 —— 需要 frozen LM；捏一個假的比不報更糟\n")

    res = {}
    print(f"  {'world':>6s} {'seed':>10s} {'random':>8s} {'recency':>8s} "
          f"{'goal*':>8s} {'oracle':>8s} {'Δ=orc-rec':>11s} {'95% CI':>16s}")
    for world in ("W0", "W1"):
        for seed in SEEDS:
            rng = random.Random(seed)
            sess = []
            while len(sess) < N_SESSION:
                s = session(rng, world)
                if s:
                    sess.append(s)
            u = {p: [] for p in ("random", "recency", "goal_oracle_policy", "oracle")}
            for s in sess:
                for p in u:
                    u[p].append(utility(s, policy_keep(s, p, rng)))
            d = [o - r for o, r in zip(u["oracle"], u["recency"])]
            lo, hi = boot_ci(d)
            res[f"{world}|{seed}"] = {
                **{p: statistics.fmean(v) for p, v in u.items()},
                "delta": statistics.fmean(d), "ci": [lo, hi]}
            print(f"  {world:>6s} {seed:>10d} "
                  f"{statistics.fmean(u['random']):>7.1%} "
                  f"{statistics.fmean(u['recency']):>7.1%} "
                  f"{statistics.fmean(u['goal_oracle_policy']):>7.1%} "
                  f"{statistics.fmean(u['oracle']):>7.1%} "
                  f"{statistics.fmean(d):>10.1%} "
                  f"[{lo:>5.1%},{hi:>5.1%}]")

    print("\n  判準（Codex [175]，事前鎖定）：")
    print("    W1 的 Δ 若 **95% 上限 <= 5pp** → `W1 non-discriminating`，")
    print("    停止該 world 的 gate training，**不等於**「formation 研究不存在」。")
    ups = [res[f"W1|{s}"]["ci"][1] for s in SEEDS]
    nondisc = all(u <= 0.05 for u in ups)
    print(f"\n    W1 各 seed 的 Δ 95% 上限：{['%.1f%%' % (u*100) for u in ups]}")
    if nondisc:
        print("    → **W1 non-discriminating**：轉 `RWA-0` 設計 review。")
    else:
        print("    → **W1 有 headroom**：依已鎖的 W0/W1 規格起草 MF-0 prereg，一次跑完。")
    print("\n  ⚠️ W0 是**負對照**：`random`／`recency` 的期望應相等；")
    print("     任何穩定優勢**先查漏洩**，不得先當成能力。")
    json.dump({"spec": {"N": N_EVENT, "B": BUDGET, "Q": N_QUERY,
                        "sessions": N_SESSION, "n_cat": N_CAT},
               "results": res, "w1_nondiscriminating": nondisc},
              open(os.path.join(HERE, "results_mf0_preflight.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_mf0_preflight.json")


if __name__ == "__main__":
    main()
