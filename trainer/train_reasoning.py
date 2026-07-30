import os
import sys

__package__ = "trainer"
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import argparse
import time
import warnings
import torch
import torch.distributed as dist
from contextlib import nullcontext
from torch import optim, nn
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader, DistributedSampler
from model.model_minimind import MiniMindConfig
from dataset.data_reasoning import ReasoningDataset
from trainer.trainer_utils import get_lr, Logger, is_main_process, lm_checkpoint, init_distributed_mode, setup_seed, init_model, SkipBatchSampler, get_model_paths

warnings.filterwarnings('ignore')


def train_epoch(epoch, loader, iters, start_step=0, wandb=None):
    start_time = time.time()
    last_step = start_step
    # 初始化遞迴記憶體
    mems = None
    
    for step, (input_ids, labels) in enumerate(loader, start=start_step + 1):
        input_ids = input_ids.to(args.device)
        labels = labels.to(args.device)
        last_step = step
        
        # 動態調整學習率
        lr = get_lr(epoch * iters + step, args.epochs * iters, args.learning_rate)
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        with autocast_ctx:
            # 傳遞 mems 到模型
            res = model(input_ids, labels=labels, mems=mems)
            loss = res.loss + res.aux_loss
            loss = loss / args.accumulation_steps
            
            # 更新 mems 並 detach (Segment-Level Recurrence)
            if lm_config.use_recurrence:
                mems = res.next_mems # next_mems 已經在模型內部 detach() 過了

        scaler.scale(loss).backward()

        if step % args.accumulation_steps == 0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)

            scaler.step(optimizer)
            scaler.update()

            optimizer.zero_grad(set_to_none=True)

        if step % args.log_interval == 0 or step == iters:
            spend_time = time.time() - start_time
            current_loss = loss.item() * args.accumulation_steps
            current_aux_loss = res.aux_loss.item() if res.aux_loss is not None else 0.0
            current_logits_loss = current_loss - current_aux_loss
            current_lr = optimizer.param_groups[-1]['lr']
            eta_min = spend_time / max(step - start_step, 1) * (iters - step) // 60
            Logger(f'Epoch:[{epoch + 1}/{args.epochs}]({step}/{iters}), loss: {current_loss:.4f}, logits_loss: {current_logits_loss:.4f}, aux_loss: {current_aux_loss:.4f}, lr: {current_lr:.8f}, eta: {eta_min:.1f}min')
            if wandb: wandb.log({"loss": current_loss, "logits_loss": current_logits_loss, "aux_loss": current_aux_loss, "learning_rate": current_lr})

        if (step % args.save_interval == 0 or step == iters) and is_main_process():
            model.eval()
            ckp, _ = get_model_paths(args.save_dir, args.save_weight, lm_config)
            raw_model = model.module if isinstance(model, DistributedDataParallel) else model
            raw_model = getattr(raw_model, '_orig_mod', raw_model)
            state_dict = raw_model.state_dict()
            # 保存為 FP16 以節省空間
            torch.save({k: v.half().cpu() for k, v in state_dict.items()}, ckp)
            lm_checkpoint(lm_config, weight=args.save_weight, model=model, optimizer=optimizer, scaler=scaler, epoch=epoch, step=step, wandb=wandb, save_dir='../checkpoints')
            model.train()
            del state_dict

        del input_ids, labels, res, loss

    # 確保最後一組梯度被更新
    if last_step > start_step and last_step % args.accumulation_steps != 0:
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad(set_to_none=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MiniMind Reasoning Training")
    parser.add_argument("--save_dir", type=str, default="../out", help="模型保存目錄")
    parser.add_argument('--save_weight', default='reason', type=str, help="保存權重的前綴名")
    parser.add_argument("--epochs", type=int, default=3, help="訓練輪數")
    parser.add_argument("--batch_size", type=int, default=8, help="batch size")
    parser.add_argument("--learning_rate", type=float, default=2e-5, help="初始學習率")
    parser.add_argument("--device", type=str, default="cuda:0" if torch.cuda.is_available() else "cpu", help="訓練設備")
    parser.add_argument("--dtype", type=str, default="bfloat16", help="混合精度類型")
    parser.add_argument("--num_workers", type=int, default=4, help="數據加載線程數")
    parser.add_argument("--accumulation_steps", type=int, default=8, help="梯度累積步數")
    parser.add_argument("--grad_clip", type=float, default=1.0, help="梯度裁剪閾值")
    parser.add_argument("--log_interval", type=int, default=50, help="日誌打印間隔")
    parser.add_argument("--save_interval", type=int, default=500, help="模型保存間隔")
    
    # 架構控制
    parser.add_argument('--hidden_size', default=768, type=int, help="隱藏層維度")
    parser.add_argument('--num_hidden_layers', default=8, type=int, help="隱藏層數量")
    parser.add_argument('--max_seq_len', default=512, type=int, help="最大截斷長度")
    parser.add_argument('--use_moe', default=0, type=int, choices=[0, 1], help="是否使用MoE")
    parser.add_argument('--use_looped_transformer', default=0, type=int, choices=[0, 1], help="是否使用Looped Transformer架构")
    parser.add_argument('--num_loops', default=1, type=int, help="Transformer 循环次数")
    parser.add_argument('--loop_lora_rank', default=16, type=int, help="Loop LoRA rank")
    parser.add_argument('--use_engram', default=0, type=int, choices=[0, 1], help="是否使用Engram")
    parser.add_argument('--use_dense_attention', default=1, type=int, choices=[0, 1], help="是否使用Dense Attention")
    parser.add_argument('--use_latent_attention', default=0, type=int, choices=[0, 1], help="是否使用Latent Attention")
    parser.add_argument('--use_recurrence', default=0, type=int, choices=[0, 1], help="是否使用遞迴機制（0=否，1=是）")
    parser.add_argument('--mem_len', default=512, type=int, help="遞迴記憶長度")
    
    # 模型與資料載入控制
    parser.add_argument("--tokenizer_path", type=str, default="./model", help="Tokenizer 目錄")
    parser.add_argument("--max_samples", type=int, default=None, help="每個資料集的最大樣本數")
    parser.add_argument('--from_weight', default='none', type=str, help="基於哪個權重訓練")
    parser.add_argument('--from_resume', default=0, type=int, choices=[0, 1], help="是否自動檢測&續訓")
    
    # 工具控制
    parser.add_argument("--use_wandb", action="store_true", help="是否使用wandb")
    parser.add_argument("--wandb_project", type=str, default="MiniMind-Reasoning", help="wandb項目名")
    parser.add_argument("--use_compile", default=0, type=int, choices=[0, 1], help="是否使用torch.compile")
    args = parser.parse_args()

    # ========== 1. 初始化環境 ==========
    local_rank = init_distributed_mode()
    if dist.is_initialized(): args.device = f"cuda:{local_rank}"
    setup_seed(42 + (dist.get_rank() if dist.is_initialized() else 0))
    
    # ========== 2. 配置配置類 ==========
    os.makedirs(args.save_dir, exist_ok=True)
    lm_config = MiniMindConfig(
        hidden_size=args.hidden_size, 
        num_hidden_layers=args.num_hidden_layers, 
        use_moe=bool(args.use_moe), 
        use_engram=bool(args.use_engram),
        use_dense_attention=bool(args.use_dense_attention),
        use_latent_attention=bool(args.use_latent_attention),
        use_recurrence=bool(args.use_recurrence),
        mem_len=args.mem_len,
        use_looped_transformer=bool(args.use_looped_transformer) if hasattr(args, 'use_looped_transformer') else False,
        num_loops=args.num_loops if hasattr(args, 'num_loops') else 1,
        loop_lora_rank=args.loop_lora_rank if hasattr(args, 'loop_lora_rank') else 16)
    ckp_data = lm_checkpoint(lm_config, weight=args.save_weight, save_dir='../checkpoints') if args.from_resume==1 else None
    
    # ========== 3. 混合精度與環境 ==========
    device_type = "cuda" if "cuda" in args.device else "cpu"
    dtype = torch.bfloat16 if args.dtype == "bfloat16" else torch.float16
    autocast_ctx = nullcontext() if device_type == "cpu" else torch.cuda.amp.autocast(dtype=dtype)
    
    # ========== 4. WandB ==========
    wandb = None
    if args.use_wandb and is_main_process():
        import wandb
        wandb_id = ckp_data.get('wandb_id') if ckp_data else None
        resume = 'must' if wandb_id else None
        wandb_run_name = f"MiniMind-Reasoning-Epoch-{args.epochs}-BatchSize-{args.batch_size}-LearningRate-{args.learning_rate}"
        wandb.init(
            project=args.wandb_project,
            name=wandb_run_name,
            id=wandb_id,
            resume=resume,
            config=lm_config.__dict__)
    
    # ========== 5. 模型與資料載入 ==========
    # 初始化模型（會自動處理 from_weight）
    model, tokenizer = init_model(lm_config, args.from_weight, tokenizer_path=args.tokenizer_path, device=args.device)
    
    # 載入 Reasoning 資料集 (注意：資料載入會花一些時間進行長度過濾)
    train_ds = ReasoningDataset(tokenizer, max_length=args.max_seq_len, max_samples=args.max_samples)
    
    train_sampler = DistributedSampler(train_ds) if dist.is_initialized() else None
    scaler = torch.cuda.amp.GradScaler(enabled=(args.dtype == 'float16'))
    optimizer = optim.AdamW(model.parameters(), lr=args.learning_rate)
    
    # ========== 6. 恢復狀態 ==========
    start_epoch, start_step = 0, 0
    if ckp_data:
        model.load_state_dict(ckp_data['model'])
        optimizer.load_state_dict(ckp_data['optimizer'])
        scaler.load_state_dict(ckp_data['scaler'])
        start_epoch = ckp_data['epoch']
        start_step = ckp_data.get('step', 0)
    
    # ========== 7. 分散式與編譯 ==========
    if args.use_compile == 1:
        model = torch.compile(model)
    if dist.is_initialized():
        model._ddp_params_and_buffers_to_ignore = {"freqs_cos", "freqs_sin"}
        model = DistributedDataParallel(model, device_ids=[local_rank])
    
    # ========== 8. 訓練 ==========
    for epoch in range(start_epoch, args.epochs):
        if train_sampler:
            train_sampler.set_epoch(epoch)
            loader = DataLoader(train_ds, batch_size=args.batch_size, sampler=train_sampler, num_workers=args.num_workers, pin_memory=True)
        else:
            # 使用隨機打亂
            setup_seed(42 + epoch)
            loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True)
            
        train_epoch(epoch, loader, len(loader), start_step if epoch == start_epoch else 0, wandb)
        # 每輪結束重置 start_step
        start_step = 0
    
    # ========== 9. 清理 ==========
    if dist.is_initialized(): dist.destroy_process_group()
