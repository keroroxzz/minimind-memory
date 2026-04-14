python train_pretrain.py \
  --data_path "../dataset/pretrain_t2t_mini.jsonl" \
  --tokenizer_path "../model" \
  --use_dense_attention 1 \
  --use_moe 0 \
  --use_engram 0 \
  --batch_size 32 \
  --accumulation_steps 8 \
  --use_wandb \
  --save_weight pretrain_dense_test