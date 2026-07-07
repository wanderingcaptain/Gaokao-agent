import os
import unittest

from database.models import User
from pydantic import ValidationError

from routers.auth import AuthRequest, validate_password, validate_username
from services.auth_service import create_access_token, decode_access_token, hash_password, now_iso, verify_password


class AuthServiceTest(unittest.TestCase):
    def setUp(self):
        os.environ["JWT_SECRET_KEY"] = "test-secret"

    def test_password_hash_verify(self):
        password_hash = hash_password("secret123")

        self.assertTrue(verify_password("secret123", password_hash))
        self.assertFalse(verify_password("wrong", password_hash))
        self.assertNotIn("secret123", password_hash)

    def test_jwt_roundtrip(self):
        user = User(id=7, username="alice", password_hash="x", created_at=now_iso(), updated_at=now_iso())
        token = create_access_token(user)
        payload = decode_access_token(token)

        self.assertEqual(payload["sub"], "7")
        self.assertEqual(payload["username"], "alice")

    def test_username_rules(self):
        self.assertEqual(validate_username(" alice_01 "), "alice_01")

        invalid_usernames = ["ab", "a" * 21, "张三", "alice-01", "alice 01", "alice@01"]
        for username in invalid_usernames:
            with self.subTest(username=username):
                with self.assertRaises(ValueError):
                    validate_username(username)

    def test_password_rules(self):
        self.assertEqual(validate_password("abc12345"), "abc12345")

        invalid_passwords = ["abc123", "a" * 33 + "1", "abcdefgh", "12345678", "abc 12345"]
        for password in invalid_passwords:
            with self.subTest(password=password):
                with self.assertRaises(ValueError):
                    validate_password(password)

    def test_auth_request_uses_shared_rules(self):
        request = AuthRequest(username="alice_01", password="abc12345")

        self.assertEqual(request.username, "alice_01")
        self.assertEqual(request.password, "abc12345")

        with self.assertRaises(ValidationError) as ctx:
            AuthRequest(username="alice-01", password="abcdefgh")

        message = str(ctx.exception)
        self.assertIn("用户名需为3-20位", message)
        self.assertIn("密码需同时包含字母和数字", message)


if __name__ == "__main__":
    unittest.main()
