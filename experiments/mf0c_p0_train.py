"""`P0` frozen canonical predictive-surprisal —— **授權範圍 B（Codex [180]）**。

**只做兩件事**：
1. 補 manifest 的 audit metadata（**不改 corpus**）。
2. 在已鎖的 corpus 上跑**一次** `P0` training，**只取 final update 20,000 的 checkpoint**。

**不得**：在此階段算 `H`、寫 controller training code、碰 `W1` primary 成績、
或看 train loss 之後改 config／重跑／挑 checkpoint。

規格全部取自 `MF0C_prereg.json`，**本檔不決定任何規格數字**。
"""
import hashlib
import json
import os
import subprocess
import sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "MF0C_prereg.json")))
S = P["frozen_predictive_surprisal"]
DEV = "cuda" if torch.cuda.is_available() else "cpu"

N_ENT, N_ATTR, N_CAT, W = 16, 12, 3, 4
IN_DIM = N_ENT + N_ATTR                      # 28：onehot(entity) || onehot(attr)
H_DIM = 16


class P0(nn.Module):
    """單層 `GRUCell(28,16)` ＋ 兩個 softmax head；goal 的 3-way onehot **接到 head**。

    **無其他 embedding／tokenizer**（Codex [178]）。
    """

    def __init__(self):
        super().__init__()
        self.cell = nn.GRUCell(IN_DIM, H_DIM)
        self.h_ent = nn.Linear(H_DIM + N_CAT, N_ENT)
        self.h_att = nn.Linear(H_DIM + N_CAT, N_ATTR)

    def forward(self, ctx, ctx_len, goal):
        """`ctx`: (B, W, 2) 的前文 canonical (entity, attr)，**左對齊、最舊在前**。

        **每次從零狀態重跑**，絕不跨視窗保留 state（Codex [178]）。
        """
        B = ctx.shape[0]
        h = torch.zeros(B, H_DIM, device=ctx.device)
        for j in range(W):
            u = torch.cat([F.one_hot(ctx[:, j, 0].long(), N_ENT),
                           F.one_hot(ctx[:, j, 1].long(), N_ATTR)], -1).float()
            hn = self.cell(u, h)
            live = (ctx_len > j).float().unsqueeze(-1)      # 短於 W 的視窗不更新
            h = live * hn + (1 - live) * h
        g = F.one_hot(goal.long(), N_CAT).float()
        z = torch.cat([h, g], -1)
        return self.h_ent(z), self.h_att(z)


def sha_file(p, full=False):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest() if full else h.hexdigest()[:16]


def augment_manifest():
    """補 audit metadata。**不改 corpus。**"""
    mp = os.path.join(HERE, "mf0c_manifest.json")
    m = json.load(open(mp))
    cp = os.path.join(HERE, "mf0c_p0_corpus.npy")
    ap = os.path.join(HERE, "mf0c_artifact.json")
    m["audit"] = {
        "truncation_rule": "所有 fingerprint 皆為 **SHA-256 的前 16 個 hex 字元**；full SHA 另存於本節",
        "P0_corpus_sha256_full": sha_file(cp, True),
        "MF0C_artifact_sha256_full": sha_file(ap, True),
        "canonical_digest_algorithm": "sha256(str(goal) || concat_over_t bytes((entity_t, attr_t)))[:16]",
        "canonical_digest_schema": "(goal:int, 有序的 64 個 (entity:int, attr:int))",
        "n_unique_P0": 100000,
        "n_unique_MF0C": 4300,
        "code_manifest_sha": sha_file(os.path.join(HERE, "mf0c_data.py"), True),
        "trainer_manifest_sha": sha_file(os.path.abspath(__file__), True),
        "prereg_sha": sha_file(os.path.join(HERE, "MF0C_prereg.json"), True),
    }
    json.dump(m, open(mp, "w"), indent=2, ensure_ascii=False)
    return m


def main():
    m = augment_manifest()
    print("  P0 training（授權範圍 B，Codex [180]）")
    print(f"  corpus fingerprint = {m['P0_corpus']['sha']}"
          f"（必須等於 prereg 的 required commit）")
    assert m["P0_corpus"]["sha"] == S["P0_corpus_fingerprint"], \
        "corpus fingerprint 與 prereg 不符 —— 不得在未承諾的 corpus 上訓練"
    print("  ---- manifest audit metadata 已補（**未改 corpus**）")
    print(f"    truncation rule / canonical digest schema / n_unique / code-config hash ✓\n")

    arr = np.load(os.path.join(HERE, "mf0c_p0_corpus.npy"))
    assert arr.shape == (100000, 65, 2)
    goals = torch.tensor(arr[:, 0, 0].astype(np.int64))
    events = torch.tensor(arr[:, 1:].astype(np.int64))
    n, T = events.shape[0], events.shape[1]

    torch.manual_seed(S["P0_train_seed"])
    m0 = P0().to(DEV)
    opt = torch.optim.AdamW(m0.parameters(), lr=1e-3, betas=(0.9, 0.999),
                            weight_decay=0.01)
    g = torch.Generator(device="cpu").manual_seed(S["P0_train_seed"])
    B, UPD = S["P0_batch"], S["P0_updates"]
    print(f"  單層 GRUCell({IN_DIM},{H_DIM}) ＋ 兩個 head；"
          f"batch={B} 隨機 causal window；updates={UPD}")
    print("  **無 validation／early stop／checkpoint selection —— 僅取 final step**\n")

    m0.train()
    last = None
    for step in range(1, UPD + 1):
        si = torch.randint(0, n, (B,), generator=g)
        ti = torch.randint(0, T, (B,), generator=g)
        ctx = torch.zeros(B, W, 2, dtype=torch.long)
        clen = torch.zeros(B, dtype=torch.long)
        for b in range(B):
            lo = max(0, int(ti[b]) - W)
            c = events[si[b], lo:int(ti[b])]
            clen[b] = c.shape[0]
            if c.shape[0]:
                # **左對齊**（最舊的在 j=0），配合 `live = ctx_len > j`：
                # 前 `clen` 步是真事件、其餘不更新。
                # ⚠️ 先前寫成右對齊而 mask 未跟著改，會把 padding 當真事件、
                #    真事件當 padding —— 方向剛好相反。
                ctx[b, :c.shape[0]] = c
        tgt = events[si, ti]
        le, la = m0(ctx.to(DEV), clen.to(DEV), goals[si].to(DEV))
        # 訓練目標與 scoring 的 `s_t` 同形：0.5 * (CE_entity + CE_attr)
        loss = 0.5 * (F.cross_entropy(le, tgt[:, 0].to(DEV))
                      + F.cross_entropy(la, tgt[:, 1].to(DEV)))
        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()
        last = float(loss)
        if step % 5000 == 0:
            print(f"    step {step}/{UPD}  loss={last:.4f}")

    ckpt = os.path.join(HERE, "mf0c_p0.pth")
    torch.save({"model": m0.state_dict(), "updates": UPD,
                "corpus_sha": m["P0_corpus"]["sha"],
                "final_train_loss": last}, ckpt)
    fp = sha_file(ckpt)
    print(f"\n  final update {UPD} 的 checkpoint（**唯一** checkpoint）")
    print(f"    final train loss = {last:.4f}")
    print(f"    **P0_final_checkpoint_fingerprint_recorded_after_run = {fp}**")

    m["P0_final_checkpoint"] = {"path": "mf0c_p0.pth", "sha": fp,
                                "updates": UPD, "final_train_loss": last,
                                "note": "**如實記錄於 final update 之後**；"
                                        "不得作為訓練前 requirement 或 checkpoint-selection 條件"}
    json.dump(m, open(os.path.join(HERE, "mf0c_manifest.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> mf0c_p0.pth / mf0c_manifest.json")
    print("\n  ⚠️ 下一步是**不訓練**的 H audit；本階段**不算 H、不碰 W1 primary**。")


if __name__ == "__main__":
    main()
