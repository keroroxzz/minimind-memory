"""`RWA-0` 三-seed final-only train ＋ frozen eval（Codex [192] 授權）。

**只讀** prereg v3 ＋ `artifact_v2`。**禁止**短跑 smoke／validation／
epoch 或中途 checkpoint selection／依 train loss 或 eval 改任何值。
final train loss 只作 provenance，**不得據它採取行動**。
"""
import hashlib, json, os, random
import torch, torch.nn as nn, torch.nn.functional as F
import rwa0_data as B

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "RWA0_prereg.json")))
EC = P["encoder_compute_locked"]
DEV = "cuda" if torch.cuda.is_available() else "cpu"
MAXLEN, NCLS = 10, 132
TOK = {t: i for i, t in enumerate(B.VOCAB)}
SLOTS = ["target", "same_entity", "same_attr", "swap"]


class Enc(nn.Module):
    def __init__(self):
        super().__init__()
        d = EC["width"]
        self.emb = nn.Embedding(len(B.VOCAB), d, padding_idx=TOK["PAD"])
        self.pos = nn.Embedding(MAXLEN, d)
        lay = nn.TransformerEncoderLayer(d, EC["heads"], EC["ffn"], 0.0,
                                         batch_first=True)
        self.enc = nn.TransformerEncoder(lay, EC["layers"])
        self.he = nn.Linear(d, NCLS)
        self.ha = nn.Linear(d, NCLS)

    def forward(self, x, m):
        h = self.emb(x) + self.pos(torch.arange(x.shape[1], device=x.device))
        h = self.enc(h, src_key_padding_mask=~m)
        h = (h * m.unsqueeze(-1)).sum(1) / m.sum(1, keepdim=True)   # 排除 PAD
        return self.he(h), self.ha(h)


def encode(surfs):
    x = torch.full((len(surfs), MAXLEN), TOK["PAD"], dtype=torch.long)
    m = torch.zeros(len(surfs), MAXLEN, dtype=torch.bool)
    for i, s in enumerate(surfs):
        assert len(s) <= MAXLEN
        for j, t in enumerate(s):
            x[i, j] = TOK[t]; m[i, j] = True
    return x.to(DEV), m.to(DEV)


@torch.no_grad()
def decode(model, surfs):
    """argmax exact-decode；tie **固定取最低 code**（argmax 已是最低 index）。"""
    x, m = encode(surfs)
    le, la = model(x, m)
    return [(int(e), int(a)) for e, a in zip(le.argmax(-1), la.argmax(-1))]


class Store:
    def __init__(self): self.d = {}
    def commit(self, k, v):
        if k in self.d: return "reject_conflict" if self.d[k] != v else "duplicate"
        self.d[k] = v; return "new"
    def read(self, k): return self.d.get(k)


def startup_asserts(art):
    assert B.sha(P) == json.load(open(os.path.join(HERE, "rwa0_manifest.json")))["prereg_sha"]
    fp = json.load(open(os.path.join(HERE, "rwa0_manifest_v2.json")))["artifact_v2_fingerprint"]
    assert B.sha(art) == fp == "8ca1d40413aae706", "artifact fingerprint 不符"
    ts = [s for r in art["train"] for s in (r["w"], r["r"])]
    assert len(ts) == 48000, len(ts)
    es = [s for r in art["eval"] for sl in r["surfaces"] for s in (sl["w"], sl["r"])]
    assert len(es) == 2400, len(es)
    assert len({tuple(sl["key"]) for r in art["eval"] for sl in r["surfaces"]}) == 1200
    for s in ts + es:
        assert len(s) <= MAXLEN and all(t in TOK for t in s)
    for k, p in enumerate(B.PAIRS):                     # code-rank ↔ RWAKey 雙向
        assert B.RANK[p] == k and B.PAIRS[k] == p
    print("  ✓ startup asserts：prereg SHA／fingerprint／48,000／2,400／1,200／"
          "length-pad／rank round-trip")


def train_one(art, seed):
    torch.manual_seed(seed)
    m = Enc().to(DEV)
    opt = torch.optim.AdamW(m.parameters(), lr=3e-4, betas=(0.9, 0.999),
                            weight_decay=0.01, eps=1e-8, amsgrad=False)
    rows = [(s, tuple(r["K"])) for r in art["train"] for s in (r["w"], r["r"])]
    stream = []
    for ep in range(64):                                # 64 個完整 permutation 串接
        idx = list(range(len(rows)))
        random.Random(hashlib.blake2b(f"{seed}-{ep}".encode(),
                                      digest_size=8).digest()).shuffle(idx)
        stream += idx
    m.train(); last = None
    for u in range(EC["updates"]):
        b = stream[u * 256:(u + 1) * 256]               # batch 可跨 epoch 邊界
        surfs = [rows[i][0] for i in b]
        ke = torch.tensor([rows[i][1][0] for i in b], device=DEV)
        ka = torch.tensor([rows[i][1][1] for i in b], device=DEV)
        x, msk = encode(surfs)
        le, la = m(x, msk)
        loss = 0.5 * (F.cross_entropy(le, ke) + F.cross_entropy(la, ka))
        opt.zero_grad(set_to_none=True); loss.backward()
        nn.utils.clip_grad_norm_(m.parameters(), 1.0)
        opt.step(); last = float(loss.detach())
        if (u + 1) % 4000 == 0:
            print(f"      seed {seed} upd {u+1}/{EC['updates']} loss={last:.4f}")
    return m.eval(), last


def evaluate(model, art):
    r = {"a_ok": 0, "b": {s: 0 for s in SLOTS[1:]}, "c_correct": 0,
         "c_wrong_existing": 0, "c_abstain": 0,
         "commit_status": {"new": 0, "duplicate": 0, "reject_conflict": 0},
         "first_fail": None, "n": len(art["eval"])}
    for row, a in enumerate(art["eval"]):
        sl = {s["slot"]: s for s in a["surfaces"]}
        Kt = tuple(sl["target"]["K"])
        dec = {s: {"w": None, "r": None} for s in SLOTS}
        outs = decode(model, [sl[s][c] for s in SLOTS for c in ("w", "r")])
        for i, s in enumerate(SLOTS):
            dec[s]["w"], dec[s]["r"] = outs[2 * i], outs[2 * i + 1]
        ok_a = dec["target"]["w"] == dec["target"]["r"] == Kt
        r["a_ok"] += int(ok_a)
        for s in SLOTS[1:]:                              # (b)：兩個 carrier 都查
            if dec[s]["w"] == Kt or dec[s]["r"] == Kt:
                r["b"][s] += 1
        st = Store(); vals = {}
        for s in SLOTS:                                  # (c)：用 model 解出的 write key
            v = (row, s)
            vals[dec[s]["w"]] = vals.get(dec[s]["w"], v)
            r["commit_status"][st.commit(dec[s]["w"], v)] += 1
        got = st.read(dec["target"]["r"])
        if got == (row, "target"): r["c_correct"] += 1
        elif got is None: r["c_abstain"] += 1
        else: r["c_wrong_existing"] += 1
        if r["first_fail"] is None and not (ok_a and got == (row, "target")):
            r["first_fail"] = {"row": row, "a": ok_a, "read": str(got)}
    return r


def main():
    art = json.load(open(os.path.join(HERE, "rwa0_artifact_v2.json")))
    print("  RWA-0 三-seed final-only train（Codex [192] 授權）")
    startup_asserts(art)
    print(f"  seeds={EC['seeds']}  updates={EC['updates']}  batch 256  **只取 final**\n")
    out, fails = {}, []
    for seed in EC["seeds"]:
        m, loss = train_one(art, seed)
        r = evaluate(m, art)
        n = r["n"]
        pa = r["a_ok"] == n
        pb = all(v == 0 for v in r["b"].values())
        pc = (r["c_correct"] == n and r["c_wrong_existing"] == 0
              and r["c_abstain"] == 0 and r["commit_status"]["new"] == 4 * n)
        if not (pa and pb and pc):
            fails.append(str(seed))
        r["final_train_loss"] = loss
        out[str(seed)] = r
        print(f"    seed {seed}  (a) {r['a_ok']}/{n} {'✓' if pa else '✗'}   "
              f"(b) " + " ".join(f"{k}={v}" for k, v in r["b"].items())
              + f" {'✓' if pb else '✗'}   (c) correct {r['c_correct']}/{n} "
              f"wrong {r['c_wrong_existing']} abstain {r['c_abstain']} "
              f"commit {r['commit_status']} {'✓' if pc else '✗'}")
    print()
    print(f"  → **RWA-0 {'PASS' if not fails else 'FAIL'}**"
          + ("" if not fails else f"（seeds {fails}）"))
    json.dump({"artifact": "8ca1d40413aae706", "seeds": out, "fails": fails,
               "pass": not fails},
              open(os.path.join(HERE, "results_rwa0.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_rwa0.json")


if __name__ == "__main__":
    main()
