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

pairs = []
with torch.no_grad():
    for i in range(a.n):
        r = random.Random(50_000 + i)
        pa, ans, lat_a = S.make_n2(a.k, random.Random(50_000 + i), a.carrier_a)
        pb, _,  lat_b = S.make_n2(a.k, random.Random(50_000 + i), a.carrier_b)
        assert lat_a == lat_b, "兩個 carrier 的 latent 不一致，配對前提不成立"
        pairs.append((run(MA, pa) == ans, run(MB, pb) == ans))

n = len(pairs)
ha = sum(x for x, _ in pairs); hb = sum(y for _, y in pairs)
b01 = sum(1 for x, y in pairs if x and not y)      # A 對 B 錯
b10 = sum(1 for x, y in pairs if y and not x)      # B 對 A 錯
print(f"配對樣本 n={n}  （latent 已驗證一致）")
print(f"  {a.carrier_a:8s} {ha/n:6.1%}    {a.carrier_b:8s} {hb/n:6.1%}    差 {(ha-hb)/n:+.1f}pp")
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
print(f"  配對 bootstrap 95% CI  [{diffs[125]:+.3f}, {diffs[4875]:+.3f}]")
