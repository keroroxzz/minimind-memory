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


def parse_groups(prompt):
    """從 prompt 切出所有「5 個以空格分隔的數字」群組。

    ⚠️ 不要用 lookbehind regex —— 我第一版寫 `(?<![\\d ])...`，
    那會**排除前面是空格的群組**，而新格式的 value 前面正是 `| `，
    結果 200 題全部誤判。切分比 regex 穩。
    """
    out = []
    for seg in prompt.split("|"):
        seg = seg.strip().removeprefix("x=").removesuffix("求x=").strip()
        parts = seg.split()
        if len(parts) == PERM_N and all(p.isdigit() for p in parts):
            out.append(" ".join(parts))
    return out


PLACEHOLDER = ". . . . ."          # 與一個 value group 同樣是 5 個 token


def render_L0(s: CanonicalSample):
    """**oracle 已把 chain 解成 selected explicit values**，逐步排在使用處。

    ⚠️ **不可給 definition block、不可留 f-key**（Codex 指出的致命偏差）。
    先前版本是 `f3=… f0=… | x=S f3 求x=` —— 那是 **core-select pointer 任務**，
    core 仍要把 f-key 比對到定義區，正是已知的 binding 瓶頸。
    拿它當 positive control 會把 pointer carrier 與 matching 混進 G1，
    違反「oracle-selection 固定前端」的設計。
    """
    steps = " ".join(f"| {_nums(s.defs[g])}" for g in s.chain)
    return f"| x={_nums(s.state)} {steps} 求x=", _nums(s.answer)


def render_latent(s: CanonicalSample):
    """L1/L2：每個 value group 換成**中性 placeholder**，資訊改由 delivery 提供。

    長度、邊界、步數位置與 L0 **完全一致** —— 只有「該位置有沒有值」在變。
    """
    steps = " ".join(f"| {PLACEHOLDER}" for _ in s.chain)
    return f"| x={_nums(s.state)} {steps} 求x=", _nums(s.answer)


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
    # ⚠️ 必須從 **prompt 裡實際出現的 value groups** 獨立 parse 並 replay，
    #    不可用 s.defs/s.chain 重算 —— 那只是在驗證我自己的內部一致性（Codex）。
    print("\n關卡 2：L0 的 input 與 label 對齊（從 prompt 獨立 replay）")
    import re as _re
    bad = []
    for s in ds:
        p, a = render_L0(s)
        groups = parse_groups(p)
        if len(groups) != s.k + 1:                       # state + k 個 value
            bad.append((s.sample_id, "群組數不符")); continue
        st = [int(x) for x in groups[0].split()]
        for g in groups[1:]:
            q = [int(x) for x in g.split()]
            st = [st[q[i]] for i in range(PERM_N)]
        if _nums(st) != a:
            bad.append((s.sample_id, "replay 不等於 label"))
    print(f"  {'✅' if not bad else '❌'} {len(ds)} 題可由 prompt 內的 value groups "
          f"獨立 replay 出 label{'' if not bad else f'  例：{bad[0]}'}")
    ok &= not bad
    has_key = [s.sample_id for s in ds if _re.search(r"f\d", render_L0(s)[0])]
    print(f"  {'✅' if not has_key else '❌'} L0 prompt **不含任何 f-key**"
          f"（否則就退化成 pointer 任務）")
    ok &= not has_key

    # latent render 不得洩漏 value。
    # ⚠️ 不可用「defs 的字串是否出現在 prompt 裡」判斷 —— state 本身就是一個置換，
    #    某個 def 恰好等於 state 的機率約 1.7%/def，實測 200 題誤判 6 題。
    #    正確做法是**數 5 個數字的群組個數**：latent prompt 只該有 state 一組。
    leak = []
    for s in ds:
        pr = render_latent(s)[0]
        g = parse_groups(pr)
        if len(g) != 1 or g[0] != _nums(s.state):
            leak.append((s.sample_id, g))
    print(f"  {'✅' if not leak else '❌'} latent prompt 只含 state 一組數字，無任何 value"
          f"{'' if not leak else f'  例：{leak[0]}'}")
    ok &= not leak
    shape_ok = all(len(parse_groups(render_L0(s)[0])) == s.k + 1 for s in ds)
    print(f"  {'✅' if shape_ok else '❌'} 對照：L0 的 prompt 有 k+1 組數字（state + k 個 value）")
    ok &= shape_ok
    same_len = all(len(render_L0(s)[0].split()) == len(render_latent(s)[0].split()) for s in ds)
    print(f"  {'✅' if same_len else '❌'} L0 與 latent 的 token 數完全相同"
          f"（只有『該位置有沒有值』在變）")
    ok &= same_len

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

    # 關卡 4：train/val 的 canonical ID 必須不相交
    print("\n關卡 4：train / val 的 canonical ID 不相交")
    tr = build_dataset(max_k=4, n_per_k=50, seed=42)
    va = build_dataset(max_k=4, n_per_k=20, seed=42 + 999)
    inter = {s.sample_id for s in tr} & {s.sample_id for s in va}
    print(f"  {'✅' if not inter else '❌'} 交集 {len(inter)} 個"
          f"（train {len(tr)} / val {len(va)}）")
    print(f"     train checksum {pairing_checksum(tr)}   val checksum {pairing_checksum(va)}")
    ok &= not inter

    ex = ds[2]
    print(f"\n範例（k={ex.k}, id={ex.sample_id}）")
    print(f"  L0      {render_L0(ex)[0]}")
    print(f"  latent  {render_latent(ex)[0]}")
    print(f"  answer  {render_L0(ex)[1]}")
    print("\n" + "=" * 62)
    print(f"  {'全部通過' if ok else '有失敗'}")
    sys.exit(0 if ok else 1)
