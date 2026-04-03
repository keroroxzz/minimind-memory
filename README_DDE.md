# MiniMind DDE-v1 (Dynamic Discrete Engram)

MiniMind DDE-v1 是一個基於 MiniMind 體系結構的擴展模組，旨在賦予微型語言模型（LLM）**動態離散記憶（Dynamic Working Memory）**的能力。

與靜態詞組查表不同，DDE-v1 實現了**端到端可微的記憶讀寫鏈條**，讓模型能真正學會「為了回答後面的問題，我現在應該記住什麼」。

---

## 核心技術實作 (Advanced Implementation)

### 1. 區塊化可微記憶 (Chunk-based Differentiable Memory)
為了解決 Transformer 平行運算與記憶時序性的衝突，我們將 Forward 過程切分為兩個階段，並在單次 Forward 中完成：
*   **Context 區塊 (寫入階段)**：處理事實注入。此階段產生的記憶更新量帶有完整梯度函數，並生成 `new_memory` 狀態。
*   **Query 區塊 (讀取階段)**：處理問題回答。模型從 `new_memory` 中提取信息。
*   **梯度回傳 (BPTT)**：Query 端的 Loss 產生的梯度可以完美穿透記憶狀態，回流至 Context 端的 **Packer** 與 **Write Gate**，強制模型學會篩選關鍵信息。

### 2. 嘆息之牆 (Chunked Attention Mask)
為了防止模型透過原生 Attention 直接觀察 Context 來「偷看答案」（捷徑依賴），我們實作了客製化的 4D Mask：
*   **物理隔離**：Query 區塊的 Token 對 Context 區塊的 Attention 權重被強制設為 `-inf`。
*   **強制依賴**：模型**必須**透過 DDE 記憶高速公路來傳遞跨區塊信息，否則無法回答問題。

### 3. 層次化定址 (Hierarchical Addressing)
採用 16 (Coarse) x 64 (Fine) 的結構管理 1024 個 Slot：
*   透過外積（Outer Product）計算機率分佈，提供比單層大空間更穩定的定址梯度。
*   **溫度退火**：訓練從 τ=2.0 (全記憶遍歷) 退火至 τ=0.5 (精確離散定位)。

---

## 核心設計原則 (DDE Constitution)

1. **主幹凍結 (Frozen Backbone)**：鎖定語義空間，確保 DDE 模組在穩定的特徵基礎上學習地址映射。
2. **運行態記憶 (Runtime State)**：記憶表雖作為 `Parameter` (初始值) 參與訓練，但其更新過程是基於隱藏狀態的動態計算。
3. **門控過濾 (Gated I/O)**：
    *   **Write Gate**：自發學習哪些 Token 具備高度驚訝度，值得被寫入。
    *   **Read Gate**：控制記憶內容注入殘差流的比例。

---

## 訓練策略

### 輔助損失函數 (Auxiliary Losses)
*   **Diversity Loss (熵損失)**：防止定址坍縮到少數 Slot，激勵 1024 個空間的平均利用。
*   **Sparsity Loss (稀疏損失)**：鼓勵寫入門控在非必要時保持關閉，減少記憶噪音。

---

## 使用指南

### 1. 訓練 DDE
```bash
python trainer/train_dde.py --data_path "../dataset/dde-v1.jsonl" --from_weight "../minimind-3/model.safetensors"
```

### 2. 數據集格式
DDE 期待 `dialogue` 格式，其中最後兩輪被定義為 Query，其餘為 Context：
```json
{
  "dialogue": [
    {"role": "user", "content": "事實：小明的手機號碼是 12345"},
    {"role": "user", "content": "問題：小明的手機號碼是多少？"},
    {"role": "assistant", "content": "12345"}
  ]
}
```

---

## 下一步計劃 (DDE-v2)
*   **LRU 管理器**：動態管理 Slot 的生命週期。
*   **跨樣本持久化**：在長對話中保持記憶不被重置。
