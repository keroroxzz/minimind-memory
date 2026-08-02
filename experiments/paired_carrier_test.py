"""三個 carrier 的配對比較（McNemar + 配對 bootstrap）。

n2 的三個條件用完全相同的底層樣本（latent checksum 相同），所以
逐題是配對的 —— 用未配對的 SE 會低估精確度並算錯顯著性（Codex 指出）。

用法：python paired_carrier_test.py <ckptA> <ckptB> --k 1 [--n 300]
"""
import sys, argparse, random, torch
sys.path.insert(0, __file__.rsplit("/", 1)[0])
sys.path.insert(0, __file__.rsplit("/", 2)[0])
import synth_depth_task as S
from transformers import AutoTokenizer
from model.model_minimind import MiniMindForCausalLM, MiniMindConfig

ap = argparse.ArgumentParser()
ap.add_argument("ckpt_a"); ap.add_argument("ckpt_b")
ap.add_argument("--carrier-a", default="pointer"); ap.add_argument("--carrier-b", default="blank")
ap.add_argument("--k", type=int, default=1); ap.add_argument("--n", type=int, default=300)
a = ap.parse_args()

tok = AutoTokenizer.from_pretrained(__file__.rsplit("/", 2)[0] + "/model")

def load(path):
    m = MiniMindForCausalLM(MiniMindConfig(**S.BACKBONE, **dict(S.CONFIGS["loop2"])))
    m.load_state_dict(torch.load(path, map_location="cpu"), strict=False)
    return m.to(S.DEVICE).eval()

MA, MB = load(a.ckpt_a), load(a.ckpt_b)

def run(m, prompt):
    ids = tok(tok.bos_token + prompt, add_special_tokens=False,
              return_tensors="pt").input_ids.to(S.DEVICE)
    o = m.generate(ids, max_new_tokens=6, do_sample=False, eos_token_id=tok.eos_token_id)
    return tok.decode(o[0][ids.shape[1]:], skip_special_tokens=True).strip()

def official_val(carrier, want_k, n_per_k=300):
    """重建**官方 val 列**（`build(..., unique=True)` 用 Random(SEED+999)）。

    先前這裡另抽 `Random(50000+i)` 的新題，那是同分布但不同批 ——
    所以印出的 26.5/24.2% 與各自 JSON 的 k1 24.5/24.0% 是兩套數字（Codex 指出）。
    改成重建官方 val，數字才能與結果檔對得起來，也保證不是訓練集裡的題。
    """
    rng = random.Random(S.SEED + 999); out = []
    for k in range(1, 25):
        seen, made, att = set(), 0, 0
        while made < n_per_k and att < n_per_k * 50:
            att += 1
            pr, ans, lat = S.make_n2(k, rng, carrier)
            if lat in seen:
                continue
            seen.add(lat)
            if k == want_k:
                out.append((pr, ans, lat))
            made += 1
        if k > want_k:
            break
    return out

rows_a = official_val(a.carrier_a, a.k)[:a.n]
rows_b = official_val(a.carrier_b, a.k)[:a.n]
assert len(rows_a) == len(rows_b)
pairs = []
with torch.no_grad():
    for (pa, ans, la), (pb, _, lb) in zip(rows_a, rows_b):
        assert la == lb, "兩個 carrier 的 latent 不一致，配對前提不成立"
        pairs.append((run(MA, pa) == ans, run(MB, pb) == ans))

n = len(pairs)
ha = sum(x for x, _ in pairs); hb = sum(y for _, y in pairs)
b01 = sum(1 for x, y in pairs if x and not y)      # A 對 B 錯
b10 = sum(1 for x, y in pairs if y and not x)      # B 對 A 錯
import hashlib
lat_ck = hashlib.sha256("\n".join(l for _, _, l in rows_a).encode()).hexdigest()[:16]
ck = lambda f: hashlib.sha256(open(f, "rb").read()).hexdigest()[:16]
print(f"配對樣本 n={n}  官方 val 列，k={a.k}")
print(f"  eval latent checksum {lat_ck}")
print(f"  ckpt A {a.ckpt_a.split('/')[-1]} {ck(a.ckpt_a)}")
print(f"  ckpt B {a.ckpt_b.split('/')[-1]} {ck(a.ckpt_b)}")
print(f"  {a.carrier_a:8s} {ha/n:6.1%}    {a.carrier_b:8s} {hb/n:6.1%}    "
      f"差 {(ha - hb) / n * 100:+.1f}pp")
print(f"  不一致格 b01={b01}（僅 A 對）  b10={b10}（僅 B 對）")

# McNemar 精確檢定（雙尾）：不一致格在 H0 下服從 Binomial(b01+b10, .5)
from math import comb
tot = b01 + b10
if tot:
    lo = min(b01, b10)
    p = 2 * sum(comb(tot, i) for i in range(lo + 1)) / 2 ** tot
    print(f"  McNemar 精確檢定  p = {min(p,1.0):.4f}")
else:
    print("  兩者完全一致，無法檢定")

rng = random.Random(0)
diffs = sorted((sum(pairs[j][0] for j in idx) - sum(pairs[j][1] for j in idx)) / n
               for idx in ([rng.randrange(n) for _ in range(n)] for _ in range(5000)))
print(f"  配對 bootstrap 95% CI  [{diffs[125]*100:+.1f}pp, {diffs[4875]*100:+.1f}pp]")
