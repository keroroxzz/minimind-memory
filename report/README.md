# 研究報告

`memory_module.tex` —— 可分離記憶模組的完整實證研究報告（G1 ~ G6）。

## 編譯

需要 **XeLaTeX**（因為用 `ctex` 排中文）。本機目前**沒有安裝 LaTeX 引擎**，
但中文字型（Noto Serif/Sans CJK TC）已存在，裝好引擎即可直接編。

```bash
# Debian/Ubuntu：texlive-xetex 提供 xelatex，texlive-lang-chinese 提供 ctex
sudo apt install texlive-xetex texlive-lang-chinese texlive-fonts-recommended

cd report
xelatex memory_module.tex      # 跑兩次，第二次才會生成正確的目錄與交叉引用
xelatex memory_module.tex
```

若字型缺失，改 `\setCJKmainfont` 為系統上實際有的字型：

```bash
fc-list :lang=zh family | sort -u | head
```

## 內容對照

報告的每個章節都對應 `research.md` 的實驗記錄與 `experiments/` 的腳本：

| 報告章節 | research.md | 主要腳本 |
|---|---|---|
| §4 G1 交付 | §4.23–4.25 | `g1_train.py` |
| §5 G2 檢索 | §4.26–4.30 | `g2_train.py`, `g2c_identity_gate.py`, `g2c_cal_v2.py` |
| §6 G3 寫入/閉環/故障/邊界 | §4.31–4.39 | `g3a_train.py` … `g3f_membership_guard.py` |
| §7 G4 位址與時序 | §4.39–4.41 | `g4a_*.py`, `g4b_*.py` |
| §8 G5 能力面 | §4.42–4.45 | `g5a_*.py`, `g5b_*.py`, `g5c_*.py` |
| §9 G6 修復嘗試 | §4.46–4.48 | `g6a_*.py`, `g6c_margin.py`, `g6d/e_*.py` |
| §10 已撤回的主張 | 散見全篇 | — |

代理間的往返審查記錄在 `claude-speak.md` 與 `codex-speak.md`；
所有數值的原始輸出在 `experiments/results_*.json`。

## 報告的紀律

報告中每個主張都帶著它的範圍限制，且**保留了所有失敗與被撤回的假設**——
§10 的撤回清單本身是研究成果的一部分，不是附註。
凡是「規格無效」的實驗（介入與基準不可分辨）都明確標為 INVALID，
與「失敗結果」分開計，不得混為一談。
