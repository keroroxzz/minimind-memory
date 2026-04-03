import os
import sys
import math

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
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from dataset.lm_dataset import DDEDataset
from trainer.trainer_utils import get_lr, Logger, is_main_process, lm_checkpoint, init_distributed_mode, setup_seed, init_model, SkipBatchSampler, get_model_paths

warnings.filterwarnings('ignore')

def train_epoch(epoch, loader, iters, start_step=0, wandb=None):
    start_time = time.time()
    last_step = start_step
    
    # 溫度退火參數
    temp_start = args.dde_temp_start
    temp_end = args.dde_temp_end
    total_steps = args.epochs * iters
    
    for step, (input_ids, labels, split_idx) in enumerate(loader, start=start_step + 1):
        input_ids = input_ids.to(args.device)
        labels = labels.to(args.device)
        split_idx = split_idx.to(args.device)
        last_step = step
        
        # 計算當前步數的總進度
        global_step = epoch * iters + step
        
        # 學習率調度
        lr = get_lr(global_step, total_steps, args.learning_rate)
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
            
        # 溫度退火調度 (線性退火)
        current_temp = max(temp_end, temp_start - (temp_start - temp_end) * (global_step / total_steps))

        with autocast_ctx:
            # 傳入 dde_temp 與 split_idx 給模型
            res = model(input_ids, labels=labels, dde_temp=current_temp, split_idx=split_idx)
            loss = res.loss + res.aux_loss
            loss = loss / args.accumulation_steps

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
            Logger(f'Epoch:[{epoch + 1}/{args.epochs}]({step}/{iters}), loss: {current_loss:.4f}, logits_loss: {current_logits_loss:.4f}, aux_loss: {current_aux_loss:.4f}, lr: {current_lr:.8f}, temp: {current_temp:.3f}, epoch_time: {eta_min:.1f}min')
            if wandb: 
                wandb.log({
                    "loss": current_loss, 
                    "logits_loss": current_logits_loss, 
                    "aux_loss": current_aux_loss, 
                    "learning_rate": current_lr, 
                    "dde_temp": current_temp,
                    "epoch_time": eta_min
                })

        if (step % args.save_interval == 0 or step == iters) and is_main_process():
            model.eval()
            ckp, _ = get_model_paths(args.save_dir, args.save_weight, lm_config)
            raw_model = model.module if isinstance(model, DistributedDataParallel) else model
            raw_model = getattr(raw_model, '_orig_mod', raw_model)
            state_dict = raw_model.state_dict()
            torch.save({k: v.half().cpu() for k, v in state_dict.items()}, ckp)
            model.train()
            del state_dict

        del input_ids, labels, res, loss

    if last_step > start_step and last_step % args.accumulation_steps != 0:
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad(set_to_none=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MiniMind DDE-v1 Training")
    parser.add_argument("--save_dir", type=str, default="../out", help="模型保存目录")
    parser.add_argument('--save_weight', default='dde_v1', type=str, help="保存权重的前綴名")
    parser.add_argument("--epochs", type=int, default=5, help="訓練輪數")
    parser.add_argument("--batch_size", type=int, default=16, help="batch size")
    parser.add_argument("--learning_rate", type=float, default=1e-3, help="初始学习率")
    parser.add_argument("--device", type=str, default="cuda:0" if torch.cuda.is_available() else "cpu", help="训练设备")
    parser.add_argument("--dtype", type=str, default="bfloat16", help="混合精度类型")
    parser.add_argument("--num_workers", type=int, default=8, help="数据加载线程数")
    parser.add_argument("--accumulation_steps", type=int, default=8, help="梯度累积步数")
    parser.add_argument("--grad_clip", type=float, default=1.0, help="梯度裁剪阈值")
    parser.add_argument("--log_interval", type=int, default=10, help="日志打印间隔")
    parser.add_argument("--save_interval", type=int, default=500, help="模型保存间隔")
    
    # 模型基礎架構 (需與預訓練模型一致)
    parser.add_argument('--hidden_size', default=768, type=int, help="隐藏层维度 (minimind-3 為 768)")
    parser.add_argument('--num_hidden_layers', default=8, type=int, help="隐藏层数量")
    parser.add_argument('--intermediate_size', default=2432, type=int, help="中間層維度")
    parser.add_argument('--rms_norm_eps', default=1e-6, type=float, help="RMSNorm epsilon")
    parser.add_argument('--use_moe', default=0, type=int, choices=[0, 1], help="是否使用MoE架构")
    parser.add_argument('--use_engram', default=0, type=int, choices=[0, 1], help="是否使用Engram架构")
    
    # DDE 專屬參數
    parser.add_argument('--dde_layer', default=4, type=int, help="DDE 插入層數")
    parser.add_argument('--dde_temp_start', default=2.0, type=float, help="溫度退火起始值")
    parser.add_argument('--dde_temp_end', default=0.5, type=float, help="溫度退火結束值")
    
    parser.add_argument("--max_seq_len", type=int, default=512, help="训练的最大截断长度")
    parser.add_argument("--data_path", type=str, default="../dataset/dde-v1.jsonl", help="記憶訓練數據路徑")
    parser.add_argument('--from_weight', default='../minimind-3/model.safetensors', type=str, help="預訓練權重路徑")
    parser.add_argument("--use_wandb", action="store_true", help="是否使用wandb")
    parser.add_argument("--wandb_project", type=str, default="MiniMind-DDE-v1", help="wandb项目名")
    
    args = parser.parse_args()

    # 1. 初始化環境
    local_rank = init_distributed_mode()
    if dist.is_initialized(): args.device = f"cuda:{local_rank}"
    setup_seed(42 + (dist.get_rank() if dist.is_initialized() else 0))
    
    # 2. 配置目錄與模型參數
    os.makedirs(args.save_dir, exist_ok=True)
    lm_config = MiniMindConfig(
        hidden_size=args.hidden_size, 
        num_hidden_layers=args.num_hidden_layers, 
        intermediate_size=args.intermediate_size,
        rms_norm_eps=args.rms_norm_eps,
        use_moe=bool(args.use_moe), 
        use_engram=bool(args.use_engram),
        use_dde=True,
        dde_layer=args.dde_layer
    )
    
    # 3. 混合精度
    device_type = "cuda" if "cuda" in args.device else "cpu"
    dtype = torch.bfloat16 if args.dtype == "bfloat16" else torch.float16
    autocast_ctx = nullcontext() if device_type == "cpu" else torch.cuda.amp.autocast(dtype=dtype)
    
    # 4. 初始化模型 (加載預訓練權重)
    model, tokenizer = init_model(lm_config, args.from_weight, device=args.device)
    
    # 5. 凍結主幹，僅訓練 DDE
    if isinstance(model, DistributedDataParallel):
        model.module.freeze_backbone()
    else:
        model.freeze_backbone()
    
    Logger("Backbone frozen. Training DDE module only.")
    
    # 6. 數據與優化器
    train_ds = DDEDataset(args.data_path, tokenizer, max_length=args.max_seq_len)
    train_sampler = DistributedSampler(train_ds) if dist.is_initialized() else None
    scaler = torch.cuda.amp.GradScaler(enabled=(args.dtype == 'float16'))
    
    # 僅將需要梯度的參數放入優化器
    trainable_params = [p for p in model.parameters() if p.requires_grad]
    optimizer = optim.AdamW(trainable_params, lr=args.learning_rate, weight_decay=0.01)
    
    # 7. wandb
    wandb = None
    if args.use_wandb and is_main_process():
        import wandb
        wandb_run_name = f"DDE-v1-Layer{args.dde_layer}-LR{args.learning_rate}"
        wandb.init(project=args.wandb_project, name=wandb_run_name)
    
    # 8. 分布式包裝
    if dist.is_initialized():
        model._ddp_params_and_buffers_to_ignore = {"freqs_cos", "freqs_sin", "model.dde_module.memory_table"}
        model = DistributedDataParallel(model, device_ids=[local_rank])
    
    # 9. 開始訓練
    iters = len(train_ds) // args.batch_size
    for epoch in range(args.epochs):
        if train_sampler: train_sampler.set_epoch(epoch)
        setup_seed(42 + epoch)
        indices = torch.randperm(len(train_ds)).tolist()
        batch_sampler = SkipBatchSampler(train_sampler or indices, args.batch_size, 0)
        loader = DataLoader(train_ds, batch_sampler=batch_sampler, num_workers=args.num_workers, pin_memory=True)
        train_epoch(epoch, loader, iters, 0, wandb)
    
    if dist.is_initialized(): dist.destroy_process_group()
