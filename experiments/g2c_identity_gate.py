"""G2c 資料閘（Codex 要求，未過不得訓 G2c）。

在把 tied address encoder 蓋上去之前，先證明 key identity 在**輸入層面**是可分的，
並把「28 對 write hidden 完全重疊」精確歸因 —— 是 tokenization／取位，還是 core 表示。

四關，任何一關不過就停：
  關 1  canonical key token 序列兩兩相異（純資料，與模型無關）
  關 2  逐對 dump：完整 token ids、所取 index、raw hidden max|diff|（不只 rounded cos）
  關 3  token embedding 的碰撞對是否同 token/subtoken（解釋 cos=1）
  關 4  聚合方式對可分離度的影響（mean 丟順序 → anagram 假碰撞）
"""
import os
import sys
from itertools import combinations

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_train import ARCH, BACKBONE, DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = "g1_G1a_zdelta_d9a603e4e2.pth"


# canonical span 已升格為 renderer invariant（Codex [92] ACK）——
# 這裡只是轉出來給閘門用，定義單一來源在 g1_renderer。
canonical_key_ids = R.canonical_key_ids
locate_span = R.locate_key_span


def key_span_in_write_view(tok, key):
    return locate_span(tok, R.render_write_view(key), key)


@torch.no_grad()
def write_hidden(m, ids):
    return m.model(ids.unsqueeze(0).to(DEVICE), num_loops=ARCH["num_loops"])[0][0]


def pairwise_abs_cos(V):
    V = torch.nn.functional.normalize(V, dim=-1)
    return (V @ V.T - torch.eye(len(V), device=V.device)).abs()


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    m.load_state_dict(torch.load(os.path.join(HERE, CORE), map_location="cpu")["model"],
                      strict=False)
    keys = R.ALL_KEYS
    print(f"  core = {CORE}（凍結）   keys = {len(keys)} 個\n")

    # ---- 關 1：canonical 序列兩兩相異 ------------------------------------
    cids = {k: canonical_key_ids(tok, k) for k in keys}
    dup = [(a, b) for a, b in combinations(keys, 2) if cids[a] == cids[b]]
    print(f"  關 1  canonical token 序列兩兩相異：{'PASS' if not dup else 'FAIL ' + str(dup)}")
    lens = sorted({len(v) for v in cids.values()})
    print(f"        序列長度分布 {lens}（多 token identity 存在 → 不可只取最後一個 token）")
    if dup:
        sys.exit("關 1 不過：不同 key 切成同一串 token，identity 在資料層就不可分")

    # 關 1b：同一 canonical span 必須在 write view 與 retrieval view 兩側都原樣出現
    n_chk = 0
    for k in keys:
        locate_span(tok, R.render_write_view(k), k)
    for s in R.build_dataset(4, 40, 7):
        view = R.render_retrieval_view(s)
        for k in s.chain:
            locate_span(tok, view, k)
            n_chk += 1
    print(f"        關 1b  write／retrieval 兩側 canonical span 一致：PASS"
          f"（write {len(keys)} 個 + retrieval {n_chk} 次定位）")

    # ---- 關 2：write hidden 逐對 dump ------------------------------------
    spans, H_last, H_span = {}, [], []
    for k in keys:
        ids, span = key_span_in_write_view(tok, k)
        spans[k] = (ids, span)
        h = write_hidden(m, ids)
        H_last.append(h[span[-1]])
        H_span.append(h[span].mean(0))
    H_last, H_span = torch.stack(H_last), torch.stack(H_span)

    M = pairwise_abs_cos(H_last)
    coll = [(keys[a], keys[b]) for a, b in (M > 0.9999).nonzero().tolist() if a < b]
    print(f"\n  關 2  write hidden（取 key 最後一 token）|cos|>0.9999 的配對：{len(coll)} 對")
    print(f"        {'pair':<12s} {'tokens A':<18s} {'tokens B':<18s} {'idx':>7s} "
          f"{'raw max|diff|':>14s} {'‖hA‖':>8s}")
    for a, b in coll[:8]:
        ha = write_hidden(m, spans[a][0])[spans[a][1][-1]]
        hb = write_hidden(m, spans[b][0])[spans[b][1][-1]]
        print(f"        {a+'↔'+b:<12s} {str(cids[a]):<18s} {str(cids[b]):<18s} "
              f"{str(spans[a][1][-1])+'/'+str(spans[b][1][-1]):>7s} "
              f"{(ha-hb).abs().max().item():>14.4e} {ha.norm().item():>8.3f}")
    if len(coll) > 8:
        print(f"        …（另 {len(coll)-8} 對）")
    print(f"        判讀：raw max|diff| 若遠大於 float32 eps，就**不是精確重疊**，"
          f"\n              而是共用成分過大 —— 歸因為「core 未見 f-key」需要更強證據")

    # ---- 關 3：token embedding 的碰撞是否為 anagram/同 subtoken ----------
    emb = m.model.embed_tokens
    E_mean = torch.stack([emb(torch.tensor(cids[k], device=DEVICE)).mean(0) for k in keys])
    Me = pairwise_abs_cos(E_mean)
    ec = [(keys[a], keys[b]) for a, b in (Me > 0.99).nonzero().tolist() if a < b]
    print(f"\n  關 3  token embedding(mean) |cos|>0.99：{len(ec)} 對")
    for a, b in ec:
        same = sorted(cids[a]) == sorted(cids[b])
        print(f"        {a}↔{b}  {cids[a]} vs {cids[b]}  "
              f"→ {'**anagram**，mean-pool 丟掉順序造成的假碰撞' if same else '非 anagram，需另查'}")

    # ---- 關 4：聚合方式 --------------------------------------------------
    def posw(k):
        e = emb(torch.tensor(cids[k], device=DEVICE))
        w = torch.arange(1, len(e) + 1, device=DEVICE, dtype=e.dtype).unsqueeze(-1)
        return (e * w).sum(0) / w.sum()
    rows = [("emb mean", E_mean),
            ("emb 位置加權", torch.stack([posw(k) for k in keys])),
            ("hidden 完整 span mean", H_span),
            ("hidden 最後 token", H_last)]
    print(f"\n  關 4  {'聚合方式':<22s} {'中位數':>8s} {'最大':>8s} {'>0.99 對數':>11s}")
    for name, V in rows:
        X = pairwise_abs_cos(V)
        print(f"        {name:<22s} {X.median():>8.3f} {X.max():>8.3f} "
              f"{(X > 0.99).sum().item()//2:>11d}")
    # ---- artifact：身分此後凍結（Codex [92]）-----------------------------
    # 存完整 pairwise 分布與 canonical 切法。**不得**用 margin 門檻篩掉 key
    # 或重抽資料 —— 那會把難例從 test 設計裡洗掉。0.964 是接受的既成事實。
    def posw_all(ks):
        return torch.stack([posw(k) for k in ks])
    art = {
        "core": CORE,
        "keys": keys,
        "canonical_ids": cids,
        "splits": {"train": R.KEYS_TRAIN, "cal": R.KEYS_CAL, "test": R.KEYS_TEST},
        "pairwise_abs_cos_emb_posw": pairwise_abs_cos(posw_all(keys)).cpu(),
        "pairwise_abs_cos_hidden_last": M.cpu(),
        "cross_test_train_max": pairwise_abs_cos(
            torch.cat([posw_all(R.KEYS_TEST), posw_all(R.KEYS_TRAIN)]))[
                :len(R.KEYS_TEST), len(R.KEYS_TEST):].max().item(),
        "note": "身分凍結；訓練後只在 untouched test 報 impostor margin，不重抽 key",
    }
    out = os.path.join(HERE, "g2c_identity_artifact.pt")
    torch.save(art, out)
    print(f"\n  artifact → {os.path.basename(out)}"
          f"（跨組 max {art['cross_test_train_max']:.3f}，身分自此凍結）")


if __name__ == "__main__":
    main()
