# Research report

Two versions of the same report:

| File | Language | Engine | Figures |
|---|---|---|---|
| `memory_module_en.tex` | English | **pdflatex** (xelatex/lualatex also fine) | 8, embedded |
| `memory_module.tex` | 繁體中文 | **xelatex only** (needs `ctex` + Noto CJK) | none |

The English version is the primary one — it has the figures, needs no CJK fonts,
and compiles with a plain TeX Live install.

## Build

```bash
# figures first (reads experiments/results_*.json, writes fig/*.pdf + fig/*.png)
python report/make_figures.py

cd report
pdflatex memory_module_en.tex      # run twice for the ToC and cross-references
pdflatex memory_module_en.tex
```

No LaTeX engine is installed on this machine, so the `.tex` has **not been
compiled here** — it has only been checked structurally (environment pairing,
table cell counts vs. column specs, figure references, no stray markdown).
Install one with:

```bash
sudo apt install texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended
# for the Chinese version, additionally:
sudo apt install texlive-xetex texlive-lang-chinese
```

## Figures

`make_figures.py` reads the recorded JSON results directly — **no plotted number
is typed by hand where a JSON exists**. Three tables predate the JSON convention
(the S₅ depth sweep, the looped-transformer ceilings, the G1 delivery comparison);
those are declared once at the top of the script with a provenance comment so the
figures and the prose cannot drift apart.

| Figure | Shows | Source |
|---|---|---|
| `depth` | S₅ depth ceiling; looped-transformer ceilings | research.md / CLAUDE.md |
| `delivery` | 8 delivery mechanisms, residual vs. absolute | research.md §4.23–4.25 |
| `boundary` | store size flat to 31; k-axis censored by the backbone | `results_g3d_{pool,k}.json` |
| `retention` | memory flat vs. full-context collapsing; token cost | `results_g5a.json` |
| `composition` | repeated delivery / composition-after-delivery / segmentation | `results_g5c*.json`, `results_g5b.json` |
| `margin` | m_text flat, Δm degrading, 100% flip coverage | `results_g6c.json` |
| `v2` | zdelta-v2: config transfers, depth does not | `results_g6a.json` |
| `safety` | membership guard; the G3b causal chain | `results_g3e_k1.json`, `results_g3f.json`, `results_g3b.json` |

## Section map

| Report | research.md | Main scripts |
|---|---|---|
| §4 G1 delivery | §4.23–4.25 | `g1_train.py` |
| §5 G2 retrieval | §4.26–4.30 | `g2_train.py`, `g2c_identity_gate.py`, `g2c_cal_v2.py` |
| §6 G3 write / closure / faults / boundaries / safety | §4.31–4.38 | `g3a_train.py` … `g3f_membership_guard.py` |
| §7 G4 addressing and temporal binding | §4.39–4.41 | `g4a_*.py`, `g4b_*.py` |
| §8 G5 capability | §4.42–4.45 | `g5a_*.py`, `g5b_*.py`, `g5c_*.py` |
| §9 G6 repair attempts | §4.46–4.48 | `g6a_*.py`, `g6c_margin.py`, `g6d/e_*.py` |
| §10 Retracted claims | throughout | — |

## Editorial rules the report follows

Every claim carries its scope. **All failures and withdrawn hypotheses are kept** —
§10's retraction table is a result, not an appendix note. Experiments whose
intervention was indistinguishable from the baseline are labelled **INVALID** and
counted separately from failed results; their numbers may not be compared against
the baseline. Two "100%" readings that later proved underpowered are corrected in
place, with the observation preserved and only the interpretation withdrawn.
