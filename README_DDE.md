# MiniMind DDE-v1.7 (Dynamic Discrete Engram)

MiniMind DDE-v1.7 是一個基於 MiniMind 體系結構的高級擴展模組，旨在賦予微型語言模型（LLM）**高密度、低噪聲的動態離散工作記憶**能力。

從 v1.0 到 v1.7，我們將記憶從「全量模糊遍歷」進化到了「精確稀疏定位」，大幅提升了模型在物理隔離環境下的事實提取能力。

---

## 核心技術演進 (Architectural Evolution)

### 1. Top-K 稀疏定址 (Sparse Memory Access)
為了解決 v1.0 中 1024 個 Slot 導致的資訊稀釋與定址噪聲，v1.7 引入了稀疏機制：
*   **高密度 Slot**：精簡為 **256 個高效 Slot** (16 Coarse x 16 Fine)，增加每個 Slot 的資訊承載量。
*   **Top-K 提取 (K=8)**：模型在讀寫時僅與最強的 8 個 Slot 互動。這強迫模型學會精確定址，並確保記憶信號不被背景噪聲淹沒。

### 2. 無損語義對齊 (Lossless Alignment)
*   **維度對齊**：記憶維度從 256 直接提升至 **768**（與 MiniMind 隱藏層完全對齊）。
*   **Identity 初始化**：Packer (寫入) 與 Unpacker (讀取) 採用 **Identity (單位矩陣) 初始化**。這確保了模型在訓練初期能以「透傳」狀態傳遞資訊，極大地縮短了冷啟動時間。

### 3. 解除熵之陷阱 (KLD Diversity Loss)
*   **均勻目標 KLD**：不再簡單地最小化負熵，而是計算預測分佈與均勻分佈之間的 **KL 散度**。
*   **目的**：模型只有在「偏離均勻且無法降低 Logits Loss」時才會受到懲罰。這解決了模型為了騙取負熵獎勵而故意保持「無效均勻分佈」的問題。

### 4. 數值穩定性與深度 Indexer
*   **Deep Indexer**：增加層數並引入 `LayerNorm`，提供比原版更穩定的地址映射能力。
*   **FP32 核心運算**：Indexer 內部強制使用 `float32` 進行 softmax 與外積運算，防止因數值溢位導致的定址坍縮。

---

## 核心設計原則 (DDE Constitution)

1.  **主幹凍結 (Frozen Backbone)**：鎖定語義空間，確保 DDE 模組在穩定的特徵基礎上學習。
2.  **積極門控 (Aggressive Gating)**：
    *   **Write Gate**：初始偏置設為 **1.0**，強迫模型在訓練初期「多做筆記」。
    *   **Read Gate**：初始偏置設為 **0.0**，平衡記憶注入。
3.  **參數化 EMA (dde_ema_decay)**：支援動態調整記憶更新劇烈程度（預設 0.5），適配短對話事實記憶。

---

## 訓練指南 (v1.7 建議參數)

### 1. 執行訓練
```bash
python trainer/train_dde.py \
    --data_path "../dataset/dde-v1.jsonl" \
    --from_weight "../minimind-3/model.safetensors" \
    --dde_layer 4 \
    --epochs 50 \
    --batch_size 32 \
    --learning_rate 1e-3 \
    --accumulation_steps 4 \
    --dde_ema_decay 0.5 \
    --dde_diversity_weight 0.02 \
    --dde_sparsity_weight 0.05 \
    --dde_temp_start 2.0 \
    --dde_temp_end 0.2
```

### 2. 數據集格式 (簡潔模式)
捨棄了 `chat_template` 的冗餘標籤，採用純淨格式以節省記憶空間：
```json
{
  "dialogue": [
    {"role": "user", "content": "事實：小明今年 18 歲"},
    {"role": "user", "content": "問題：小明幾歲？"},
    {"role": "assistant", "content": "18"}
  ]
}
```

---

## 下一步計劃 (DDE-v2)
*   **Slot 生命周期管理**：引入 LRU 緩存。
*   **跨對話持久化**：讓 MiniMind 具備長期「認人」的能力。
