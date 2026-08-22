"""
Dance SaaS — 曲をアップするとダンス動画が自動生成される Web アプリ（骨格）

フロー:
  1. スマホのフォームから曲(音声)をアップロード
  2. Seedance (Evolink API) にダンス動画の生成ジョブを投げる
  3. task_id を返し、ブラウザがステータスをポーリング
  4. 完成したら生成動画(無音)をダウンロードし、アップした曲を ffmpeg で合成
  5. 完成 MP4 をダウンロードできる
"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import requests
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

# ----------------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# MizuiSound の config.json を再利用（evolink_video_key を使う）
CONFIG_PATH = Path.home() / "Desktop" / "MizuiSound" / "config.json"

ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "ogg", "flac"}
ALLOWED_IMAGE = {"png", "jpg", "jpeg"}
ALLOWED_VIDEO = {"mp4", "mov", "webm"}
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200MB（参照動画のMP4に対応）

EVOLINK_BASE = "https://api.evolink.ai/v1"

# Seedance の動画生成に使うデフォルト（config から上書き可能）
DEFAULT_MODEL = "seedance-2.0-mini-image-to-video"
DEFAULT_PROMPT = (
    "A person dancing energetically to the music, full body, smooth dynamic "
    "motion, rhythmic choreography, studio lighting, cinematic"
)
# reference-to-video 用のプロンプト（@image1=キャラ画像 / @video1=参照ダンス動画）
REFERENCE_PROMPT = (
    "@image1 is the character. Make the character dance following the motion "
    "style of @video1. Full body visible, 9:16 vertical, studio lighting."
)


def load_config() -> dict:
    """MizuiSound/config.json を読み込む（デモ用アセットURL等のデフォルト値のみ）。

    ※ API キーはここからは読まない。各ユーザーが自分の EvoLink API キーを
       フォームで入力する方式に変更した。
    """
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


CONFIG = load_config()

# task_id -> メタ情報 の簡易ストア（骨格なのでメモリ上。後で DB へ）
# 各ジョブには投入したユーザーの api_key も保持し、status/finalize でも使う。
JOBS: dict[str, dict] = {}


# ----------------------------------------------------------------------------
# Flask
# ----------------------------------------------------------------------------
# static/ を /static/ で配信（mascot.png などをブラウザから参照できるようにする）
app = Flask(__name__, static_folder="static", static_url_path="/static")
# Render 等のリバースプロキシ配下で X-Forwarded-Proto/Host を尊重し、
# url_for(_external=True) が正しい https:// の外部URLを生成できるようにする。
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_asset(file, allowed: set[str]) -> str | None:
    """アップロードされた素材（画像 / 参照動画）を保存し、外部からアクセスできる
    URL を返す。ファイルが無い / 拡張子が不正な場合は None。

    ※ Seedance 側のサーバーが取得できる URL である必要があるため、公開ホストに
       デプロイして使うことを想定（localhost では EvoLink から到達できない）。
    """
    if file is None or not file.filename:
        return None
    if "." not in file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in allowed:
        return None
    stored_name = f"asset_{uuid.uuid4().hex}.{ext}"
    file.save(UPLOAD_DIR / stored_name)
    return url_for("uploaded_file", name=stored_name, _external=True)


def create_seedance_job(
    prompt: str,
    model: str,
    api_key: str,
    mode: str = "image-to-video",
    image_url: str | None = None,
    ref_url: str | None = None,
    resolution: str = "480p",
) -> dict:
    """Seedance にダンス動画生成ジョブを作成し、レスポンス JSON を返す。

    api_key:     ユーザーがフォームで入力した EvoLink API キー。
    mode:        生成モード（image-to-video / reference-to-video）。
    image_url:   image-to-video 用のキャラクター画像 URL（アップロード物）。
    ref_url:     reference-to-video 用の参照ダンス動画 URL（アップロード物）。
    resolution:  出力解像度（720p / 1080p）。
    """
    if not api_key:
        raise RuntimeError("EvoLink API キーが入力されていません")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "duration": int(CONFIG.get("seedance_duration", 8)),
        "quality": resolution or "480p",
        "aspect_ratio": "9:16",  # スマホ縦向き
        "generate_audio": False,  # 曲は後工程で合成する
    }

    # 選択されたモードに応じて素材（アップロード優先→config）を排他的に付与する。
    # アップロードも config も無い場合は、対応する payload を追加しない。
    if mode == "image-to-video":
        img = image_url or CONFIG.get("seedance_image_url")
        if img:
            payload["image"] = img
    elif mode == "reference-to-video":
        ref = ref_url or CONFIG.get("seedance_ref_url")
        if ref:
            payload["reference_video"] = ref

    print(f"[EvoLink payload] image={payload.get('image')}, reference_video={payload.get('reference_video')}, model={payload.get('model')}, prompt={payload.get('prompt')[:50]}", flush=True)
    resp = requests.post(
        f"{EVOLINK_BASE}/videos/generations",
        headers=headers,
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def get_seedance_task(task_id: str, api_key: str) -> dict:
    """Seedance のタスク状態を取得する。"""
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.get(
        f"{EVOLINK_BASE}/tasks/{task_id}",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def download_file(url: str, dest: Path) -> Path:
    """URL のファイルをストリーミングで dest に保存する。"""
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return dest


def mux_audio_video(video_path: Path, audio_path: Path, out_path: Path) -> Path:
    """ffmpeg で動画(無音)にアップした曲を合成する。長さは短い方に合わせる。"""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),   # 0: 生成動画(映像)
        "-i", str(audio_path),   # 1: アップした曲(音声)
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",          # 映像は再エンコードせずコピー
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",             # 短い方の長さで打ち切り
        "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 合成に失敗しました: {proc.stderr[-800:]}")
    return out_path


# ----------------------------------------------------------------------------
# ルート
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return jsonify({"error": "EvoLink API キーを入力してください"}), 400

    # 曲アップロードは任意。あれば保存して後工程で合成する。
    stored_name = None
    file = request.files.get("song")
    if file and file.filename:
        if not allowed_file(file.filename):
            return jsonify({"error": "対応していない音声形式です"}), 400
        ext = file.filename.rsplit(".", 1)[1].lower()
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        file.save(UPLOAD_DIR / stored_name)

    # ユーザーが入力した値（image-to-video 時 / フォールバック時に使う）
    user_prompt = request.form.get("prompt", "").strip()
    user_resolution = request.form.get("resolution", "").strip() or "480p"

    # 素材を保存して URL 化。
    #   画像 … 必須（image-to-video のベース）
    #   参照動画 … 任意。あれば reference-to-video、なければ image-to-video に自動切替。
    image_url = save_asset(request.files.get("image"), ALLOWED_IMAGE)
    ref_url = save_asset(request.files.get("reference"), ALLOWED_VIDEO)

    if not image_url and not CONFIG.get("seedance_image_url"):
        return jsonify(
            {"error": "キャラクター画像（PNG / JPG）をアップロードしてください"}
        ), 400

    # 参照動画の有無でモデル・プロンプト・解像度を自動切り替えする。
    if ref_url:
        # 参照動画あり: reference-to-video（720p 強制、専用プロンプト）
        mode = "reference-to-video"
        model = "seedance-2.0-reference-to-video"
        prompt = REFERENCE_PROMPT
        resolution = "720p"
    else:
        # 参照動画なし: mini image-to-video（解像度はユーザー選択のまま）
        mode = "image-to-video"
        model = "seedance-2.0-mini-image-to-video"
        prompt = user_prompt or DEFAULT_PROMPT
        resolution = user_resolution

    # Seedance にジョブ投入（ユーザー自身の API キーを使用）
    try:
        result = create_seedance_job(
            prompt, model, api_key, mode, image_url, ref_url, resolution
        )
    except Exception as first_err:  # noqa: BLE001
        # reference-to-video で失敗した場合は、参照動画を外して
        # image-to-video にフォールバックして再試行する。
        can_fallback = ref_url and (
            image_url or CONFIG.get("seedance_image_url")
        )
        if can_fallback:
            # reference-to-video → image-to-video に切り替えるので、
            # プロンプト・解像度も image-to-video 用に戻す。
            mode = "image-to-video"
            model = "seedance-2.0-mini-image-to-video"
            prompt = user_prompt or DEFAULT_PROMPT
            resolution = user_resolution
            ref_url = None
            try:
                result = create_seedance_job(
                    prompt, model, api_key, mode, image_url, ref_url, resolution
                )
            except requests.HTTPError as e:
                body = e.response.text if e.response is not None else str(e)
                return jsonify(
                    {"error": f"Seedance へのリクエストに失敗しました: {body}"}
                ), 502
            except Exception as e:  # noqa: BLE001
                return jsonify({"error": str(e)}), 500
        elif isinstance(first_err, requests.HTTPError):
            body = (
                first_err.response.text
                if first_err.response is not None
                else str(first_err)
            )
            return jsonify(
                {"error": f"Seedance へのリクエストに失敗しました: {body}"}
            ), 502
        else:
            return jsonify({"error": str(first_err)}), 500

    task_id = result.get("id") or result.get("task_id")
    JOBS[task_id] = {
        "task_id": task_id,
        "song": stored_name,  # 曲は任意。無ければ None
        "original_name": secure_filename(file.filename) if (file and file.filename) else None,
        "prompt": prompt,
        "model": model,
        "api_key": api_key,  # status/finalize でも同じユーザーのキーを使う
        "created": time.time(),
    }

    return jsonify({"task_id": task_id, "status": result.get("status", "pending")})


@app.route("/status/<task_id>")
def status(task_id):
    job = JOBS.get(task_id)
    if not job:
        return jsonify({"error": "unknown task_id（サーバー再起動などで消えた可能性）"}), 404

    try:
        data = get_seedance_task(task_id, job["api_key"])
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502

    results = data.get("results") or []
    return jsonify(
        {
            "task_id": task_id,
            "status": data.get("status"),
            "progress": data.get("progress"),
            "video_url": results[0] if results else None,
            "error": data.get("error"),
        }
    )


@app.route("/finalize/<task_id>", methods=["POST"])
def finalize(task_id):
    """生成完了した動画をDLし、アップした曲を合成して完成MP4を作る。冪等。"""
    job = JOBS.get(task_id)
    if not job:
        return jsonify({"error": "unknown task_id"}), 404

    final_name = f"final_{task_id}.mp4"
    final_path = UPLOAD_DIR / final_name

    # 既に合成済みならそのまま返す（冪等）
    if final_path.exists():
        return jsonify({"download_url": url_for("download", task_id=task_id)})

    # Seedance から結果動画の URL を取得
    try:
        data = get_seedance_task(task_id, job["api_key"])
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"状態取得に失敗: {e}"}), 502

    if data.get("status") != "completed":
        return jsonify({"error": "まだ生成が完了していません"}), 409
    results = data.get("results") or []
    if not results:
        return jsonify({"error": "生成結果の動画がありません"}), 502

    # 生成動画をDL → 曲があれば合成、無ければそのまま完成扱い
    try:
        if job.get("song"):
            raw_video = UPLOAD_DIR / f"raw_{task_id}.mp4"
            download_file(results[0], raw_video)
            song_path = UPLOAD_DIR / job["song"]
            mux_audio_video(raw_video, song_path, final_path)
        else:
            # 曲が無い場合は生成動画をそのまま完成MP4として保存
            download_file(results[0], final_path)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    finally:
        # 中間ファイルは掃除
        raw_video = UPLOAD_DIR / f"raw_{task_id}.mp4"
        if raw_video.exists():
            raw_video.unlink()

    job["final"] = final_name
    return jsonify({"download_url": url_for("download", task_id=task_id)})


@app.route("/download/<task_id>")
def download(task_id):
    """完成した MP4 をダウンロードさせる。"""
    final_path = UPLOAD_DIR / f"final_{task_id}.mp4"
    if not final_path.exists():
        return jsonify({"error": "まだ完成していません"}), 404
    return send_from_directory(
        UPLOAD_DIR,
        final_path.name,
        as_attachment=True,
        download_name=f"dance_{task_id}.mp4",
    )


@app.route("/uploads/<path:name>")
def uploaded_file(name):
    return send_from_directory(UPLOAD_DIR, name)


if __name__ == "__main__":
    # スマホから同じ Wi-Fi で開けるよう 0.0.0.0 で待受
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=False)
