# Attention 架構對照實驗

比較四種設定在小資料集上的收斂速度與 held-out 表現：

| config | `use_engram` | `use_dense_attention` |
|---|---|---|
| `vanilla` | ✗ | ✗ |
| `engram` | ✓ | ✗ |
| `complete_att` | ✗ | ✓ |
| `engram+ca` | ✓ | ✓ |

## 執行方式

```bash
conda activate sd
python experiments/prepare_data.py       # 一次性 tokenize，產生 data_tokens.pt
python experiments/run_ablation.py       # 跑完四個 config，輸出 results.json
python experiments/plot_results.py       # 產生 convergence.svg 與比較表
```

單獨重跑一個：`python experiments/run_ablation.py --only engram`

## 實驗設定

- 骨幹：8 層 / 512 dim / 8 heads / 2 KV heads，vocab 6400 → backbone 28.98M 參數
- 資料：`pretrain_t2t_mini.jsonl` 前 150k 行，**packing** 成 512-token 序列（無 padding）
- train 48,000 條 / val 2,000 條（訓練期間完全沒看過）
- 3000 步 × bs16 × 512 = **24.58M tokens**，恰好一個 epoch
- AdamW，lr 5e-4，cosine + 150 步 warmup，grad clip 1.0，bf16 autocast
- 所有 config 共用同一個 seed 與**完全相同的批次順序**

## 必須注意的實驗設計問題

1. **參數量不對等。** `use_dense_attention` 不增加任何參數，`use_engram` 則多出
   105.47M（總量 29M → 134M，4.6 倍）。engram 的任何優勢都必須先扣掉這一項才有意義。
2. **`use_engram` 預設是 `True`。** `MiniMindConfig` 的預設值會讓 "vanilla" 偷偷帶著
   engram 跑，因此 `CONFIGS` 裡每個旗標都寫死。
3. **weight decay 必須分組。** AdamW 每步衰減所有參數，但 105M 列的雜湊表每步只有
   約 0.4% 的列拿得到梯度；統一 decay 等於持續把未造訪的列拉向 0，對 engram 不公平。
   目前 embedding / norm / engram 表一律豁免。
   `results_wd_on_all.json` 保留了「全部套用 decay」的對照組。
4. **`engram_offload_cpu=False`。** CPU offload 慢 6.2 倍（1.30 vs 8.03 step/s），
   12G VRAM 放得下，且只影響表存在哪、不影響數學。
5. **單一 seed。** 小於約 0.05 的 val loss 差距不應視為顯著。
6. **規模極小。** 29M 參數只看 24.6M tokens，遠低於 compute-optimal；
   架構優勢常常要到更大規模或更長訓練才顯現。
