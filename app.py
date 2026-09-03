"""Dance Studio — Seedance（EvoLink API）ダンス動画生成Webアプリ。

参照動画ありは720p・無音、参照動画なしは選択解像度で生成し、曲があれば
ffmpegで完成動画へ合成する。認証・Stripe月額課金・利用者単位のジョブ保護を行う。
"""

import json
import os
import secrets
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
import stripe
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# ----------------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# MizuiSound の config.json を再利用（evolink_video_key を使う）
CONFIG_PATH = Path.home() / "Desktop" / "MizuiSound" / "config.json"

ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a", "aac"}
ALLOWED_IMAGE = {"png", "jpg", "jpeg"}
ALLOWED_VIDEO = {"mp4"}
MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200MB（参照動画のMP4に対応）
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_REFERENCE_SECONDS = 15.0
VALID_RESOLUTIONS = {"480p", "720p", "1080p"}

EVOLINK_BASE = "https://api.evolink.ai/v1"

# Stripe（サブスク課金）。キーは環境変数から読み込む。
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
PRICE_ID = os.environ.get("STRIPE_PRICE_ID")

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

# ログイン試行制限（ブルートフォース対策）。メモリ上のみ（永続化しない）。
# email -> {"count": 失敗回数, "last": 最終失敗時刻(epoch)}
LOGIN_ATTEMPTS: dict[str, dict] = {}
MAX_LOGIN_ATTEMPTS = 5          # この回数連続で失敗すると
LOGIN_LOCKOUT_SECONDS = 15 * 60  # この秒数だけロックする（15分）


# ----------------------------------------------------------------------------
# Flask
# ----------------------------------------------------------------------------
# static/ を /static/ で配信（mascot.png などをブラウザから参照できるようにする）
app = Flask(__name__, static_folder="static", static_url_path="/static")
# Render 等のリバースプロキシ配下で X-Forwarded-Proto/Host を尊重し、
# url_for(_external=True) が正しい https:// の外部URLを生成できるようにする。
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


@app.errorhandler(413)
def request_too_large(_error):
    if request.path == "/upload":
        return api_error(
            "アップロード全体の上限は200MBです。ファイルサイズを小さくしてください。",
            "DS-FILE-413",
            413,
        )
    return "Request entity too large", 413

# セッション署名用のシークレット（本番は環境変数で必ず上書きする）
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SESSION_COOKIE_SECURE", "false"
).lower() in {"1", "true", "yes"}

# ----------------------------------------------------------------------------
# データベース（SQLite）& 認証
# ----------------------------------------------------------------------------
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + str(BASE_DIR / "users.db")
).replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(UserMixin, db.Model):
    """アプリのユーザー。subscription_status で課金状態を管理する。

    subscription_status:
      free     … 登録直後（アップロード不可 / 402）
      active   … 課金中（アップロード可）
      canceled … 解約済み（アップロード不可）
    """

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    subscription_status = db.Column(
        db.String(20), nullable=False, default="free"
    )
    # Stripe の顧客ID。Checkout 完了時に保存し、subscription.deleted で照合する。
    stripe_customer_id = db.Column(db.String(255), unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    """未ログイン時の挙動。API 用の /upload は 401 JSON、それ以外は
    ログインページにリダイレクトする。"""
    if request.path.startswith(("/upload", "/status/", "/finalize/", "/download/")):
        return api_error("ログインが必要です", "DS-AUTH-001", 401)
    return redirect(url_for("login"))


with app.app_context():
    db.create_all()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def api_error(message: str, code: str, status: int):
    """利用者向けの安全なエラー形式を統一する。"""
    return jsonify({"error": message, "error_code": code}), status


def upload_size(file) -> int:
    """アップロードを消費せずにバイト数を調べ、読み取り位置を戻す。"""
    current = file.stream.tell()
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(current)
    return size


def validate_upload(file, allowed: set[str], max_bytes: int) -> str | None:
    """未選択なら None、問題があれば利用者向けメッセージを返す。"""
    if file is None or not file.filename:
        return None
    if "." not in file.filename:
        return "ファイルの拡張子を確認してください"
    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in allowed:
        return f"対応形式は {', '.join(sorted(allowed))} です"
    if upload_size(file) > max_bytes:
        return f"ファイルサイズが上限（{max_bytes // (1024 * 1024)}MB）を超えています"
    return None


def save_asset(file, allowed: set[str]) -> tuple[str | None, Path | None]:
    """アップロードされた素材（画像 / 参照動画）を保存し、外部からアクセスできる
    URL を返す。ファイルが無い / 拡張子が不正な場合は None。

    ※ Seedance 側のサーバーが取得できる URL である必要があるため、公開ホストに
       デプロイして使うことを想定（localhost では EvoLink から到達できない）。
    """
    if file is None or not file.filename:
        return None, None
    if "." not in file.filename:
        return None, None
    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in allowed:
        return None, None
    stored_name = f"asset_{uuid.uuid4().hex}.{ext}"
    stored_path = UPLOAD_DIR / stored_name
    file.save(stored_path)
    return url_for("uploaded_file", name=stored_name, _external=True), stored_path


def probe_video_duration(path: Path) -> float:
    """ffprobe で動画尺を取得する。解析不能な動画は不正ファイルとして扱う。"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise ValueError("参照動画を解析できませんでした")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise ValueError("参照動画の長さを確認できませんでした") from exc


def delete_paths(*paths: Path | None) -> None:
    for path in paths:
        if path and path.exists():
            path.unlink()


def cleanup_job_inputs(job: dict, keep_song: bool = False) -> None:
    """生成完了・失敗後に一時素材とAPIキーを破棄する。"""
    for value in job.get("asset_paths", []):
        delete_paths(Path(value))
    if not keep_song and job.get("song"):
        delete_paths(UPLOAD_DIR / job["song"])
    job["asset_paths"] = []
    job["api_key"] = None


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
    resolution:  出力解像度（480p / 720p / 1080p）。
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
        "duration": int(CONFIG.get("seedance_duration", 10)),
        "quality": resolution or "480p",
        "aspect_ratio": "9:16",  # スマホ縦向き
        "generate_audio": False,  # 曲は後工程で合成する
    }

    # 選択されたモードに応じて素材（アップロード優先→config）を排他的に付与する。
    # アップロードも config も無い場合は、対応する payload を追加しない。
    if mode == "image-to-video":
        img = image_url or CONFIG.get("seedance_image_url")
        if img:
            payload["image_urls"] = [img]
    elif mode == "reference-to-video":
        img = image_url or CONFIG.get("seedance_image_url")
        ref = ref_url or CONFIG.get("seedance_ref_url")
        if img:
            payload["image_urls"] = [img]
        if ref:
            payload["video_urls"] = [ref]

    print(f"[EvoLink payload] image_urls={payload.get('image_urls')}, video_urls={payload.get('video_urls')}, model={payload.get('model')}, prompt={payload.get('prompt')[:50]}", flush=True)
    resp = requests.post(
        f"{EVOLINK_BASE}/videos/generations",
        headers=headers,
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        # EvoLink から返ってきた実際のエラー本文（クレジット不足 / 動画形式エラー /
        # 認証エラー等）をサーバーログに残してから例外化する。原因特定にはこの本文が要。
        print(
            f"[EvoLink error] mode={mode} model={model} "
            f"status={resp.status_code} body={resp.text[:1000]}",
            flush=True,
        )
    resp.raise_for_status()
    return resp.json()


def friendly_evolink_error(
    err: Exception, status_code: int | None = None, body: str = ""
) -> str:
    """EvoLink / 通信まわりのエラーを、意味が伝わる日本語メッセージに言い換える。

    専門的すぎる原文はそのまま出さず、ユーザーが次に取るべき行動が分かる表現にする。
    （※クレジット不足 402 は呼び出し側で個別に処理するため、ここでは扱わない）
    """
    low = (body or "").lower()

    # HTTP 応答が無いケース（タイムアウト / 接続エラー）
    if isinstance(err, requests.Timeout):
        return "EvoLinkからの応答がタイムアウトしました。時間をおいて再度お試しください。"
    if isinstance(err, requests.ConnectionError):
        return (
            "EvoLinkに接続できませんでした。ネットワーク状況を確認のうえ、"
            "時間をおいて再度お試しください。"
        )

    # HTTP ステータス別の言い換え
    if status_code == 401 or "invalid api key" in low or "unauthorized" in low:
        return (
            "EvoLink APIキーが正しくないか、無効になっています。"
            "キーを確認して、もう一度入力してください。"
        )
    if status_code == 429 or "rate limit" in low:
        return (
            "EvoLink側のリクエスト制限に達しました。"
            "少し時間をおいてから再度お試しください。"
        )
    if status_code == 400:
        if "video" in low or "format" in low or "duration" in low:
            return (
                "参照動画の形式または長さに問題がある可能性があります。"
                "15秒以内のMP4（H.264など一般的な形式）でお試しください。"
            )
        if "image" in low:
            return (
                "キャラクター画像の形式に問題がある可能性があります。"
                "PNGまたはJPGでお試しください。"
            )
        return (
            "リクエスト内容に問題があり、生成できませんでした。"
            "アップロードした画像・参照動画を見直して再度お試しください。"
        )
    if status_code is not None and status_code >= 500:
        return (
            "EvoLink側で一時的なエラーが発生しました。"
            "時間をおいて再度お試しください。"
        )

    # 判別できない外部サービスの応答はログだけに残し、画面へ生データを出さない。
    return "生成に失敗しました。時間をおいて再度お試しください。"


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
@login_required
def index():
    public_links = {
        "guide": os.environ.get("GUIDE_URL", ""),
        "terms": os.environ.get("TERMS_URL", ""),
        "privacy": os.environ.get("PRIVACY_URL", ""),
        "cancel": os.environ.get("CANCELLATION_POLICY_URL", ""),
        "support": os.environ.get("SUPPORT_URL", ""),
    }
    return render_template("index.html", public_links=public_links)


@app.route("/tokushoho")
def tokushoho():
    """特定商取引法に基づく表記（誰でも閲覧できるよう認証不要）。"""
    return render_template("tokushoho.html")


# ----------------------------------------------------------------------------
# 認証ルート
# ----------------------------------------------------------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("signup.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        return render_template(
            "signup.html", error="メールアドレスとパスワードを入力してください"
        ), 400
    if len(password) < 8:
        return render_template(
            "signup.html", error="パスワードは8文字以上で設定してください"
        ), 400
    if User.query.filter_by(email=email).first():
        return render_template(
            "signup.html", error="このメールアドレスは既に登録されています"
        ), 400

    user = User(email=email, subscription_status="free")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user)
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    # ブルートフォース対策：連続失敗が上限に達していたらロック中か判定する。
    now = time.time()
    record = LOGIN_ATTEMPTS.get(email)
    if record and record["count"] >= MAX_LOGIN_ATTEMPTS:
        if now - record["last"] < LOGIN_LOCKOUT_SECONDS:
            # まだロック期間中
            return render_template(
                "login.html",
                error="試行回数の上限に達しました。しばらくお待ちください",
            ), 429
        # ロック期間が過ぎたのでカウンタをリセットして再試行を許可する
        LOGIN_ATTEMPTS.pop(email, None)

    user = User.query.filter_by(email=email).first()
    if user is None or not user.check_password(password):
        # 失敗回数を記録（メールアドレス単位）
        rec = LOGIN_ATTEMPTS.setdefault(email, {"count": 0, "last": 0.0})
        rec["count"] += 1
        rec["last"] = now
        if rec["count"] >= MAX_LOGIN_ATTEMPTS:
            return render_template(
                "login.html",
                error="試行回数の上限に達しました。しばらくお待ちください",
            ), 429
        return render_template(
            "login.html", error="メールアドレスまたはパスワードが違います"
        ), 401

    # 成功時は試行回数をリセットする
    LOGIN_ATTEMPTS.pop(email, None)
    login_user(user)
    return redirect(url_for("index"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ----------------------------------------------------------------------------
# Stripe サブスク課金
# ----------------------------------------------------------------------------
@app.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session():
    """サブスク契約用の Stripe Checkout セッションを作成し、その URL を返す。"""
    if not stripe.api_key or not PRICE_ID:
        return api_error(
            "決済設定が完了していません。サポートへお問い合わせください。",
            "DS-PAYMENT-001",
            503,
        )
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            success_url=url_for("index", checkout="success", _external=True),
            cancel_url=url_for("index", checkout="cancel", _external=True),
            # webhook 側でユーザーを特定するために現在のユーザーIDを埋め込む
            client_reference_id=str(current_user.id),
            customer_email=current_user.email,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[Stripe checkout error] {type(e).__name__}: {e}", flush=True)
        return api_error(
            "決済ページを開けませんでした。時間をおいて再度お試しください。",
            "DS-PAYMENT-002",
            502,
        )

    return jsonify({"url": session.url})


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """Stripe からの Webhook を受け取り、課金状態を DB に反映する。"""
    if not STRIPE_WEBHOOK_SECRET:
        return api_error("Webhook設定が完了していません", "DS-PAYMENT-003", 503)
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        # 不正なペイロード or 署名検証失敗
        print(f"[Stripe webhook verification error] {type(e).__name__}: {e}", flush=True)
        return api_error("Webhook署名を確認できませんでした", "DS-PAYMENT-004", 400)

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        # 契約完了 → ユーザーを active にし、customer_id を保存する。
        user_id = getattr(obj, "client_reference_id", None)
        customer_id = getattr(obj, "customer", None)
        if user_id:
            user = db.session.get(User, int(user_id))
            if user:
                user.subscription_status = "active"
                if customer_id:
                    user.stripe_customer_id = customer_id
                db.session.commit()

    elif event_type == "customer.subscription.deleted":
        # 解約 → 顧客IDからユーザーを特定して canceled にする。
        customer_id = getattr(obj, "customer", None)
        if customer_id:
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if user:
                user.subscription_status = "canceled"
                db.session.commit()

    return jsonify({"received": True}), 200


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    if current_user.subscription_status != "active":
        return api_error(
            "有効なDance Studioサブスクリプションが必要です",
            "DS-SUBSCRIPTION-001",
            402,
        )

    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        return api_error(
            "EvoLink APIキーを入力してください", "DS-KEY-003", 400
        )

    # 素材の権利に関する同意（チェックボックス）を必須とする。
    consent = request.form.get("consent", "").strip()
    if consent not in ("1", "on", "true"):
        return api_error(
            "アップロード素材の権利に関する確認事項への同意が必要です",
            "DS-CONSENT-001",
            400,
        )

    image_file = request.files.get("image")
    ref_file = request.files.get("reference")
    song_file = request.files.get("song")

    if image_file is None or not image_file.filename:
        return api_error(
            "キャラクター画像（PNG / JPG）をアップロードしてください",
            "DS-IMAGE-001",
            400,
        )

    image_problem = validate_upload(image_file, ALLOWED_IMAGE, MAX_IMAGE_BYTES)
    if image_problem:
        return api_error(f"キャラクター画像: {image_problem}", "DS-IMAGE-002", 400)

    ref_problem = validate_upload(ref_file, ALLOWED_VIDEO, MAX_VIDEO_BYTES)
    if ref_problem:
        return api_error(f"参照動画: {ref_problem}", "DS-VIDEO-002", 400)

    song_problem = validate_upload(song_file, ALLOWED_EXTENSIONS, MAX_AUDIO_BYTES)
    if song_problem:
        return api_error(f"曲: {song_problem}", "DS-AUDIO-001", 400)

    # ユーザーが入力した値（image-to-video 時 / フォールバック時に使う）
    user_prompt = request.form.get("prompt", "").strip()
    user_resolution = request.form.get("resolution", "").strip() or "480p"
    if user_resolution not in VALID_RESOLUTIONS:
        user_resolution = "480p"

    # 素材を検査後に保存。参照動画が選択されている場合、曲は保存も合成もしない。
    image_url, image_path = save_asset(image_file, ALLOWED_IMAGE)
    ref_url, ref_path = save_asset(ref_file, ALLOWED_VIDEO)

    if ref_path:
        try:
            duration = probe_video_duration(ref_path)
        except ValueError as exc:
            delete_paths(image_path, ref_path)
            return api_error(str(exc), "DS-VIDEO-003", 400)
        except subprocess.TimeoutExpired:
            delete_paths(image_path, ref_path)
            return api_error(
                "参照動画の解析がタイムアウトしました。動画を軽くして再度お試しください。",
                "DS-VIDEO-003",
                400,
            )
        if duration > MAX_REFERENCE_SECONDS:
            delete_paths(image_path, ref_path)
            return api_error(
                f"参照動画は最大15秒です（選択された動画: {duration:.1f}秒）",
                "DS-VIDEO-004",
                400,
            )

    # 参照動画の有無でモデル・プロンプト・解像度を自動切り替えする。
    if ref_url:
        # 参照動画あり: 720p・無音。曲が同時送信されてもサーバー側で使用しない。
        mode = "reference-to-video"
        model = "seedance-2.0-reference-to-video"
        prompt = REFERENCE_PROMPT
        resolution = "720p"
        stored_name = None
    else:
        mode = "image-to-video"
        model = "seedance-2.0-mini-image-to-video"
        prompt = user_prompt or DEFAULT_PROMPT
        resolution = user_resolution
        stored_name = None
        if song_file and song_file.filename:
            ext = song_file.filename.rsplit(".", 1)[1].lower()
            stored_name = f"{uuid.uuid4().hex}.{ext}"
            song_file.save(UPLOAD_DIR / stored_name)

    # Seedance にジョブ投入（ユーザー自身の API キーを使用）
    try:
        result = create_seedance_job(
            prompt, model, api_key, mode, image_url, ref_url, resolution
        )
    except Exception as err:  # noqa: BLE001
        # 失敗理由をサーバーログに残す（原因特定用）。
        status_code = None
        body = ""
        if isinstance(err, requests.HTTPError) and err.response is not None:
            status_code = err.response.status_code
            body = err.response.text or ""
            print(
                f"[generation failed] mode={mode} status={status_code} "
                f"body={body[:1000]}",
                flush=True,
            )
        else:
            print(
                f"[generation failed] mode={mode} {type(err).__name__}: {err}",
                flush=True,
            )

        # クレジット不足（402 / insufficient_quota）は、方針上フォールバックしない。
        # ユーザーは自分の EvoLink キー・クレジットを使う設計のため、理由を伝えず
        # 別モデル（mini-image-to-video）で低品質な別物を生成するのは誠実でない。
        # クレジット追加を促す明確なメッセージを返す。
        if status_code == 402 or "insufficient_quota" in body.lower():
            delete_paths(image_path, ref_path, UPLOAD_DIR / stored_name if stored_name else None)
            credit_hint = "約150" if mode == "reference-to-video" else "約10〜"
            return api_error(
                "EvoLinkのクレジットが不足しています。EvoLinkでクレジットを追加して"
                f"から再度お試しください。（このモードの目安: {credit_hint}クレジット）",
                "DS-CREDIT-001",
                402,
            )

        delete_paths(image_path, ref_path, UPLOAD_DIR / stored_name if stored_name else None)
        return api_error(
            friendly_evolink_error(err, status_code, body),
            "DS-EVOLINK-001",
            502,
        )

    task_id = result.get("id") or result.get("task_id")
    if not task_id:
        delete_paths(image_path, ref_path, UPLOAD_DIR / stored_name if stored_name else None)
        print(f"[EvoLink invalid response] {result}", flush=True)
        return api_error(
            "EvoLinkから生成番号を受け取れませんでした。時間をおいて再度お試しください。",
            "DS-EVOLINK-002",
            502,
        )

    JOBS[task_id] = {
        "task_id": task_id,
        "user_id": current_user.id,
        "song": stored_name,
        "original_name": secure_filename(song_file.filename) if (stored_name and song_file) else None,
        "prompt": prompt,
        "model": model,
        "mode": mode,
        "resolution": resolution,
        "asset_paths": [str(path) for path in (image_path, ref_path) if path],
        "api_key": api_key,  # status/finalize でも同じユーザーのキーを使う
        "created": time.time(),
    }

    return jsonify(
        {
            "task_id": task_id,
            "status": result.get("status", "pending"),
            "mode": mode,
            "resolution": resolution,
            "has_audio": bool(stored_name),
        }
    )


@app.route("/status/<task_id>")
@login_required
def status(task_id):
    job = JOBS.get(task_id)
    if not job:
        return api_error(
            "生成情報が見つかりません。サーバー再起動などで消えた可能性があります。",
            "DS-JOB-404",
            404,
        )
    if job.get("user_id") != current_user.id:
        return api_error("この生成情報にはアクセスできません", "DS-AUTH-002", 403)

    try:
        data = get_seedance_task(task_id, job["api_key"])
    except Exception as e:  # noqa: BLE001
        print(f"[status error] task={task_id} {type(e).__name__}: {e}", flush=True)
        return api_error(
            "生成状況を取得できませんでした。時間をおいて再度お試しください。",
            "DS-STATUS-001",
            502,
        )

    results = data.get("results") or []
    task_error = None
    error_code = None
    if data.get("status") == "failed":
        raw_error = data.get("error")
        print(
            f"[EvoLink task failed] task={task_id} error={raw_error}",
            flush=True,
        )
        # 方針: 生成失敗の理由は隠さず、EvoLink が返した内容を添えて利用者に伝える。
        # （運営側の内部事情を隠す必要はないため。原文はサーバーログにも残す）
        detail = str(raw_error).strip() if raw_error else "詳細不明"
        task_error = f"EvoLinkからの失敗理由: {detail}"
        error_code = "DS-EVOLINK-003"
        cleanup_job_inputs(job)
    return jsonify(
        {
            "task_id": task_id,
            "status": data.get("status"),
            "progress": data.get("progress"),
            "video_url": results[0] if results else None,
            "error": task_error,
            "error_code": error_code,
        }
    )


@app.route("/finalize/<task_id>", methods=["POST"])
@login_required
def finalize(task_id):
    """生成完了した動画をDLし、アップした曲を合成して完成MP4を作る。冪等。"""
    job = JOBS.get(task_id)
    if not job:
        return api_error("生成情報が見つかりません", "DS-JOB-404", 404)
    if job.get("user_id") != current_user.id:
        return api_error("この生成情報にはアクセスできません", "DS-AUTH-002", 403)

    final_name = f"final_{task_id}.mp4"
    final_path = UPLOAD_DIR / final_name

    # 既に合成済みならそのまま返す（冪等）
    if final_path.exists():
        cleanup_job_inputs(job)
        return jsonify({"download_url": url_for("download", task_id=task_id)})

    # Seedance から結果動画の URL を取得
    try:
        data = get_seedance_task(task_id, job["api_key"])
    except Exception as e:  # noqa: BLE001
        print(f"[finalize status error] task={task_id} {type(e).__name__}: {e}", flush=True)
        return api_error(
            "完成動画の状態を取得できませんでした", "DS-FINALIZE-001", 502
        )

    if data.get("status") != "completed":
        return api_error("まだ生成が完了していません", "DS-FINALIZE-002", 409)
    results = data.get("results") or []
    if not results:
        return api_error("生成結果の動画がありません", "DS-FINALIZE-003", 502)

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
        print(f"[finalize error] task={task_id} {type(e).__name__}: {e}", flush=True)
        return api_error(
            "完成動画を準備できませんでした。時間をおいて再度お試しください。",
            "DS-FINALIZE-004",
            500,
        )
    finally:
        # 中間ファイルは掃除
        raw_video = UPLOAD_DIR / f"raw_{task_id}.mp4"
        if raw_video.exists():
            raw_video.unlink()

    job["final"] = final_name
    cleanup_job_inputs(job)
    return jsonify({"download_url": url_for("download", task_id=task_id)})


@app.route("/download/<task_id>")
@login_required
def download(task_id):
    """完成した MP4 をダウンロードさせる。"""
    job = JOBS.get(task_id)
    if not job:
        return api_error("生成情報が見つかりません", "DS-JOB-404", 404)
    if job.get("user_id") != current_user.id:
        return api_error("この動画にはアクセスできません", "DS-AUTH-002", 403)
    final_path = UPLOAD_DIR / f"final_{task_id}.mp4"
    if not final_path.exists():
        return api_error("まだ完成していません", "DS-DOWNLOAD-001", 404)
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
