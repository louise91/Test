"""
video_processor.py — 影片分析核心
功能：
  1. ffmpeg 抽關鍵幀（圖片）
  2. ffmpeg 提取音軌
  3. Groq Whisper API 轉字幕
  4. 整合結果供 AI 分析
"""
import os, subprocess, base64, json, time, requests, tempfile, shutil
from pathlib import Path

# ── 設定 ──
MAX_VIDEO_MB   = 50           # 最大上傳大小
MAX_FRAMES     = 8            # 最多抽幾幀
FRAME_QUALITY  = 2            # ffmpeg -q:v 品質（1=最好，5=最差）
AUDIO_BITRATE  = "64k"        # 音訊壓縮率（Whisper 不需要高品質）
WHISPER_MODEL  = "whisper-large-v3-turbo"
GROQ_API_URL   = "https://api.groq.com/openai/v1"

SUPPORTED_EXT = {".mp4",".mov",".avi",".mkv",".webm",".flv",".wmv",".m4v",".3gp"}

def get_api_key():
    try:
        from config import GROQ_API_KEY
        return GROQ_API_KEY
    except ImportError:
        return os.environ.get("GROQ_API_KEY","")

# ══════════════════════════════════════
# ffmpeg 檢查
# ══════════════════════════════════════
def check_ffmpeg():
    """確認 ffmpeg 是否可用"""
    try:
        r = subprocess.run(["ffmpeg","-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

def get_video_info(video_path):
    """取得影片基本資訊"""
    try:
        cmd = [
            "ffprobe","-v","quiet","-print_format","json",
            "-show_streams","-show_format", str(video_path)
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
        fmt  = data.get("format",{})
        duration = float(fmt.get("duration",0))
        size_mb  = int(fmt.get("size",0)) / 1024 / 1024
        # 找影片和音訊流
        has_video = any(s.get("codec_type")=="video" for s in data.get("streams",[]))
        has_audio = any(s.get("codec_type")=="audio" for s in data.get("streams",[]))
        return {
            "duration": duration,
            "size_mb": round(size_mb, 2),
            "has_video": has_video,
            "has_audio": has_audio,
            "format": fmt.get("format_name","unknown"),
        }
    except Exception as e:
        return {"duration":0,"size_mb":0,"has_video":True,"has_audio":True,"error":str(e)}

# ══════════════════════════════════════
# 抽幀
# ══════════════════════════════════════
def extract_frames(video_path, output_dir, n_frames=MAX_FRAMES):
    """
    均勻抽取 n_frames 張關鍵幀，回傳圖片路徑列表
    """
    info = get_video_info(video_path)
    duration = info.get("duration", 0)
    if duration <= 0:
        duration = 60  # fallback

    # 計算抽幀間隔
    interval = max(1.0, duration / (n_frames + 1))
    timestamps = [interval * (i + 0.5) for i in range(n_frames)]
    timestamps = [t for t in timestamps if t < duration]

    frame_paths = []
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(output_dir, f"frame_{i:03d}.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(ts),
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", str(FRAME_QUALITY),
            "-vf", "scale=720:-2",   # 寬度限制 720px，保持比例
            out_path
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.exists(out_path):
                frame_paths.append((ts, out_path))
        except Exception:
            pass

    return frame_paths  # [(timestamp, path), ...]

def frames_to_base64(frame_paths):
    """將幀圖片轉為 base64"""
    result = []
    for ts, path in frame_paths:
        try:
            with open(path,"rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            result.append({"timestamp": round(ts,1), "b64": b64})
        except Exception:
            pass
    return result

# ══════════════════════════════════════
# 音訊提取 + Whisper
# ══════════════════════════════════════
def extract_audio(video_path, output_dir):
    """提取音軌為 mp3"""
    audio_path = os.path.join(output_dir, "audio.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",                     # 不要影像
        "-ar", "16000",            # Whisper 最佳取樣率
        "-ac", "1",                # 單聲道
        "-ab", AUDIO_BITRATE,
        "-f", "mp3",
        audio_path
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.exists(audio_path):
            return audio_path
    except Exception:
        pass
    return None

def transcribe_audio(audio_path, language=None):
    """
    呼叫 Groq Whisper API 轉字幕
    回傳：{"text":..., "segments":[{"start":0,"end":5,"text":"..."},...]}
    """
    api_key = get_api_key()
    if not api_key:
        return {"text":"（無 API Key，無法轉錄）","segments":[]}

    # 檔案大小限制：Groq Whisper 最大 25MB
    size_mb = os.path.getsize(audio_path) / 1024 / 1024
    if size_mb > 24:
        return {"text":"（音訊檔案過大，無法轉錄）","segments":[]}

    headers = {"Authorization": f"Bearer {api_key}"}
    data = {
        "model": WHISPER_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if language:
        data["language"] = language

    try:
        with open(audio_path,"rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
            r = requests.post(
                f"{GROQ_API_URL}/audio/transcriptions",
                headers=headers, data=data, files=files, timeout=120
            )
        if r.status_code == 200:
            result = r.json()
            return {
                "text": result.get("text",""),
                "segments": result.get("segments",[]),
                "language": result.get("language",""),
            }
        else:
            return {"text":f"（Whisper 錯誤 {r.status_code}）","segments":[]}
    except Exception as e:
        return {"text":f"（轉錄失敗：{e}）","segments":[]}

def format_transcript(transcript):
    """把字幕格式化成易讀文字"""
    segs = transcript.get("segments",[])
    if segs:
        lines = []
        for seg in segs:
            start = int(seg.get("start",0))
            mm, ss = divmod(start, 60)
            lines.append(f"[{mm:02d}:{ss:02d}] {seg.get('text','').strip()}")
        return "\n".join(lines)
    return transcript.get("text","")

# ══════════════════════════════════════
# 主入口：完整影片分析
# ══════════════════════════════════════
def process_video(video_path, n_frames=MAX_FRAMES, language=None):
    """
    完整處理流程，回傳：
    {
        "frames": [{"timestamp":1.5, "b64":"..."},...],
        "transcript": {"text":"...", "segments":[...]},
        "transcript_fmt": "格式化字幕",
        "info": {"duration":120, "has_audio":True,...},
        "has_audio": True,
        "error": None,
    }
    """
    if not check_ffmpeg():
        return {"error": "伺服器未安裝 ffmpeg，無法處理影片"}

    ext = Path(video_path).suffix.lower()
    if ext not in SUPPORTED_EXT:
        return {"error": f"不支援的影片格式：{ext}"}

    size_mb = os.path.getsize(video_path) / 1024 / 1024
    if size_mb > MAX_VIDEO_MB:
        return {"error": f"影片過大（{size_mb:.1f}MB），上限 {MAX_VIDEO_MB}MB"}

    tmp_dir = tempfile.mkdtemp(prefix="video_proc_")
    try:
        info = get_video_info(video_path)
        result = {"info": info, "error": None}

        # 抽幀
        if info.get("has_video", True):
            frame_paths = extract_frames(video_path, tmp_dir, n_frames)
            result["frames"] = frames_to_base64(frame_paths)
        else:
            result["frames"] = []

        # 音訊轉錄
        if info.get("has_audio", True):
            audio_path = extract_audio(video_path, tmp_dir)
            if audio_path:
                transcript = transcribe_audio(audio_path, language=language)
                result["transcript"] = transcript
                result["transcript_fmt"] = format_transcript(transcript)
                result["has_audio"] = True
            else:
                result["transcript"] = {"text":"（提取音訊失敗）","segments":[]}
                result["transcript_fmt"] = ""
                result["has_audio"] = False
        else:
            result["transcript"] = {"text":"（此影片無音訊）","segments":[]}
            result["transcript_fmt"] = ""
            result["has_audio"] = False

        return result

    except Exception as e:
        return {"error": str(e)}
    finally:
        # 清理暫存
        shutil.rmtree(tmp_dir, ignore_errors=True)

def build_video_prompt(processed, user_question="請分析這段影片"):
    """
    組合給 AI 的 prompt：
    - 字幕文字段落
    - 每幀時間戳說明
    - 用戶問題
    """
    lines = [user_question, ""]
    info = processed.get("info",{})
    duration = info.get("duration",0)
    mm, ss = divmod(int(duration), 60)
    lines.append(f"【影片資訊】時長 {mm:02d}:{ss:02d}，格式：{info.get('format','')}")
    lines.append("")

    # 字幕
    txt = processed.get("transcript_fmt","") or processed.get("transcript",{}).get("text","")
    if txt and "失敗" not in txt and "錯誤" not in txt:
        lines.append("【語音轉錄字幕】")
        lines.append(txt[:4000])  # 限制字數
        lines.append("")

    # 幀說明
    frames = processed.get("frames",[])
    if frames:
        lines.append(f"【畫面截圖】共 {len(frames)} 幀，時間點：" +
                     "、".join(f"{f['timestamp']}s" for f in frames))

    return "\n".join(lines)