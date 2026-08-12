"""`EC-1` exact-key conflict / overwrite contract —— **凍結有限 trace 的窮舉**。

授權：Codex [167]（`EC1_prereg.json`）。**無學習成分、無抽樣、無 seed、無 CI。**

窮舉的是**已鎖的 operation／transition domain**，
**不冒充**窮舉連續 latent 空間。全部 48 個 canonical key，
每 key 三個事前固定且相異的 witness（`zA/zB/zC`），跑同一條全序 trace：

    new(v0,zA) → duplicate(v0,zA) → same-version-different reject(v0,zB)
    → missing-version reject(None,zB) → newer-same-z(v1,zA)
    → stale reject(v0,zA) → newer-different(v2,zB)
    → stale-different reject(v1,zC) → duplicate(v2,zB)

每一步 assert：target 的前後 snapshot、**所有非 target key 的 snapshot 不變**、
status 與預期相符、`verify() == 0`、且 reject 時 **injector 呼叫增量為 0**。

**scope 只限單程序、全序 commit。** 不涵蓋 crash／torn／concurrent／stale snapshot。
"""
import json, os, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
import bridge_store as ST
from bridge_latent_gate import gen
from bridge_g3c_dangling import Injector
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "EC1_prereg.json")))

# 三個**事前固定且相異**的 witness value（不是抽樣出來的）
WV = (11, 22, 33)

# (label, witness index or None, version or None, 預期 status, 是否應改變狀態)
TRACE = [
    ("new",                     0, 0,    "new",                     True),
    ("duplicate",               0, 0,    "duplicate",               False),
    ("same-version-different",  1, 0,    "reject_conflict",         False),
    ("missing-version",         1, None, "reject_missing_version",  False),
    ("newer-same-z",            0, 1,    "newer",                   True),
    ("stale",                   0, 0,    "reject_stale",            False),
    ("newer-different",         1, 2,    "newer",                   True),
    ("stale-different",         2, 1,    "reject_stale",            False),
    ("duplicate-final",         1, 2,    "duplicate",               False),
]


def main(core="bridge_core_latent.pth"):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    m = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"],
                                           **arch)).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])
    inj = Injector(proj)
    loops = arch["num_loops"]

    allk = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    print(f"  EC-1 exact-key conflict / overwrite contract（Codex [167] 授權）")
    print(f"  {len(allk)} 個 canonical key × {len(TRACE)} 步全序 trace "
          f"= {len(allk)*len(TRACE)} 次操作；**無抽樣、無 seed**")
    print("  scope：**單程序、全序 commit**；不涵蓋 crash／torn／concurrent\n")

    store = ST.Store()
    bad, log = [], []
    n_step = 0
    for key in allk:
        wit = [S.fact_latent2(key[0], key[1], v) for v in WV]
        for label, wi, ver, want, mutates in TRACE:
            n_step += 1
            before_all = {k: store.entry_snapshot(*k) for k in allk}
            before_tgt = store.entry_snapshot(*key)
            before_hash = store.snapshot()
            c0 = inj.calls

            got = store.commit_v(key[0], key[1], wit[wi], ver)

            after_tgt = store.entry_snapshot(*key)
            after_hash = store.snapshot()
            changed = before_tgt != after_tgt

            # ---- 逐步 assert（任何一條不過就記下來，不中斷，以便看到全貌）
            if got != want:
                bad.append((key, label, f"status {got} != {want}"))
            if changed != mutates:
                bad.append((key, label, f"狀態改變 {changed}，預期 {mutates}"))
            if not mutates and before_hash != after_hash:
                bad.append((key, label, "非 mutating 操作改變了全域 snapshot"))
            for k in allk:                      # **所有非 target key 必須不變**
                if k != key and store.entry_snapshot(*k) != before_all[k]:
                    bad.append((key, label, f"非 target key {k} 被動到"))
            if store.verify() != 0:
                bad.append((key, label, "verify() != 0"))
            if got.startswith("reject") and inj.calls != c0:
                bad.append((key, label, "reject 卻有交付"))
            log.append({"key": list(key), "step": label, "version": ver,
                        "status": got, "before": before_hash, "after": after_hash})

        # ---- trace 結束後：newest accepted read 必須是 (v2, zB)
        if store.version(*key) != 2 or not torch.equal(store.read(*key), wit[1]):
            bad.append((key, "final", "newest accepted read 不是 (v2, zB)"))

    print(f"  ---- trace：{n_step} 次操作，違規 {len(bad)} 筆")
    for b in bad[:10]:
        print(f"      ✗ {b}")

    # ---- healthy parity：newest accepted read vs direct-current-z path -------
    # **這是 parity，不要求 core 的文字答案 100%**（既有 oracle ceiling 本就不是）。
    print("\n  ---- healthy parity（z／delivery tensor／decode 三件套逐位元）")
    par_ok = par_n = 0
    for key in allk:
        dis = next(k for k in allk if k[0] != key[0])
        z_read = store.read(*key)
        z_direct = S.fact_latent2(key[0], key[1], WV[1])     # current z 的直接構造
        tv, dv = WV[1], WV[0]
        facts = [(key[0], key[1], tv), (dis[0], dis[1], dv)]
        ep = B.Episode(facts, f"{key[0]} {B.ATTRS[key[1]]} 是 多少",
                       B.val_str(tv), [0], 0)
        ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                               add_special_tokens=False).input_ids)
        zd = S.fact_latent2(dis[0], dis[1], dv)
        mc_r = inj(torch.stack([z_read, zd]))
        mc_d = inj(torch.stack([z_direct, zd]))
        out_r = gen(m, tok, ids, loops, mc_r)
        out_d = gen(m, tok, ids, loops, mc_d)
        par_n += 1
        par_ok += int(torch.equal(z_read, z_direct) and torch.equal(mc_r, mc_d)
                      and out_r == out_d)
    print(f"    {par_ok}/{par_n} 逐位元相同")
    if par_ok != par_n:
        bad.append((None, "parity", f"{par_n - par_ok} 筆不符"))

    ok = not bad
    print()
    if ok:
        print("  → **EC-1 PASS**：單程序、全序 commit 下的 exact-key conflict contract 成立。")
        print("    **不得**升格成 crash-safe／concurrent／distributed。")
    else:
        print(f"  → **EC-1 FAIL**（{len(bad)} 筆違規）")
        print("    依 prereg：保留原始違規紀錄，**只可修 contract 實作**並以")
        print("    **完全相同的 trace／witness** 做 regression；不得改 version 語意去救。")
    json.dump({"n_keys": len(allk), "n_steps": n_step, "witnesses": list(WV),
               "trace": [t[0] for t in TRACE], "violations": [str(b) for b in bad],
               "parity": [par_ok, par_n], "pass": ok, "log_tail": log[-9:]},
              open(os.path.join(HERE, "results_ec1.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_ec1.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_latent.pth")
