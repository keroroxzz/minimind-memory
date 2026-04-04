import json
import random
from transformers import AutoTokenizer
from dataset.lm_dataset import DDEDataset

tokenizer = AutoTokenizer.from_pretrained("model")
train_ds = DDEDataset("dataset/dde-v1.jsonl", tokenizer, max_length=512)

id = random.randint(0, len(train_ds))
print(tokenizer.decode(train_ds[id][0]))

text = "这是一个测试，看看分词器是否能正确处理中文。"

# 编码：将文本转换为 token IDs
encoded_ids = tokenizer.encode(text)
print("编码结果 (Token IDs):", encoded_ids)
# 解码：将 token IDs 转换回文本
decoded_text = tokenizer.decode(encoded_ids)
print("解码结果 (文本):", decoded_text)


text = "這是一個測試，看看分詞器是否能夠正確處理中文。"

# 编码：将文本转换为 token IDs
encoded_ids = tokenizer.encode(text)
print("编码结果 (Token IDs):", encoded_ids)
# 解码：将 token IDs 转换回文本
decoded_text = tokenizer.decode(encoded_ids)
print("解码结果 (文本):", decoded_text)