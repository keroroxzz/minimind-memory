import json
import re
import os

def purify_content(text):
    # 移除常見的聊天開頭廢話
    patterns = [
        r'^當然記得，?', r'^沒問題，?', r'^你可以這樣說：', r'^你可以這樣描述：',
        r'^好的，?', r'^事實上，?', r'^根據最新的資料，?', r'^確實如此，?',
        r'^我記得，?', r'^是的，?', r'^當然，?', r'^你說得對，?',
        r'^根據你提供的來源，?', r'^您可以這樣描述：', r'^這是一個科學現象。',
        r'^這是一個很合理的擔心。'
    ]
    
    purified = text
    for p in patterns:
        purified = re.sub(p, '', purified).strip()
    
    # 移除引號
    purified = purified.strip('「」"\'')
    
    # 如果淨化後太短，保留原樣以防誤刪
    return purified if len(purified) > 0 else text

def process_file(input_path, output_path):
    print(f"🧹 Purifying {input_path}...")
    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    for sample in data:
        dialogue = sample['dialogue']
        # 只針對最後一輪 Assistant 的答案進行脫水
        if dialogue[-1]['role'] == 'assistant':
            old_ans = dialogue[-1]['content']
            new_ans = purify_content(old_ans)
            dialogue[-1]['content'] = new_ans
            
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    print(f"✨ Done! Purified data saved to {output_path}")

if __name__ == "__main__":
    # 處理你的測試集
    if os.path.exists('dataset/dde-v1-s.jsonl'):
        process_file('dataset/dde-v1-s.jsonl', 'dataset/dde-v1-s-pure.jsonl')
    # 處理你的訓練集
    if os.path.exists('dataset/dde-v1.jsonl'):
        process_file('dataset/dde-v1.jsonl', 'dataset/dde-v1-pure.jsonl')
