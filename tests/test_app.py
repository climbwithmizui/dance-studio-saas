import io
import os
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

import app as app_module  # noqa: E402


class DanceStudioTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        app_module.JOBS.clear()
        with app_module.app.app_context():
            app_module.db.drop_all()
            app_module.db.create_all()

    def create_user(self, email="user@example.com", active=True):
        with app_module.app.app_context():
            user = app_module.User(
                email=email,
                subscription_status="active" if active else "free",
            )
            user.set_password("password123")
            app_module.db.session.add(user)
            app_module.db.session.commit()
            return user.id

    def login(self, email="user@example.com"):
        return self.client.post(
            "/login",
            data={"email": email, "password": "password123"},
        )

    def active_client(self, email="user@example.com"):
        user_id = self.create_user(email)
        self.login(email)
        return user_id

    def upload_data(self, include_reference=True, include_song=True):
        data = {
            "api_key": "test-evolink-key",
            "consent": "1",
            "resolution": "1080p",
            "image": (io.BytesIO(b"image"), "character.png"),
        }
        if include_reference:
            data["reference"] = (io.BytesIO(b"video"), "dance.mp4")
        if include_song:
            data["song"] = (io.BytesIO(b"audio"), "song.mp3")
        return data

    def test_index_contains_preflight_and_quick_guide(self):
        self.active_client()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("初回用・最短3分ガイド", response.get_data(as_text=True))
        self.assertIn("生成内容の最終確認", response.get_data(as_text=True))

    @patch.object(app_module, "probe_video_duration", return_value=10.0)
    @patch.object(app_module, "create_seedance_job", return_value={"id": "task-ref", "status": "pending"})
    def test_reference_mode_is_forced_to_720p_and_silent(self, create_job, _probe):
        user_id = self.active_client()
        response = self.client.post(
            "/upload", data=self.upload_data(), content_type="multipart/form-data"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "reference-to-video")
        self.assertEqual(payload["resolution"], "720p")
        self.assertFalse(payload["has_audio"])
        self.assertEqual(app_module.JOBS["task-ref"]["user_id"], user_id)
        self.assertIsNone(app_module.JOBS["task-ref"]["song"])
        self.assertEqual(create_job.call_args.args[-1], "720p")
        app_module.cleanup_job_inputs(app_module.JOBS["task-ref"])

    @patch.object(app_module, "probe_video_duration", return_value=15.1)
    @patch.object(app_module, "create_seedance_job")
    def test_reference_video_over_15_seconds_is_rejected(self, create_job, _probe):
        self.active_client()
        response = self.client.post(
            "/upload",
            data=self.upload_data(include_song=False),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "DS-VIDEO-004")
        create_job.assert_not_called()

    @patch.object(app_module, "create_seedance_job", return_value={"id": "task-original"})
    def test_original_mode_keeps_selected_resolution_and_audio(self, create_job):
        self.active_client()
        response = self.client.post(
            "/upload",
            data=self.upload_data(include_reference=False, include_song=True),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "image-to-video")
        self.assertEqual(payload["resolution"], "1080p")
        self.assertTrue(payload["has_audio"])
        self.assertEqual(create_job.call_args.args[-1], "1080p")
        app_module.cleanup_job_inputs(app_module.JOBS["task-original"])

    def test_invalid_reference_format_does_not_fall_back(self):
        self.active_client()
        data = self.upload_data(include_reference=False, include_song=False)
        data["reference"] = (io.BytesIO(b"video"), "dance.mov")
        response = self.client.post(
            "/upload", data=data, content_type="multipart/form-data"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "DS-VIDEO-002")

    def test_job_endpoints_require_login(self):
        response = self.client.get("/status/unknown")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "DS-AUTH-001")

    def test_user_cannot_read_another_users_job(self):
        owner_id = self.create_user("owner@example.com")
        self.create_user("other@example.com")
        self.login("other@example.com")
        app_module.JOBS["private-task"] = {
            "user_id": owner_id,
            "api_key": "secret",
            "asset_paths": [],
            "song": None,
        }
        response = self.client.get("/status/private-task")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error_code"], "DS-AUTH-002")

    @patch("app.get_seedance_task")
    def test_failed_status_shows_upstream_error(self, get_seedance_task):
        # 方針: 生成失敗の理由は隠さず、EvoLink が返した内容を利用者に伝える。
        owner_id = self.create_user("owner@example.com")
        self.login("owner@example.com")
        app_module.JOBS["failed-task"] = {
            "user_id": owner_id,
            "api_key": "evk-secret",
            "asset_paths": [],
            "song": None,
        }
        get_seedance_task.return_value = {
            "status": "failed",
            "error": "sensitive upstream detail",
        }

        response = self.client.get("/status/failed-task")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["error_code"], "DS-EVOLINK-003")
        # 失敗理由（EvoLink の原文）が利用者向けレスポンスに含まれること
        self.assertIn("sensitive upstream detail", payload["error"])
        # 入力素材・api_key はクリーンアップされること
        self.assertIsNone(app_module.JOBS["failed-task"]["api_key"])

    def test_tokushoho_page_is_public(self):
        # 認証不要で表示でき、主要な記載が含まれること
        response = self.client.get("/tokushoho")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("特定商取引法に基づく表記", body)
        self.assertIn("CLIMB with MIZUI", body)
        self.assertIn("著作権等に関する免責事項", body)

    def test_public_guide_hides_details_and_api_guide_requires_subscription(self):
        guide_response = self.client.get("/guide")
        key_response = self.client.get("/guide/evolink-api-key")
        guide_body = guide_response.get_data(as_text=True)
        self.assertEqual(guide_response.status_code, 200)
        self.assertIn("STEP 1：利用準備", guide_body)
        self.assertIn("2つの生成方法", guide_body)
        self.assertIn("月額1,980円", guide_body)
        self.assertNotIn("EvoLink", guide_body)
        self.assertNotIn("PNG / JPG", guide_body)
        self.assertNotIn("APIキー取得ガイド", guide_body)
        self.assertEqual(key_response.status_code, 302)
        self.assertIn("/login", key_response.headers["Location"])

        self.create_user(active=False)
        self.login()
        free_guide = self.client.get("/guide")
        free_key = self.client.get("/guide/evolink-api-key")
        self.assertEqual(free_guide.status_code, 200)
        self.assertNotIn("PNG / JPG", free_guide.get_data(as_text=True))
        self.assertEqual(free_key.status_code, 302)
        self.assertTrue(free_key.headers["Location"].endswith("/"))

        with app_module.app.app_context():
            user = app_module.User.query.filter_by(email="user@example.com").first()
            user.subscription_status = "active"
            app_module.db.session.commit()

        active_guide = self.client.get("/guide")
        active_key = self.client.get("/guide/evolink-api-key")
        guide_body = active_guide.get_data(as_text=True)
        key_body = active_key.get_data(as_text=True)
        self.assertEqual(active_guide.status_code, 200)
        self.assertEqual(active_key.status_code, 200)
        self.assertIn("15秒・200MB", guide_body)
        self.assertIn("PNG / JPG", guide_body)
        self.assertIn("100MB", guide_body)
        self.assertIn("トラブルシューティング", guide_body)
        self.assertIn("https://evolink.ai/dashboard/keys", key_body)
        self.assertIn("APIキーはサポートへ送らない", key_body)

    def test_public_auth_pages_link_to_overview_guide(self):
        login_body = self.client.get("/login").get_data(as_text=True)
        signup_body = self.client.get("/signup").get_data(as_text=True)
        self.assertIn("/guide", login_body)
        self.assertIn("/guide", signup_body)
        self.assertIn("/terms", login_body)
        self.assertIn("/privacy", login_body)
        self.assertIn("/tokushoho", login_body)

        self.create_user(active=False)
        self.login()
        free_index = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/guide"', free_index)
        self.assertNotIn("EvoLink", free_index)
        self.assertIn("外部サービス", free_index)
        self.assertIn("/tokushoho", free_index)

        with app_module.app.app_context():
            user = app_module.User.query.filter_by(email="user@example.com").first()
            user.subscription_status = "active"
            app_module.db.session.commit()
        active_index = self.client.get("/").get_data(as_text=True)
        self.assertIn("/guide/evolink-api-key", active_index)
        self.assertNotIn("基本は10秒以内", active_index)

    def test_terms_and_privacy_are_public(self):
        terms_response = self.client.get("/terms")
        privacy_response = self.client.get("/privacy")
        terms_body = terms_response.get_data(as_text=True)
        privacy_body = privacy_response.get_data(as_text=True)

        self.assertEqual(terms_response.status_code, 200)
        self.assertEqual(privacy_response.status_code, 200)
        self.assertIn("Dance Studio 利用規約", terms_body)
        self.assertIn("月額1,980円", terms_body)
        self.assertIn("/tokushoho", terms_body)
        self.assertIn("Dance Studio プライバシーポリシー", privacy_body)
        self.assertIn("永続保存せず", privacy_body)
        self.assertIn("2026年9月3日", privacy_body)
        self.assertIn("EvoLink", terms_body)
        self.assertIn("Seedance", terms_body)
        self.assertIn("EvoLink", privacy_body)

    def test_terms_and_privacy_links_fall_back_and_allow_overrides(self):
        self.active_client()
        with patch.dict(
            os.environ,
            {"TERMS_URL": "", "PRIVACY_URL": ""},
        ):
            internal_body = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/terms"', internal_body)
        self.assertIn('href="/privacy"', internal_body)

        with patch.dict(
            os.environ,
            {
                "TERMS_URL": "https://example.com/custom-terms",
                "PRIVACY_URL": "https://example.com/custom-privacy",
            },
        ):
            override_body = self.client.get("/").get_data(as_text=True)
        self.assertIn("https://example.com/custom-terms", override_body)
        self.assertIn("https://example.com/custom-privacy", override_body)

    def test_missing_stripe_configuration_is_safe(self):
        self.active_client()
        old_price = app_module.PRICE_ID
        old_key = app_module.stripe.api_key
        app_module.PRICE_ID = None
        app_module.stripe.api_key = None
        try:
            response = self.client.post("/create-checkout-session")
        finally:
            app_module.PRICE_ID = old_price
            app_module.stripe.api_key = old_key
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error_code"], "DS-PAYMENT-001")
        self.assertNotIn("secret", response.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()
