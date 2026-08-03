"""G1 的 canonical paired renderer（`design_c_layer.md` §5）。

**同一批 canonical 樣本**產生所有條件，逐題配對、hash 驗證：

  L0  values 直接寫在 prompt 裡（positive control，**不是** latent delivery）
  L1  LatentSlots：prompt 不含 values，改由 store 交付
  L2  SyntheticKV：同上，交付形式不同

⚠️ **不可借舊 `inline` 的 99.8%** —— `absent p=0` 與舊 `inline` 的 prompt/latent
分布不同。baseline 必須由同一批樣本 render 出來。
"""
import hashlib
import json
import os
import random
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from dataclasses import dataclass, field

import torch

from model.memory_module import LATENT_DIM, PERM_N, LatentStore, MemoryEntry, perm_to_latent

KEYS = [f"f{i}" for i in range(4)]


@dataclass
class CanonicalSample:
    """一個樣本的**表示無關**核心。三種 render 共用同一個實例。"""
    k: int
    defs: dict                    # key -> permutation（每樣本重抽：背進權重無用且有害）
    present: list                 # 記憶區內的 key（有序）
    state: list
    chain: list                   # 有序、可重複 —— 表達 composition chain
    answer: list
    sample_id: str = field(default="")

    def __post_init__(self):
        if not self.sample_id:
            core = json.dumps({"k": self.k, "defs": {k: self.defs[k] for k in sorted(self.defs)},
                               "present": self.present, "state": self.state,
                               "chain": self.chain, "answer": self.answer},
                              sort_keys=True)
            self.sample_id = hashlib.sha256(core.encode()).hexdigest()[:16]


def make_canonical(k, rng, n_present=2):
    """產生一個 canonical 樣本。與 `synth_depth_task.make_absent(p_missing=0)` 同構。"""
    present = sorted(rng.sample(range(len(KEYS)), n_present))
    defs, seen = {}, set()
    for i in present:
        while True:
            q = list(range(PERM_N)); rng.shuffle(q); t = tuple(q)
            if q != list(range(PERM_N)) and t not in seen:
                seen.add(t); defs[KEYS[i]] = q; break
    order = [KEYS[i] for i in present]; rng.shuffle(order)
    state = list(range(PERM_N)); rng.shuffle(state)
    chain = [KEYS[rng.choice(present)] for _ in range(k)]
    st = list(state)
    for g in chain:
        st = [st[defs[g][i]] for i in range(PERM_N)]
    return CanonicalSample(k=k, defs=defs, present=order, state=state, chain=chain, answer=st)


# ----------------------------------------------------------------------- render

def _nums(xs):
    return " ".join(map(str, xs))


def render_L0(s: CanonicalSample):
    """explicit values 寫在記憶區裡 —— positive control。"""
    block = " ".join(f"{k}=" + _nums(s.defs[k]) for k in s.present)
    prompt = f"{block} | x={_nums(s.state)} " + " ".join(s.chain) + " 求x="
    return prompt, _nums(s.answer)


def render_latent(s: CanonicalSample):
    """L1/L2：**prompt 不含任何 value**，定義改由 store 交付。

    其餘（state、chain、答案）與 L0 **完全相同** —— 這是配對的前提。
    """
    prompt = f"| x={_nums(s.state)} " + " ".join(s.chain) + " 求x="
    return prompt, _nums(s.answer)


def build_store(s: CanonicalSample) -> LatentStore:
    """每樣本重建 store —— 定義每樣本重抽，背進權重無用且有害。

    store **凍結**（無梯度、無 learned write policy），但內容是 per-episode 的。
    """
    st = LatentStore()
    st.commit([MemoryEntry(k, perm_to_latent(s.defs[k]), {"src": "oracle"})
               for k in s.present])
    return st


def resolve_chain(s: CanonicalSample):
    """oracle 把 chain 解成**有序、可重複**的 latent list。"""
    return torch.stack([perm_to_latent(s.defs[g]) for g in s.chain])


# ----------------------------------------------------------------------- 資料集

def build_dataset(max_k, n_per_k, seed):
    rng = random.Random(seed)
    out = []
    for k in range(1, max_k + 1):
        seen = set()
        made = attempts = 0
        while made < n_per_k and attempts < n_per_k * 50:
            attempts += 1
            s = make_canonical(k, rng)
            if s.sample_id in seen:          # 用 sample_id 去重，不用 render 出來的字串 ——
                continue                     # 否則 render 形式會影響抽樣（先前踩過）
            seen.add(s.sample_id); out.append(s); made += 1
    return out


def pairing_checksum(samples):
    return hashlib.sha256("\n".join(s.sample_id for s in samples).encode()).hexdigest()[:16]


if __name__ == "__main__":
    print("=" * 62)
    print("  G1 canonical paired renderer —— 三關檢查")
    print("=" * 62)
    ok = True

    ds = build_dataset(max_k=4, n_per_k=50, seed=42)
    ds2 = build_dataset(max_k=4, n_per_k=50, seed=42)
    print(f"\n樣本 {len(ds)} 個，pairing checksum {pairing_checksum(ds)}")

    # 關卡 1：canonical hash pairing
    print("\n關卡 1：三種 render 必須來自同一批樣本")
    same = pairing_checksum(ds) == pairing_checksum(ds2)
    print(f"  {'✅' if same else '❌'} 決定性：同 seed 兩次產生的 checksum 相同")
    ok &= same
    per_render = {}
    for name, fn in [("L0", render_L0), ("latent", render_latent)]:
        per_render[name] = hashlib.sha256(
            "\n".join(f"{s.sample_id}:{fn(s)[1]}" for s in ds).encode()).hexdigest()[:16]
    aligned = per_render["L0"] == per_render["latent"]
    print(f"  {'✅' if aligned else '❌'} L0 與 latent 的 (sample_id, answer) 序列一致："
          f"{per_render['L0']}")
    ok &= aligned

    # 關卡 2：L0 input/label alignment
    print("\n關卡 2：L0 的 input 與 label 對齊")
    bad = []
    for s in ds:
        p, a = render_L0(s)
        st = list(s.state)
        for g in s.chain:
            st = [st[s.defs[g][i]] for i in range(PERM_N)]
        if _nums(st) != a:
            bad.append(s.sample_id)
        for g in set(s.chain):                     # chain 用到的 key 必須在 prompt 裡
            if f"{g}=" not in p:
                bad.append(s.sample_id)
    print(f"  {'✅' if not bad else '❌'} {len(ds)} 題的答案皆可由 prompt 內容重算得出")
    ok &= not bad

    # latent render 不得洩漏 value。
    # ⚠️ 不可用「defs 的字串是否出現在 prompt 裡」判斷 —— state 本身就是一個置換，
    #    某個 def 恰好等於 state 的機率約 1.7%/def，實測 200 題誤判 6 題。
    #    正確做法是**數 5 個數字的群組個數**：latent prompt 只該有 state 一組。
    import re
    grp = re.compile(r"(?<![\d ])(?:\d ){4}\d(?![\d])")
    leak = []
    for s in ds:
        pr = render_latent(s)[0]
        g = grp.findall(pr)
        if len(g) != 1 or g[0] != _nums(s.state):
            leak.append((s.sample_id, g))
    print(f"  {'✅' if not leak else '❌'} latent prompt 只含 state 一組數字，無任何 value"
          f"{'' if not leak else f'  例：{leak[0]}'}")
    ok &= not leak
    n0 = len(grp.findall(render_L0(ds[0])[0]))
    print(f"  {'✅' if n0 == 3 else '❌'} 對照：L0 的 prompt 有 3 組數字"
          f"（2 個 def + state），實測 {n0}")
    ok &= (n0 == 3)

    # 關卡 3：store / oracle 一致性
    print("\n關卡 3：store 交付的內容與 L0 寫在 prompt 裡的相同")
    from model.memory_module import latent_to_perm
    mism = []
    for s in ds[:200]:
        store = build_store(s)
        for e, g in zip(store.read(s.chain), s.chain):
            if e is None or latent_to_perm(e.latent) != s.defs[g]:
                mism.append(s.sample_id)
        lat = resolve_chain(s)
        if lat.shape != (s.k, LATENT_DIM):
            mism.append(s.sample_id)
    print(f"  {'✅' if not mism else '❌'} oracle 解出的 latent 與 defs 逐項相同，"
          f"且形狀為 (k, {LATENT_DIM})")
    ok &= not mism

    ex = ds[2]
    print(f"\n範例（k={ex.k}, id={ex.sample_id}）")
    print(f"  L0      {render_L0(ex)[0]}")
    print(f"  latent  {render_latent(ex)[0]}")
    print(f"  answer  {render_L0(ex)[1]}")
    print("\n" + "=" * 62)
    print(f"  {'全部通過' if ok else '有失敗'}")
    sys.exit(0 if ok else 1)
