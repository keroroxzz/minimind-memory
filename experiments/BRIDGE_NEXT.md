# 橋接任務：接手指南

`research.md` §4.50 是完整決議。這裡只寫「訓練完之後做什麼」。

## 現在在跑什麼

`bridge_train_core.py` —— 新 core（29.37M，`num_loops=4` 作為**最大 budget**），
24000 步、約 1.8 小時，輸出 `bridge_core.pth`。

**同時在兩種 render 上訓練**（各半）：
- `L0`：值寫在文字裡
- `carrier`：值由 **oracle inline**（零參數）在 placeholder 位置到達

這是為了杜絕 G4b 那個錯 —— 當時 oracle 天花板掉到 **0.0%**，只因為 core
沒見過那個格式，而那與記憶完全無關。

## 訓練完的三個 gate（依序，任何一關不過就停）

1. **`L0` 天花板與 loop ceiling** ——
   用最大 loop budget 跑 `L0` 的 depth sweep（j=0..4+），
   **`num_loops` 依只看 `L0` 的預先登記規則選**，**不沿用 S₅ 的 2**。
2. **`j=0` 的 consumer ceiling** —— oracle latent（連續）在純讀出上的天花板。
3. **`j=1`（最多 `j=2`）的 fusion ceiling**。

**任何 `L0` < 95% 的格子 censor** —— 不把「core 看不懂語法」誤判成 memory FAIL。

## 第一個正式實驗（Codex 定案，我原本的提案被否決）

> **連續內容 schema ＋ 最小可驗的 `j=0 → j=1`（必要時 `j=2`）融合探針。**
> 固定 oracle binding，**不開** semantic retrieval、**不開** open-set。

我原本提「連續內容 ＋ 多步融合」，被指出是**兩個大題疊加**、會重演混淆。

**判讀事前鎖定**：
- `j=0` 就敗 → **先修 core/adapter**，不是融合問題
- **`j=0` 過、`j=1` 敗 → 才有資格談真正的融合介面**

## 跨任務帶過來的東西（只有方法，沒有數字）

- 契約與故障演練、`operator range preflight`、paired split
- oracle ceiling 與 censoring、episode reset
- `CVaR10(Δm)`、`P(c|dec)`、paired logit 分解
- **假說**：G7d 的「argmax 可解碼 ≠ 可消費」（core manifold / consumer
  interface mismatch）—— 但 28 格數字**不帶**

## 不帶過來的（S₅-local，不得當自然語言證據）

G1–G7 的**一切具體數值與 PASS/FAIL**。特別是：
- G5c 的「交付後合成劣化」只是**待驗預測**（那是群乘法）
- G2c 的 exact-key open-set FAIL
- writer 的 120 類泛化

## 完整 ladder（不可跳階）

連續碼／消費與一小步融合 → 描述檢索 → 近鄰/衝突 → 更長多步融合

## ③ state separation

抽象 spec 可寫，**但不在 S₅ 上跑正式版** —— 實作目標是這個新 core。
