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
