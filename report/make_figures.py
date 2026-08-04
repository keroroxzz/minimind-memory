"""Generate every figure in the report **from the recorded result JSONs**.

No number is typed by hand where a JSON exists. Values that predate the JSON
convention (the S5 depth sweep, the looped-transformer table, the G1 delivery
table) are declared once at the top with an explicit provenance comment so the
report and the figures cannot drift apart.

    python report/make_figures.py        # writes report/fig/*.pdf
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "experiments")
FIG = os.path.join(HERE, "fig")
os.makedirs(FIG, exist_ok=True)

plt.rcParams.update({
    "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "legend.fontsize": 8, "xtick.labelsize": 8, "ytick.labelsize": 8,
    "axes.grid": True, "grid.alpha": 0.25, "figure.dpi": 150,
    "axes.spines.top": False, "axes.spines.right": False,
})

C = {"text": "#2b6cb0", "oracle": "#718096", "mem": "#c05621",
     "v1": "#a0aec0", "v2": "#c05621", "v3": "#805ad5",
     "allph": "#2f855a", "mixed": "#c53030", "floor": "#cbd5e0"}


def J(name):
    with open(os.path.join(EXP, name)) as f:
        return json.load(f)


def save(fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(FIG, name + ".pdf"), bbox_inches="tight")
    fig.savefig(os.path.join(FIG, name + ".png"), bbox_inches="tight", dpi=150)
    plt.close(fig)
    print(f"  fig/{name}.pdf")


# --- provenance: pre-JSON results, recorded in research.md / CLAUDE.md -------
S5_K = [1, 2, 3, 4, 5, 6, 8, 12]
S5_EXACT = [.993, .967, .627, .113, .013, .0, .007, .007]
S5_POS = [.997, .989, .828, .440, .280, .225, .195, .189]
LOOP = [("loop1", 8, .103, 2.69), ("loop2", 16, .201, 4.83),
        ("loop3", 24, .732, 17.92), ("loop4", 32, .991, 24.0)]
G1 = [("oracle_inline / teacher_kv", 0.0, 1.00, "in-place"),
      ("InlineLatent", 1.33, 1.00, "in-place"),
      ("contextual", 1.13, 1.00, "in-place"),
      ("zdelta", 1.13, 0.990, "in-place"),
      ("static-full", 6.32, 0.372, "in-place"),
      ("SyntheticKV", 1.06, 0.348, "prefix"),
      ("LatentSlots", 0.14, 0.033, "prefix"),
      ("none", 0.0, 0.012, "floor")]


def fig_depth():
    fig, ax = plt.subplots(1, 2, figsize=(7.4, 2.7))
    ax[0].plot(S5_K, [100*x for x in S5_EXACT], "o-", color=C["text"], label="exact match")
    ax[0].plot(S5_K, [100*x for x in S5_POS], "s--", color=C["oracle"], label="per-position")
    ax[0].axhline(100/120, color="k", ls=":", lw=.8)
    ax[0].axhline(20, color="k", ls=":", lw=.8)
    ax[0].text(11.6, 2.2, "chance 0.8%", fontsize=7, ha="right")
    ax[0].text(11.6, 21.5, "chance 20%", fontsize=7, ha="right")
    ax[0].axvspan(2, 5, color="#fefcbf", alpha=.55, zorder=0)
    ax[0].text(3.5, 88, "sensitive\nwindow", fontsize=7, ha="center", color="#975a16")
    ax[0].set_xlabel("chain length $k$"); ax[0].set_ylabel("accuracy (%)")
    ax[0].set_title("(a) $S_5$ depth ceiling, vanilla 29M / 8 layers")
    ax[0].legend(frameon=False)

    names = [l[0] for l in LOOP]
    ax[1].bar(names, [l[3] for l in LOOP], color=["#cbd5e0", "#a0aec0", "#dd6b20", "#c05621"])
    for i, l in enumerate(LOOP):
        lab = f"{l[3]:.1f}" + (">" if l[0] == "loop4" else "")
        ax[1].text(i, l[3]+.6, lab, ha="center", fontsize=8)
        ax[1].text(i, .6, f"{100*l[2]:.0f}%", ha="center", fontsize=7, color="w")
    ax[1].set_ylabel("ceiling $k^*$"); ax[1].set_ylim(0, 28)
    ax[1].set_title("(b) looped transformer: identical params, more compute")
    ax[1].grid(axis="x", visible=False)
    save(fig, "depth")


def fig_delivery():
    """Horizontal bars: 8 labelled cells overlap badly as a scatter.

    Colour encodes the **mechanism** (residual/embedding vs absolute vs prefix
    token), not the position, because `static-full` is in-place yet fails --
    the separating axis is residual-vs-absolute, not in-place-vs-prefix.
    """
    mech = {"oracle_inline / teacher_kv": "native / embedding",
            "InlineLatent": "native / embedding",
            "contextual": "residual KV",
            "zdelta": "residual KV",
            "static-full": "absolute KV",
            "SyntheticKV": "absolute KV (prefix)",
            "LatentSlots": "carrier token (prefix)",
            "none": "floor"}
    col = {"native / embedding": "#2f855a", "residual KV": "#38a169",
           "absolute KV": "#dd6b20", "absolute KV (prefix)": "#c53030",
           "carrier token (prefix)": "#822727", "floor": "#cbd5e0"}
    rows = sorted(G1, key=lambda r: r[2])
    fig, ax = plt.subplots(figsize=(6.6, 3.1))
    y = range(len(rows))
    ax.barh(list(y), [100*r[2] for r in rows],
            color=[col[mech[r[0]]] for r in rows], height=.62)
    for i, r in enumerate(rows):
        ax.text(100*r[2] + 1.5, i, f"{100*r[2]:.1f}%" +
                (f"   ({r[1]:.2f}M)" if r[1] else "   (0 params)"),
                va="center", fontsize=7.5)
    ax.set_yticks(list(y)); ax.set_yticklabels([r[0] for r in rows], fontsize=8)
    ax.set_xlabel("accuracy (%)"); ax.set_xlim(0, 132)
    ax.set_title("G1: the separating axis is residual-vs-absolute, not capacity")
    ax.grid(axis="y", visible=False)
    seen, h = [], []
    for r in rows:
        m = mech[r[0]]
        if m not in seen and m != "floor":
            seen.append(m)
            h.append(plt.Rectangle((0, 0), 1, 1, color=col[m], label=m))
    ax.legend(handles=h[::-1], frameon=False, loc="lower right", fontsize=7.5)
    save(fig, "delivery")


def fig_boundary():
    p, k = J("results_g3d_pool.json")["cells"], J("results_g3d_k.json")["cells"]
    fig, ax = plt.subplots(1, 2, figsize=(7.4, 2.7))
    pv = sorted(int(x.split("=")[1]) for x in p)
    for lab, key, c, ms in (("L0 (no delivery)", "L0", "k", "^"),
                            ("oracle + zdelta", "oracle", C["oracle"], "s"),
                            ("learned (full chain)", "learned", C["mem"], "o")):
        ys = []
        for v in pv:
            cell = p[f"pool={v}"]
            if key == "L0":
                ys.append(100*cell["L0"][0]/max(cell["L0"][1], 1))
            elif key in cell:
                e = cell[key]["e2e"]; ys.append(100*e[0]/max(e[1], 1))
            else:
                ys.append(float("nan"))
        ax[0].plot(pv, ys, ms+"-", color=c, label=lab)
    ax[0].set_xlabel("store size (pool)"); ax[0].set_ylabel("end-to-end (%)")
    ax[0].set_ylim(0, 105); ax[0].set_xticks(pv)
    ax[0].set_title("(a) store size: flat to the addressing limit")
    ax[0].legend(frameon=False, loc="lower left")

    kv = sorted(int(x.split("=")[1]) for x in k)
    for lab, key, c, ms in (("L0 (no delivery)", "L0", "k", "^"),
                            ("oracle + zdelta", "oracle", C["oracle"], "s"),
                            ("learned (full chain)", "learned", C["mem"], "o")):
        xs, ys = [], []
        for v in kv:
            cell = k[f"k={v}"]
            if key == "L0":
                xs.append(v); ys.append(100*cell["L0"][0]/max(cell["L0"][1], 1))
            elif key in cell:
                e = cell[key]["e2e"]; xs.append(v); ys.append(100*e[0]/max(e[1], 1))
        ax[1].plot(xs, ys, ms+"-", color=c, label=lab)
    ax[1].axhline(100/120, color="k", ls=":", lw=.8)
    ax[1].text(8, 5, "chance 0.8%", fontsize=7, ha="right")
    ax[1].set_xlabel("chain length $k$"); ax[1].set_ylabel("end-to-end (%)")
    ax[1].set_ylim(-4, 105); ax[1].set_xticks(kv)
    ax[1].set_title("(b) depth: $L_0$ at chance $\\Rightarrow$ delivery censored")
    ax[1].legend(frameon=False, loc="lower left")
    save(fig, "boundary")


def fig_retention():
    d = J("results_g5a.json")
    ks = sorted({int(c.split("_")[0][1:]) for c in d["cells"]["memory"]})
    ds = sorted({int(c.split("_")[1][1:]) for c in d["cells"]["memory"]})
    fig, ax = plt.subplots(1, 2, figsize=(7.4, 2.7))
    style = {1: "-", 2: "--", 4: ":"}
    for k in ks:
        for cond, c, m in (("memory", C["mem"], "o"), ("full_context", C["text"], "s")):
            ys = [100*d["cells"][cond][f"k{k}_d{x}"][0] /
                  max(d["cells"][cond][f"k{k}_d{x}"][1], 1) for x in ds]
            ax[0].plot(ds, ys, m+style[k], color=c, ms=4,
                       label=f"{cond} ($k$={k})" if k == 4 else None)
    ax[0].set_xlabel("delay (filler events between write and query)")
    ax[0].set_ylabel("accuracy (%)"); ax[0].set_ylim(-4, 105); ax[0].set_xticks(ds)
    ax[0].set_title("(a) memory decouples accuracy from prompt length")
    ax[0].legend(frameon=False, loc="center left")
    ax[0].text(70, 55, "$k$=1 solid, 2 dashed, 4 dotted", fontsize=7, color="#4a5568")

    w, dmax = .35, ds[-1]
    xs = range(len(ks))
    ax[1].bar([x-w/2 for x in xs], [d["tokens"]["full_context"][f"k{k}_d{dmax}"] for k in ks],
              w, color=C["text"], label="full_context")
    ax[1].bar([x+w/2 for x in xs], [d["tokens"]["memory"][f"k{k}_d{dmax}"] for k in ks],
              w, color=C["mem"], label="memory")
    for i, k in enumerate(ks):
        a = d["tokens"]["full_context"][f"k{k}_d{dmax}"]
        b = d["tokens"]["memory"][f"k{k}_d{dmax}"]
        ax[1].text(i, a+14, f"{a/b:.0f}$\\times$", ha="center", fontsize=8)
    ax[1].set_xticks(list(xs)); ax[1].set_xticklabels([f"$k$={k}" for k in ks])
    ax[1].set_ylabel(f"prompt tokens at $d$={dmax}"); ax[1].set_ylim(0, 520)
    ax[1].set_title("(b) per-query prompt cost")
    ax[1].legend(frameon=False); ax[1].grid(axis="x", visible=False)
    save(fig, "retention")


def fig_composition():
    main, aft = J("results_g5c.json"), J("results_g5c_after.json")
    seg = J("results_g5b.json")
    fig, ax = plt.subplots(1, 3, figsize=(8.6, 2.5))
    dm = main["dmax"]
    # all three configs sit at exactly 100% -- dodge in x so none is hidden.
    for off, (cfg, c) in zip((-.07, 0, .07),
                             (("text", C["text"]), ("allph", C["allph"]),
                              ("mixed", C["mixed"]))):
        ax[0].plot([x+off for x in range(dm+1)],
                   [100*x for x in main["by_config"][cfg]["acc"]],
                   "o-", color=c, ms=4, label=cfg, alpha=.85)
    ax[0].set_xlabel("deliveries $d$ (identity round-trips)")
    ax[0].set_ylabel("accuracy (%)"); ax[0].set_ylim(50, 104)
    ax[0].set_xticks(range(dm+1))
    ax[0].set_title("(a) repeated delivery: no decay")
    ax[0].text(2, 88, "all three coincide\nat 100%", fontsize=7.5, ha="center",
               color="#4a5568")
    ax[0].legend(frameon=False, loc="lower left")

    jm = aft["jmax"]
    for cfg, c in (("text", C["text"]), ("allph", C["allph"]), ("mixed", C["mixed"])):
        ax[1].plot(range(jm+1), [100*x for x in aft["acc"][cfg]], "o-",
                   color=c, ms=4, label=cfg)
    ax[1].set_xlabel("composition steps $j$ after one delivery")
    ax[1].set_ylabel("accuracy (%)"); ax[1].set_ylim(50, 104)
    ax[1].set_title("(b) composition after delivery")

    Ks = sorted(int(x) for x in seg["cells"]["monolithic"])
    for cond, c, m in (("monolithic", "k", "^"), ("pred_text", C["text"], "s"),
                       ("pred_memory", C["mem"], "o")):
        ys = [100*seg["cells"][cond][str(K)][0]/max(seg["cells"][cond][str(K)][1], 1)
              for K in Ks]
        ax[2].plot(Ks, ys, m+"-", color=c, ms=4, label=cond)
    ax[2].set_xlabel("total chain length $K$"); ax[2].set_ylabel("accuracy (%)")
    ax[2].set_ylim(-4, 105); ax[2].set_xticks(Ks)
    ax[2].set_title("(c) segmentation vs. memory carry")
    ax[2].legend(frameon=False, loc="center left")
    save(fig, "composition")


def fig_margin():
    d = J("results_g6c.json")
    js = sorted({int(k.split("_")[0][1:]) for k in d})
    fig, ax = plt.subplots(1, 3, figsize=(7.4, 2.5))
    # m_text is measured on the text-carry arm, so it is identical for allph and
    # mixed by construction -- plot it once rather than drawing one line over another.
    ax[0].plot(js, [d[f"j{j}_allph_v1"]["m_text_med"] for j in js], "o-",
               color=C["text"], ms=4, label="median")
    ax[0].plot(js, [d[f"j{j}_allph_v1"]["m_text_min"] for j in js], "s--",
               color=C["text"], ms=3, alpha=.6, label="min")
    ax[0].axhline(0, color="k", lw=.8)
    ax[0].set_xlabel("$j$"); ax[0].set_ylabel("$m_{\\mathrm{text}}$")
    ax[0].set_ylim(0, 16.5)
    ax[0].set_title("(a) task margin does NOT shrink"); ax[0].set_xticks(js)
    ax[0].legend(frameon=False, fontsize=7, loc="lower left")

    for cfg, c in (("allph", C["allph"]), ("mixed", C["mixed"])):
        ax[1].plot(js, [d[f"j{j}_{cfg}_v1"]["dm_med"] for j in js], "o-",
                   color=c, ms=4, label=f"median ({cfg})")
        ax[1].plot(js, [d[f"j{j}_{cfg}_v1"]["dm_min"] for j in js], "s--",
                   color=c, ms=3, alpha=.6, label=f"min ({cfg})")
    ax[1].axhline(0, color="k", lw=.8)
    ax[1].set_xlabel("$j$"); ax[1].set_ylabel("$\\Delta m$")
    ax[1].set_title("(b) the delivery-induced shift does"); ax[1].set_xticks(js)
    ax[1].legend(frameon=False, fontsize=7, loc="lower left")

    pred = [d[f"j{j}_{c}_{v}"]["pred_flip"] for j in js
            for c in ("allph", "mixed") for v in ("v1", "v2")]
    real = [d[f"j{j}_{c}_{v}"]["real_flip"] for j in js
            for c in ("allph", "mixed") for v in ("v1", "v2")]
    ax[2].scatter(pred, real, s=34, color=C["mem"], edgecolor="k", linewidth=.4, zorder=3)
    lim = max(pred+real)+3
    ax[2].plot([0, lim], [0, lim], "k--", lw=.9)
    ax[2].set_xlim(-1, lim); ax[2].set_ylim(-1, lim)
    ax[2].set_xlabel("episodes with $m_{\\mathrm{text}}+\\Delta m<0$")
    ax[2].set_ylabel("episodes that actually flip")
    ax[2].set_title("(c) 100% coverage, every cell")
    save(fig, "margin")


def fig_v2():
    d = J("results_g6a.json")["acc"]
    held = {(j, c) for j, c in [tuple(x) for x in J("results_g6a.json")["held_out"]]}
    keys = list(d)
    fig, ax = plt.subplots(figsize=(7.0, 2.9))
    x = range(len(keys)); w = .27
    for off, (nm, c) in zip((-w, 0, w), (("v1", C["v1"]), ("v2", C["v2"]),
                                         ("text", C["text"]))):
        ax.bar([i+off for i in x], [100*d[k][nm] for k in keys], w, color=c, label=nm)
    for i, k in enumerate(keys):
        j, cfg = int(k[1]), k.split("_")[1]
        if (j, cfg) in held or d[k].get("held_out"):
            ax.axvspan(i-.45, i+.45, color="#fefcbf", alpha=.45, zorder=0)
    ax.set_xticks(list(x))
    ax.set_xticklabels([k.replace("_", "\n") for k in keys], fontsize=7.5)
    ax.set_ylabel("accuracy (%)"); ax.set_ylim(0, 112)
    ax.set_title("zdelta-v2: config combinations transfer, composition depth does not"
                 "  (shaded = held out)")
    ax.legend(frameon=False, ncol=3, loc="lower left"); ax.grid(axis="x", visible=False)
    ax.annotate("regresses\nbelow v1", xy=(6, 82), xytext=(4.6, 45), fontsize=7.5,
                ha="center", color="#c53030",
                arrowprops=dict(arrowstyle="->", color="#c53030", lw=.9))
    save(fig, "v2")


def fig_safety():
    e, f = J("results_g3e_k1.json"), J("results_g3f.json")
    fig, ax = plt.subplots(1, 2, figsize=(7.0, 2.6))
    nm = e["n_missing"]
    bars = [("learned support\n(G3e)", 100*e["halluc"]/nm, 100*e["halluc_ub95"]),
            ("store contract\n(G3f)", 100*f["halluc"]/nm, 100*f["halluc_ub95"])]
    ax[0].bar([b[0] for b in bars], [b[1] for b in bars],
              color=[C["mixed"], C["allph"]], width=.5)
    for i, b in enumerate(bars):
        ax[0].errorbar(i, b[1], yerr=[[0], [b[2]-b[1]]], color="k", capsize=4, lw=.9)
        ax[0].text(i, b[2]+.35, f"{b[1]:.1f}%\nUB {b[2]:.2f}%", ha="center", fontsize=7.5)
    ax[0].set_ylabel("hallucination on absent keys (%)"); ax[0].set_ylim(0, 6.4)
    ax[0].set_title(f"(a) moving membership to the contract ($n$={nm})")
    ax[0].grid(axis="x", visible=False)

    ch = J("results_g3b.json")["chain"]
    lab = ["writer\nformation", "address\nretrieval", "content\nfidelity",
           "executor |\nboth", "end-to-end"]
    vals = [100*ch[k][0]/max(ch[k][1], 1)
            for k in ("formation", "address", "content", "executor", "e2e")]
    ax[1].plot(range(5), vals, "o-", color=C["mem"], ms=5)
    for i, v in enumerate(vals):
        ax[1].text(i, v-3.2, f"{v:.1f}", ha="center", fontsize=7.5)
    ax[1].axhline(99.0, color=C["oracle"], ls="--", lw=.9)
    ax[1].text(4.35, 99.3, "delivery ceiling", fontsize=7, ha="right", color="#4a5568")
    ax[1].set_xticks(range(5)); ax[1].set_xticklabels(lab, fontsize=7.5)
    ax[1].set_ylabel("accuracy (%)"); ax[1].set_ylim(93, 101.5)
    ax[1].set_title("(b) G3b closure: every link, in causal order")
    ax[1].grid(axis="x", visible=False)
    save(fig, "safety")


if __name__ == "__main__":
    print("writing figures from recorded results:")
    for fn in (fig_depth, fig_delivery, fig_boundary, fig_retention,
               fig_composition, fig_margin, fig_v2, fig_safety):
        try:
            fn()
        except Exception as ex:
            print(f"  !! {fn.__name__}: {type(ex).__name__}: {ex}", file=sys.stderr)
            raise
