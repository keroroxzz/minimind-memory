# 預先登記 — latent-only core（使用者的真正目標）

**寫於 2026-08-06，在任何 latent-only 結果之前。** 規格來自 Codex「使用者補充的真正目標」那則。

## 0. 成功句（取代舊的）

> **latent-only query 在無文字事實、無 placeholder、無特殊 prompt token 下仍能正確回答。**

**不是**「模型能解讀佔位符」。placeholder bridge（§4.51–4.54、A 臂、`[QADDR]`）
**只作診斷工具保留**，不是最終架構驗證。**`[QADDR]` 方案就此擱置。**

## 1. render（文字裡不再有事實）

| render | 文字 | 記憶 |
|---|---|---|
| **`L0`** | `\| anna a 是 4 7 \| ben b 是 8 2 問 anna a 是 多少 ?` | 無（天花板）|
| **`latent`** | **`問 anna a 是 多少 ?`** | 以 **prepended carrier embedding** 進入 |
| `floor` | 同上 | carrier 全零（評估用地板）|

**`latent` 的文字裡完全沒有事實區、沒有 `. .`、沒有新 token。**
記憶走 `memory_carriers`（既有管道，未使用時逐位元相容），
**不經 tokenizer、不佔文字**。

## 2. schema `z'`（舊 `z=[value,attr]` 作廢）

```
z' = [ addr(16) , value/100 (1) , attr one-hot(3) ]      共 20 維
       └─ φ(name, attr)：固定、確定性、未訓練
```

`φ` 對 memory 側與 query 側是**同一個函數**。舊 schema 對
`dan b=47` / `anna b=47` **完全碰撞**，資訊論上不可能恢復身份（Codex [138]）。

**query 側不注入任何東西** —— 問題的 entity 就寫在文字裡（`問 anna a ...`），
core 必須學會把**文字 entity** 與 **carrier 的 addr** 對上。
這就是 Codex 說的「把『core 會比較 address』提升為內建能力」，
因此**後續 B 測的是 learned delivery 能否追平 oracle，不是 address usage 從零湧現**。

## 3. carrier embedding 用**固定未訓練投影**（沿用 oracle inline 的原則）

`z' → hidden(512)` 用**固定隨機投影**（seed 鎖死，不進 optimizer），
並縮放到 token embedding 的平均範數。

理由與 `bridge_train_core.py` 相同：若投影可訓練，core 會學會某個特定 adapter 的輸出，
**後續 delivery 實驗就失去意義**。

## 4. 必須隨機化 carrier 順序

每個 episode **隨機打亂 carrier 的排列**。否則模型可用「第 k 個 carrier」取代 address。
**測試時另報未見過的排列。**

## 5. 訓練組成

- `L0` 與 `latent` **各半**（沿用 §4.50 的一級變因規定）。
- **不訓練「空文字＋無 memory」**：那等於教模型在沒有依據時仍輸出答案（訓練幻覺）。
  該 render **只作評估地板**。abstention 是獨立軸（R4），不在本版開。
- 同一 batch 內 `n_fact` 固定（`memory_carriers` 要求 batch 內 carrier 數一致），
  batch 之間仍在 2..4 隨機。

## 6. gate 順序與煞車（Codex 明令）

1. `L0` text ceiling
2. **`latent` oracle `n=1` consumption ceiling**
3. `latent` oracle `n≥2` ceiling
4. 才談 learned retriever / delivery

> **任何有 latent render 的 `n=1` ceiling < 95%，只記 `core/render invalid`，
> 不解讀 binding，也不談 retriever。**

`j=0`（純消費）與 `j>0`（融合）**分開報**。

## 7. 兩個 gate 分離（不得合併）

- **`retrieval`**：query → 選出哪些 `z`
- **`consumption/fusion`**：給定 `z` → 空文字下答對

本版**只做後者**，retrieval 用 oracle（給定正確的 memory 集合）。
**不得**把選中的答案或 target slot 直接注入。

## 8. 不得宣稱

- **不得宣稱省 context／token**，直到另做大小／計算量對照（Codex [138]）。
- carrier **確實佔用序列位置**（RoPE 位移），只是不佔**文字 token**。這點必須寫明。
- 舊 core 的 `_f8`/`_f8v`/`_f8k`/`_tagA` 全部綁在舊 core，**不回寫、不比較**。
