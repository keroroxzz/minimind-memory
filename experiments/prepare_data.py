"""把實驗子集一次性 tokenize 成固定的 token 張量。

四個 config 共用同一份 .pt，確保：
  1. 資料內容與順序完全相同 (公平比較的前提)
  2. 不用每次跑都重新 tokenize
"""
import os
import sys
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
from transformers import AutoTokenizer

SRC = "/media/rtu/storage/ubuntu/minimides/exp_subset.jsonl"
OUT = os.path.join(os.path.dirname(__file__), "data_tokens.pt")
SEQ_LEN = 512
N_TRAIN = 48000
N_VAL = 2000


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(os.path.dirname(__file__), "..", "model"))
    print(f"tokenizer vocab={tok.vocab_size} bos={tok.bos_token_id} eos={tok.eos_token_id} pad={tok.pad_token_id}")

    rows = []
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line)["text"])
    print(f"讀入 {len(rows)} 筆原始樣本")

    # --- Packing：把文件首尾相接後切成固定長度 ---
    # 直接 padding 到 512 會有 ~58% 是 pad，等於浪費一半以上的算力，而且 MiniMind 的
    # PretrainDataset 不傳 attention_mask，pad token 其實會被 attend 到。改用 packing
    # 讓每個位置都是真實 token，同樣的 wall-clock 下訊號強得多。
    keep = N_TRAIN + N_VAL
    need_tokens = keep * SEQ_LEN
    stream = []
    for i in range(0, len(rows), 512):
        enc = tok(rows[i:i + 512], add_special_tokens=False)["input_ids"]
        for ids in enc:
            stream.append(tok.bos_token_id)
            stream.extend(ids)
            stream.append(tok.eos_token_id)
        if len(stream) >= need_tokens:
            break
        if (i // 512) % 20 == 0:
            print(f"  已 tokenize {len(stream)/1e6:.2f}M / {need_tokens/1e6:.2f}M tokens", flush=True)

    if len(stream) < need_tokens:
        raise RuntimeError(f"token 不足：只有 {len(stream)}，需要 {need_tokens}。請加大子集。")

    data = torch.tensor(stream[:need_tokens], dtype=torch.long).view(keep, SEQ_LEN)
    print(f"最終張量: {tuple(data.shape)}")

    # packing 之後沒有 padding，每個位置都是有效的預測目標
    labels = data.clone()

    torch.save({
        "train_ids": data[:N_TRAIN], "train_labels": labels[:N_TRAIN],
        "val_ids": data[N_TRAIN:], "val_labels": labels[N_TRAIN:],
        "seq_len": SEQ_LEN, "pad_token_id": tok.pad_token_id,
    }, OUT)

    print(f"✅ 已存至 {OUT}")
    print(f"   train={N_TRAIN} val={N_VAL} seq_len={SEQ_LEN}")
    print(f"   train 真實 token 數 = {N_TRAIN*SEQ_LEN/1e6:.2f}M (packing，無 padding)")


if __name__ == "__main__":
    main()
