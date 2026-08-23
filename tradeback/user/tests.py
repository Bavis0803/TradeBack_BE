from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient


class AuthenticationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="auth-user",
            email="auth@example.com",
            password="secret-pass",
        )

    def test_signin_returns_jwt_for_valid_credentials(self):
        response = self.client.post(
            "/user/signin/",
            {"email": "auth@example.com", "password": "secret-pass"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_signin_rejects_invalid_credentials_without_server_error(self):
        response = self.client.post(
            "/user/signin/",
            {"email": "auth@example.com", "password": "wrong-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_profile_preferences_are_persisted_and_returned_on_login(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            "/user/me/",
            {"preferences": {"copy_message_notifications": False}},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["preferences"]["copy_message_notifications"])
        self.assertTrue(response.data["preferences"]["copy_signal_notifications"])

        self.client.force_authenticate(user=None)
        login = self.client.post(
            "/user/signin/",
            {"email": "auth@example.com", "password": "secret-pass"},
            format="json",
        )
        self.assertFalse(login.data["user"]["preferences"]["copy_message_notifications"])

    def test_profile_cannot_change_email(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            "/user/me/",
            {"email": "other@example.com", "first_name": "Updated"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "auth@example.com")
        self.assertEqual(self.user.first_name, "Updated")
