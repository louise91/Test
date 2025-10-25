import os
import requests
from dotenv import load_dotenv
from datetime import datetime
import re  # 加regex砍假User
import random  # 加隨機變體

# 載入環境變數
load_dotenv()
API_KEY = os.environ.get("TWCC_API_KEY")
API_URL = "https://api-ams.twcc.ai/api/models/completions"
MODEL = "llama3.3-ffm-70b-32k-chat"
#FFM-Mixtral-8x7B
#meta-llama3.3-70b-inst-32k

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# 對話紀錄
conversation_history = []
MAX_HISTORY = 3  # 調小少拉舊
MAX_MODEL_TOKENS = 32000
LOG_FILE = "log.txt"
LOG_LIMIT = 1 * 1024 * 1024

# 簡版記憶庫（dict存關鍵設定）
memory = {}  # e.g., {"主題": "超自然"}

def estimate_tokens(text: str) -> int:
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    english_words = len([w for w in text.split() if w.isascii()])
    return int(chinese_chars * 2 + english_words * 1.3)

def rotate_log():
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_LIMIT:
        base, ext = os.path.splitext(LOG_FILE)
        i = 1
        while os.path.exists(f"{base}_{i}{ext}"):
            i += 1
        os.rename(LOG_FILE, f"{base}_{i}{ext}")
        print(f"🔄 Log 已滿，已切換至 {base}_{i}{ext}")

def update_memory(user_input, reply):
    """簡版記憶：解析關鍵設定存dict"""
    if '主題' in user_input:
        memory['主題'] = user_input.replace('主題', '').strip()
    if '無設定' in reply:
        pass  # 不動
    print(f"記憶更新: {memory}")

def get_memory_summary():
    """拉記憶進prompt"""
    if memory:
        return f"當前記憶: {str(memory)}"
    return "無記憶"

def clean_reply(reply: str, user_input: str) -> str:
    """後處理：去重複、限長、砍循環 + 強制清假User + 隨機變體（修列表+亂序）"""
    # 砍註解和假User
    reply = re.sub(r'#.*', '', reply)  # 砍#註解
    reply = re.sub(r'User:\s*[\w\W]*?(\n|$)', '', reply)  # 砍User:後所有
    reply = re.sub(r'準備好了|User:\s*準備好了', '', reply)  # 砍假輸入變體
    reply = re.sub(r'請繼續對話：.*', '', reply, flags=re.DOTALL)  # 砍prompt殘留
    lines = reply.split('\n')
    unique_lines = []
    seen_phrases = set()
    for line in lines:
        line = line.strip()
        if line and line not in seen_phrases:
            unique_lines.append(line)
            seen_phrases.add(line)
        if len(unique_lines) > 2:  # 限2行超簡
            break
    cleaned = ' '.join(unique_lines)
    if len(cleaned) > 80:
        cleaned = cleaned[:80] + '...'
    # 砍循環短語
    for phrase in ['現在，請問', '你可以簡化成：', '重複', '無設定', '上一步設定：', '總結上一步']:
        cleaned = cleaned.replace(phrase, '').strip()
    # 隨機變體問句（防重複，每次亂序）
    options = ['超自然', '魔法', '科技']
    random.shuffle(options)  # 亂序
    options_str = '、'.join(options)
    variants = [
        f"你好，主人！世界主題是什麼？從{options_str}選。",
        f"你好，主人！主題選項：{options_str}？",
        f"你好，主人！讓我們建世界，從{options_str}主題開始？"
    ]
    # fallback: 隨機挑變體
    if len(cleaned) < 10 or '?' not in cleaned:
        if '你好' in user_input.lower() or '準備' in user_input:
            cleaned = random.choice(variants)
    print(f"Debug options: {options_str}")  # 調試順序
    return cleaned

def ask_llama(user_input):
    global conversation_history, memory

    if user_input.lower() == "clear":
        conversation_history = []
        memory = {}
        print("歷史已清！")
        return "歷史已清除，重啟對話。", 0, 0

    conversation_history.append(f"User: {user_input}")
    trimmed_history = "\n".join(conversation_history[-MAX_HISTORY*2:])

    memory_summary = get_memory_summary()

    prompt = f"""
絕對優先：如果輸入是「你好」或「準備好了」，唯一回應「你好，主人！世界主題是什麼？從超自然、魔法或科技選。」，無其他文字、無總結、無問題、無假輸入、無註解。
你是一位大世界創建者，我是你的主人，請用繁體中文回應。只用我的輸入來繼續世界構築，不要自動添加假User行、假對話、問題列表或任何重複句子。每次回應唯一版本：1句總結上一步設定（無則說「無設定」），然後只問1個全新問題推進世界。總長不超50字。無自省、無重複、無假輸入、無註解。問句隨機變體：主題？/選項？/基調？。

記憶摘要：{memory_summary}

以下是目前的對話紀錄：
{trimmed_history}

請繼續對話：
"""

    input_tokens = estimate_tokens(prompt)
    max_tokens = min(120, MAX_MODEL_TOKENS - input_tokens - 500)  # 調120少黏
    if max_tokens < 80:  
        max_tokens = 80

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.2,  # 超低溫，絕對遵守
        "top_p": 0.9,  # 加多樣
        "stop": ["現在，請問", "你可以簡化成：", "無設定", "請繼續對話：", "#"],  # 砍總結循環+殘留+註解
        "repetition_penalty": 1.2  # 懲罰重複token
    }

    try:
        r = requests.post(API_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        result = r.json()
        raw_reply = result["choices"][0]["text"].strip()

        # 後處理
        reply = clean_reply(raw_reply, user_input)
        update_memory(user_input, reply)  # 更新記憶
        print(f"Debug: Raw={raw_reply[:50]}... -> Cleaned={reply}")

        conversation_history.append(f"Assistant: {reply}")

        rotate_log()

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n")
            f.write(f"User: {user_input}\n")
            f.write(f"Raw Assistant: {raw_reply[:100]}...\n")
            f.write(f"Cleaned Assistant: {reply}\n")

        return reply, max_tokens, input_tokens

    except requests.exceptions.RequestException as e:
        return f"錯誤: {e}", 0, 0
    except Exception as e:
        return f"解析回應時發生錯誤: {e}", 0, 0

# 測試用
if __name__ == "__main__":
    while True:
        user_in = input("你: ")
        if user_in.lower() in ["exit", "quit", "bye"]:
            print("對話結束")
            break
        ans, used_max, input_tokens = ask_llama(user_in)
        print(f"助理 ({used_max} tokens, prompt≈{input_tokens}): {ans}")