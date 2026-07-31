"""把實驗資料一次性 tokenize 成固定的 token 張量。

所有 config 共用同一份 .pt，確保：
  1. 資料內容與順序完全相同 (公平比較的前提)
  2. 不用每次跑都重新 tokenize

用 packing（文件首尾相接後切成固定長度）而非 padding —— 直接 pad 到 512 會有
~58% 是 pad，等於浪費一半以上算力，而且 PretrainDataset 不傳 attention_mask，
pad token 其實會被 attend 到。

儲存用 int16：vocab 只有 6400，遠小於 int16 上限，比 int64 省 4 倍 RAM/磁碟。
246M tokens 下是 0.49G 而非 1.97G。訓練時每個 batch 再轉回 long。
"""
import os
import sys
import json
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
from transformers import AutoTokenizer

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DEFAULT = os.path.join(HERE, "..", "dataset", "pretrain_t2t_mini.jsonl")
SEQ_LEN = 512


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC_DEFAULT)
    ap.add_argument("--out", default=os.path.join(HERE, "data_tokens.pt"))
    ap.add_argument("--train-seqs", type=int, default=480_000, help="訓練序列數 (×512 = tokens)")
    ap.add_argument("--val-seqs", type=int, default=2_000)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    print(f"tokenizer vocab={tok.vocab_size} bos={tok.bos_token_id} eos={tok.eos_token_id}")

    keep = args.train_seqs + args.val_seqs
    need = keep * SEQ_LEN
    print(f"目標 {keep} 條 × {SEQ_LEN} = {need/1e6:.1f}M tokens")
    assert tok.vocab_size < 2**15, "vocab 超過 int16 範圍"

    # 串流讀取 + 分批 tokenize，避免把 1.2G 的 jsonl 整份讀進 RAM
    chunks, total = [], 0
    buf = []
    with open(args.src, encoding="utf-8") as f:
        for line in f:
            buf.append(json.loads(line)["text"])
            if len(buf) < 1024:
                continue
            enc = tok(buf, add_special_tokens=False)["input_ids"]
            buf = []
            flat = []
            for ids in enc:
                flat.append(tok.bos_token_id)
                flat.extend(ids)
                flat.append(tok.eos_token_id)
            chunks.append(torch.tensor(flat, dtype=torch.int16))
            total += len(flat)
            if total >= need:
                break
            if len(chunks) % 100 == 0:
                print(f"  {total/1e6:7.1f}M / {need/1e6:.1f}M tokens ({total/need:5.1%})", flush=True)

    if total < need:
        raise RuntimeError(f"token 不足：只有 {total/1e6:.1f}M，需要 {need/1e6:.1f}M。請換更大的 --src。")

    data = torch.cat(chunks)[:need].view(keep, SEQ_LEN)
    del chunks
    print(f"最終張量: {tuple(data.shape)} dtype={data.dtype}")

    torch.save({
        "train_ids": data[:args.train_seqs],
        "val_ids": data[args.train_seqs:],
        "seq_len": SEQ_LEN,
        "vocab_size": tok.vocab_size,
    }, args.out)

    sz = os.path.getsize(args.out) / 1e9
    print(f"✅ 已存至 {args.out} ({sz:.2f} GB)")
    print(f"   train={args.train_seqs} 條 = {args.train_seqs*SEQ_LEN/1e6:.1f}M tokens")
    print(f"   val  ={args.val_seqs} 條 (訓練期間完全沒看過)")


if __name__ == "__main__":
    main()
