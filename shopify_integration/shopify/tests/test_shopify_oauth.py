# Copyright (c) 2026, Frappe and Contributors
# See LICENSE

import base64
import hashlib
import hmac
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.utils import now_datetime

from shopify_integration.shopify.constants import AUTH_METHOD_OAUTH, AUTH_METHOD_STATIC, SETTING_DOCTYPE
from shopify_integration.shopify.doctype.shopify_setting.shopify_setting import ShopifySetting


class TestIsTokenValid(unittest.TestCase):
	"""Pure logic — no Frappe DB needed."""

	def _import(self):
		from shopify_integration.shopify.oauth import is_token_valid

		return is_token_valid

	def test_missing_expiry_returns_false(self):
		is_token_valid = self._import()
		self.assertFalse(is_token_valid(None))
		self.assertFalse(is_token_valid(""))

	def test_valid_token_returns_true(self):
		is_token_valid = self._import()
		future = now_datetime() + timedelta(hours=1)
		self.assertTrue(is_token_valid(future))

	def test_token_within_buffer_returns_false(self):
		"""Token expiring in 3 min (< 5-min buffer) must trigger refresh."""
		is_token_valid = self._import()
		near_expiry = now_datetime() + timedelta(minutes=3)
		self.assertFalse(is_token_valid(near_expiry))

	def test_expired_token_returns_false(self):
		is_token_valid = self._import()
		past = now_datetime() - timedelta(hours=1)
		self.assertFalse(is_token_valid(past))


class TestGetMissingAccessScopes(unittest.TestCase):
	"""Pure logic — no Frappe DB needed."""

	def _import(self):
		from shopify_integration.shopify.oauth import get_missing_access_scopes

		return get_missing_access_scopes

	def test_none_scope_means_all_missing(self):
		fn = self._import()
		self.assertEqual(len(fn(None)), 6)
		self.assertEqual(len(fn("")), 6)

	def test_partial_scope_listed(self):
		fn = self._import()
		missing = fn("read_orders, read_products")
		self.assertIn("write_products", missing)
		self.assertNotIn("read_orders", missing)

	def test_all_granted_scopes_means_empty(self):
		fn = self._import()
		self.assertEqual(
			fn("read_orders,read_products,write_products,read_locations,read_inventory,write_inventory"), []
		)


class TestGetOauthTokenEndpoint(unittest.TestCase):
	"""URL construction — no HTTP or Frappe needed."""

	def _import(self):
		from shopify_integration.shopify.oauth import get_oauth_token_endpoint

		return get_oauth_token_endpoint

	def test_plain_domain(self):
		fn = self._import()
		self.assertEqual(
			fn("example.myshopify.com"),
			"https://example.myshopify.com/admin/oauth/access_token",
		)

	def test_strips_https_prefix(self):
		fn = self._import()
		self.assertEqual(
			fn("https://example.myshopify.com"),
			"https://example.myshopify.com/admin/oauth/access_token",
		)

	def test_strips_http_prefix(self):
		fn = self._import()
		self.assertEqual(
			fn("http://example.myshopify.com"),
			"https://example.myshopify.com/admin/oauth/access_token",
		)

	def test_strips_trailing_slash(self):
		fn = self._import()
		self.assertEqual(
			fn("example.myshopify.com/"),
			"https://example.myshopify.com/admin/oauth/access_token",
		)


class TestGenerateOauthToken(unittest.TestCase):
	"""Mock requests.post — no real HTTP."""

	def _import(self):
		from shopify_integration.shopify.oauth import generate_oauth_token

		return generate_oauth_token

	def _mock_response(self, status_code=200, json_data=None):
		mock_resp = MagicMock()
		mock_resp.status_code = status_code
		mock_resp.json.return_value = json_data or {
			"access_token": "shpat_test_token_abc123",
			"expires_in": 86399,
			"scope": "read_orders,write_orders",
		}
		if status_code >= 400:
			from requests.exceptions import HTTPError

			mock_resp.raise_for_status.side_effect = HTTPError(response=mock_resp)
		else:
			mock_resp.raise_for_status.return_value = None
		return mock_resp

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_success_returns_token_data(self, mock_post, mock_log):
		mock_post.return_value = self._mock_response()
		generate_oauth_token = self._import()

		result = generate_oauth_token("example.myshopify.com", "client_id_1", "client_secret_1")

		self.assertEqual(result["access_token"], "shpat_test_token_abc123")
		self.assertEqual(result["expires_in"], 86399)
		mock_post.assert_called_once()
		call_kwargs = mock_post.call_args
		# Verify correct endpoint
		self.assertIn("example.myshopify.com", call_kwargs[0][0])
		# Verify payload
		payload = call_kwargs[1]["data"]
		self.assertEqual(payload["grant_type"], "client_credentials")
		self.assertEqual(payload["client_id"], "client_id_1")
		self.assertNotIn("client_secret_1", str(mock_log.call_args))  # secret not in logs

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_http_401_raises_validation_error(self, mock_post, mock_log):
		mock_post.return_value = self._mock_response(
			status_code=401, json_data={"error": "invalid_client", "error_description": "Bad credentials"}
		)
		generate_oauth_token = self._import()

		with self.assertRaises(frappe.exceptions.ValidationError):
			generate_oauth_token("example.myshopify.com", "bad_id", "bad_secret")

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_app_not_installed_gets_actionable_message(self, mock_post, mock_log):
		"""Shopify's app_not_installed error must tell the user how to fix it."""
		mock_post.return_value = self._mock_response(
			status_code=400,
			json_data={
				"error": "app_not_installed",
				"error_description": "The application is not installed on this shop.",
			},
		)
		generate_oauth_token = self._import()

		with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
			generate_oauth_token("example.myshopify.com", "client_id_1", "client_secret_1")

		self.assertIn("Dev Dashboard", str(ctx.exception))

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_shop_not_permitted_explains_same_organization_requirement(self, mock_post, mock_log):
		mock_post.return_value = self._mock_response(
			status_code=400,
			json_data={
				"error": "shop_not_permitted",
				"error_description": "Client credentials cannot be performed on this shop.",
			},
		)
		generate_oauth_token = self._import()

		with self.assertRaises(frappe.ValidationError) as ctx:
			generate_oauth_token("example.myshopify.com", "client_id_1", "client_secret_1")

		self.assertIn("same Shopify organization", str(ctx.exception))

	@patch("requests.post", side_effect=requests.exceptions.Timeout("timed out"))
	def test_timeout_is_preserved_for_retry(self, mock_post):
		generate_oauth_token = self._import()

		with self.assertRaises(requests.exceptions.Timeout):
			generate_oauth_token("example.myshopify.com", "client_id_1", "client_secret_1")

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_client_secret_never_logged(self, mock_post, mock_log):
		"""Client secret must never appear in log calls."""
		mock_post.return_value = self._mock_response()
		generate_oauth_token = self._import()

		generate_oauth_token("example.myshopify.com", "client_id_1", "SUPER_SECRET_DO_NOT_LOG")

		for call in mock_log.call_args_list:
			self.assertNotIn("SUPER_SECRET_DO_NOT_LOG", str(call))

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_malformed_200_without_token_raises(self, mock_post, mock_log):
		"""A 200 response with valid JSON but no access_token must raise, not KeyError later."""
		mock_post.return_value = self._mock_response(json_data={"scope": "read_orders"})
		generate_oauth_token = self._import()

		with self.assertRaises(frappe.exceptions.ValidationError):
			generate_oauth_token("example.myshopify.com", "client_id_1", "client_secret_1")

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_success_log_includes_granted_scopes(self, mock_post, mock_log):
		"""The granted scope string is the only way to see what the app may do — log it."""
		mock_post.return_value = self._mock_response()
		generate_oauth_token = self._import()

		generate_oauth_token("example.myshopify.com", "client_id_1", "client_secret_1")

		success_calls = [c for c in mock_log.call_args_list if c.kwargs.get("status") == "Success"]
		self.assertTrue(success_calls)
		self.assertIn("read_orders", success_calls[0].kwargs.get("message", ""))

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_missing_required_scope_logs_warning(self, mock_post, mock_log):
		"""A token mints fine without read_orders but order webhooks will fail — warn at mint."""
		mock_post.return_value = self._mock_response(
			json_data={"access_token": "tok", "scope": "read_products"}
		)
		generate_oauth_token = self._import()

		generate_oauth_token("example.myshopify.com", "client_id_1", "client_secret_1")

		warning_calls = [c for c in mock_log.call_args_list if c.kwargs.get("status") == "Warning"]
		self.assertTrue(warning_calls)
		message = warning_calls[0].kwargs.get("message", "")
		self.assertIn("read_orders", message)
		self.assertIn("Dev Dashboard", message)


class TestGetValidAccessToken(unittest.TestCase):
	"""Mock setting doc + requests — tests token cache and refresh logic."""

	def _make_setting(self, token=None, expires_at=None, client_secret="cs_test"):
		setting = MagicMock()
		setting.authentication_method = AUTH_METHOD_OAUTH
		setting.shopify_url = "example.myshopify.com"
		setting.client_id = "ci_test"
		setting.client_secret = None  # raw attr empty (already saved to encrypted store)
		setting.token_expires_at = expires_at
		setting.get_password.side_effect = lambda field, **kw: (
			token if field == "oauth_access_token" else client_secret
		)
		setting.name = SETTING_DOCTYPE
		return setting

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	def test_valid_cached_token_no_http_call(self, mock_log):
		"""Fresh cached token must be returned without any HTTP request."""
		from shopify_integration.shopify.oauth import get_valid_access_token

		future = now_datetime() + timedelta(hours=1)
		setting = self._make_setting(token="cached_token", expires_at=future)

		with patch("requests.post") as mock_post:
			result = get_valid_access_token(setting)

		self.assertEqual(result, "cached_token")
		mock_post.assert_not_called()

	@patch("shopify_integration.shopify.oauth.set_encrypted_password")
	@patch("frappe.db.set_value")
	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_expired_token_triggers_refresh(self, mock_post, mock_log, mock_db_set, mock_encrypt):
		"""Token past expiry must trigger a new HTTP call."""
		from shopify_integration.shopify.oauth import get_valid_access_token

		past = now_datetime() - timedelta(hours=1)
		setting = self._make_setting(token="old_token", expires_at=past)

		mock_resp = MagicMock()
		mock_resp.raise_for_status.return_value = None
		mock_resp.json.return_value = {"access_token": "new_token", "expires_in": 86399}
		mock_post.return_value = mock_resp

		result = get_valid_access_token(setting)

		mock_post.assert_called_once()
		mock_encrypt.assert_called_once()
		self.assertEqual(result, "new_token")

	@patch("shopify_integration.shopify.oauth.set_encrypted_password")
	@patch("frappe.db.set_value")
	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_missing_token_triggers_fetch(self, mock_post, mock_log, mock_db_set, mock_encrypt):
		"""No cached token at all must trigger a fresh fetch."""
		from shopify_integration.shopify.oauth import get_valid_access_token

		setting = self._make_setting(token=None, expires_at=None)

		mock_resp = MagicMock()
		mock_resp.raise_for_status.return_value = None
		mock_resp.json.return_value = {"access_token": "fresh_token", "expires_in": 86399}
		mock_post.return_value = mock_resp

		result = get_valid_access_token(setting)

		mock_post.assert_called_once()
		self.assertEqual(result, "fresh_token")

	@patch("shopify_integration.shopify.oauth.set_encrypted_password")
	@patch("frappe.db.set_value")
	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("requests.post")
	def test_refresh_marks_token_field_server_managed(self, mock_post, mock_log, mock_db_set, mock_encrypt):
		"""Saving the setting must not wipe the stored token (see _save_passwords)."""
		from shopify_integration.shopify.oauth import refresh_oauth_token

		setting = self._make_setting()

		mock_resp = MagicMock()
		mock_resp.raise_for_status.return_value = None
		mock_resp.json.return_value = {"access_token": "tok", "expires_in": 86399}
		mock_post.return_value = mock_resp

		refresh_oauth_token(setting)

		self.assertIn("oauth_access_token", setting.flags.ignore_save_passwords)

	@patch("shopify_integration.shopify.oauth.time.sleep")
	@patch("shopify_integration.shopify.oauth.refresh_oauth_token")
	def test_transient_refresh_failure_is_retried_once(self, mock_refresh, mock_sleep):
		from shopify_integration.shopify.oauth import get_valid_access_token

		setting = self._make_setting(token=None, expires_at=None)
		mock_refresh.side_effect = [requests.exceptions.ConnectionError("offline"), "new_token"]

		self.assertEqual(get_valid_access_token(setting), "new_token")
		self.assertEqual(mock_refresh.call_count, 2)
		mock_sleep.assert_called_once_with(1)


class TestInvalidateCachedToken(unittest.TestCase):
	"""Stale-scope recovery — see invalidate_cached_token."""

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("frappe.db.set_value")
	@patch("shopify_integration.shopify.oauth.remove_encrypted_password")
	def test_invalidate_removes_token_and_expiry(self, mock_remove, mock_db_set, mock_log):
		from shopify_integration.shopify.oauth import invalidate_cached_token

		setting = MagicMock()
		setting.name = SETTING_DOCTYPE

		invalidate_cached_token(setting)

		mock_remove.assert_called_once()
		args = mock_remove.call_args
		self.assertEqual(args.args[0], SETTING_DOCTYPE)
		self.assertEqual(args.kwargs.get("fieldname"), "oauth_access_token")
		mock_db_set.assert_called_once()
		self.assertIsNone(setting.token_expires_at)

	@patch("shopify_integration.shopify.oauth.create_shopify_log")
	@patch("frappe.db.set_value")
	@patch("shopify_integration.shopify.oauth.remove_encrypted_password")
	def test_invalidate_without_name_is_noop(self, mock_remove, mock_db_set, mock_log):
		"""Unsaved doc (no name) has nothing cached — must not touch __Auth."""
		from shopify_integration.shopify.oauth import invalidate_cached_token

		setting = MagicMock()
		setting.name = None

		invalidate_cached_token(setting)

		mock_remove.assert_not_called()
		mock_db_set.assert_not_called()


class TestGetAccessToken(unittest.TestCase):
	"""Auth-mode branch in connection._get_access_token (used by temp_shopify_session)."""

	def _import(self):
		from shopify_integration.shopify.connection import _get_access_token

		return _get_access_token

	def test_static_token_returns_password(self):
		_get_access_token = self._import()
		setting = MagicMock()
		setting.authentication_method = AUTH_METHOD_STATIC
		setting.get_password.return_value = "static_token"

		self.assertEqual(_get_access_token(setting), "static_token")

	def test_static_token_missing_raises(self):
		_get_access_token = self._import()
		setting = MagicMock()
		setting.authentication_method = AUTH_METHOD_STATIC
		setting.get_password.return_value = None

		with self.assertRaises(frappe.exceptions.ValidationError):
			_get_access_token(setting)

	@patch("shopify_integration.shopify.connection.create_shopify_log")
	def test_oauth_delegates_to_get_valid_access_token(self, mock_log):
		_get_access_token = self._import()
		setting = MagicMock()
		setting.authentication_method = AUTH_METHOD_OAUTH

		with patch("shopify_integration.shopify.oauth.get_valid_access_token") as mock_valid:
			mock_valid.return_value = "oauth_token"
			result = _get_access_token(setting)

		self.assertEqual(result, "oauth_token")
		mock_valid.assert_called_once_with(setting)


class TestValidateRequestHmac(unittest.TestCase):
	"""HMAC validation in connection._validate_request."""

	def _make_hmac(self, secret: str, payload: bytes) -> str:
		sig = base64.b64encode(hmac.new(secret.encode("utf8"), payload, hashlib.sha256).digest())
		return sig.decode()

	def _make_mock_request(self, payload: bytes):
		req = MagicMock()
		req.data = payload
		return req

	def _make_setting(self, auth_method, shared_secret=None, client_secret=None):
		setting = MagicMock()
		setting.authentication_method = auth_method
		setting.shared_secret = shared_secret
		setting.get_password.return_value = client_secret
		return setting

	@patch("shopify_integration.shopify.connection.create_shopify_log")
	@patch("frappe.get_doc")
	def test_oauth_uses_client_secret_for_hmac(self, mock_get_doc, mock_log):
		from shopify_integration.shopify.connection import _validate_request

		payload = b'{"id": 123}'
		secret = "oauth_client_secret"
		mock_get_doc.return_value = self._make_setting(AUTH_METHOD_OAUTH, client_secret=secret)
		correct_hmac = self._make_hmac(secret, payload)
		req = self._make_mock_request(payload)

		# Should not raise
		_validate_request(req, correct_hmac)

	@patch("shopify_integration.shopify.connection.create_shopify_log")
	@patch("frappe.get_doc")
	def test_static_token_uses_shared_secret_for_hmac(self, mock_get_doc, mock_log):
		from shopify_integration.shopify.connection import _validate_request

		payload = b'{"id": 456}'
		secret = "static_shared_secret"
		mock_get_doc.return_value = self._make_setting(AUTH_METHOD_STATIC, shared_secret=secret)
		correct_hmac = self._make_hmac(secret, payload)
		req = self._make_mock_request(payload)

		# Should not raise
		_validate_request(req, correct_hmac)

	@patch("shopify_integration.shopify.connection.create_shopify_log")
	@patch("frappe.throw")
	@patch("frappe.get_doc")
	def test_wrong_secret_fails_hmac(self, mock_get_doc, mock_throw, mock_log):
		from shopify_integration.shopify.connection import _validate_request

		payload = b'{"id": 789}'
		mock_get_doc.return_value = self._make_setting(AUTH_METHOD_OAUTH, client_secret="correct_secret")
		wrong_hmac = self._make_hmac("wrong_secret", payload)
		req = self._make_mock_request(payload)

		_validate_request(req, wrong_hmac)

		mock_throw.assert_called_once()
		self.assertIn("Unverified", str(mock_throw.call_args))

	@patch("shopify_integration.shopify.connection.create_shopify_log")
	@patch("frappe.get_doc")
	def test_missing_hmac_header_fails(self, mock_get_doc, mock_log):
		"""A request with no HMAC header must be rejected, not crash on None.encode()."""
		from shopify_integration.shopify.connection import _validate_request

		mock_get_doc.return_value = self._make_setting(AUTH_METHOD_OAUTH, client_secret="correct_secret")
		req = self._make_mock_request(b'{"id": 1}')

		# frappe.throw raises ValidationError, halting before the None.encode() path
		with self.assertRaises(frappe.exceptions.ValidationError):
			_validate_request(req, None)

	@patch("shopify_integration.shopify.connection.create_shopify_log")
	@patch("frappe.get_doc")
	def test_missing_secret_key_fails(self, mock_get_doc, mock_log):
		"""No secret configured must be rejected, not crash on None.encode()."""
		from shopify_integration.shopify.connection import _validate_request

		mock_get_doc.return_value = self._make_setting(AUTH_METHOD_OAUTH, client_secret=None)
		req = self._make_mock_request(b'{"id": 2}')

		with self.assertRaises(frappe.exceptions.ValidationError):
			_validate_request(req, self._make_hmac("any", b'{"id": 2}'))


class TestStoreRequestData(unittest.TestCase):
	@patch("shopify_integration.shopify.connection.process_request")
	@patch("shopify_integration.shopify.connection._validate_request")
	@patch("shopify_integration.shopify.connection.frappe.get_doc")
	def test_disabled_integration_ignores_remote_webhooks(self, mock_get_doc, mock_validate, mock_process):
		from shopify_integration.shopify.connection import store_request_data

		setting = MagicMock()
		setting.is_enabled.return_value = False
		mock_get_doc.return_value = setting
		had_request = hasattr(frappe.local, "request")
		previous_request = getattr(frappe.local, "request", None)
		frappe.local.request = MagicMock()
		try:
			store_request_data()
		finally:
			if had_request:
				frappe.local.request = previous_request
			else:
				del frappe.local.request

		mock_validate.assert_not_called()
		mock_process.assert_not_called()


class TestShopifySettingAuth(unittest.TestCase):
	"""Authentication field validation and token pre-generation on the setting doc.

	Plain unittest.TestCase: these tests only touch the Shopify Setting single,
	not the standard company/customer fixtures.
	"""

	def _make_setting(self, **values):
		# Construct the controller class directly instead of frappe.get_doc:
		# on benches where ecommerce_integrations and shopify_integration
		# coexist, doctype-controller resolution (module "shopify" is ambiguous)
		# may load the wrong app's controller outside request/job context.
		setting = ShopifySetting(
			{
				"doctype": SETTING_DOCTYPE,
				"enable_shopify": 1,
				"shopify_url": "test-store.myshopify.com",
				"authentication_method": AUTH_METHOD_STATIC,
				"password": "token",
				"shared_secret": "secret",
				**values,
			}
		)
		# keep tests independent of whatever password the site has in __Auth
		setting._get_password_safe = lambda fieldname: ""
		return setting

	def test_static_token_requires_password(self):
		setting = self._make_setting(password=None, shared_secret="secret")

		with self.assertRaises(frappe.ValidationError):
			setting._validate_authentication_fields()

	def test_static_token_requires_shared_secret(self):
		setting = self._make_setting(password="token", shared_secret=None)

		with self.assertRaises(frappe.ValidationError):
			setting._validate_authentication_fields()

	def test_static_token_with_all_fields_passes(self):
		setting = self._make_setting()

		setting._validate_authentication_fields()  # should not raise

	def test_oauth_requires_client_id(self):
		setting = self._make_setting(
			authentication_method=AUTH_METHOD_OAUTH,
			password=None,
			shared_secret=None,
			client_id=None,
			client_secret="cs",
		)

		with self.assertRaises(frappe.ValidationError):
			setting._validate_authentication_fields()

	def test_oauth_requires_client_secret(self):
		setting = self._make_setting(
			authentication_method=AUTH_METHOD_OAUTH,
			password=None,
			shared_secret=None,
			client_id="cid",
			client_secret=None,
		)

		with self.assertRaises(frappe.ValidationError):
			setting._validate_authentication_fields()

	def test_oauth_with_all_fields_passes(self):
		setting = self._make_setting(
			authentication_method=AUTH_METHOD_OAUTH,
			password=None,
			shared_secret=None,
			client_id="cid",
			client_secret="cs",
		)

		setting._validate_authentication_fields()  # should not raise

	def test_default_authentication_method_set_when_missing(self):
		setting = self._make_setting(authentication_method=None)

		setting._set_default_authentication_method()

		self.assertEqual(setting.authentication_method, AUTH_METHOD_STATIC)

	def test_authentication_change_is_blocked_while_previously_enabled(self):
		setting = self._make_setting(authentication_method=AUTH_METHOD_OAUTH, client_id="cid")
		setting.get_doc_before_save = lambda: frappe._dict(enable_shopify=1)
		setting.has_value_changed = lambda fieldname: fieldname == "authentication_method"

		with self.assertRaises(frappe.ValidationError):
			setting._validate_authentication_change()

	def test_authentication_change_is_allowed_after_disable(self):
		setting = self._make_setting(authentication_method=AUTH_METHOD_OAUTH, client_id="cid")
		setting.get_doc_before_save = lambda: frappe._dict(enable_shopify=0)
		setting.has_value_changed = lambda fieldname: True

		setting._validate_authentication_change()

	def test_dummy_password_is_not_treated_as_credential_change(self):
		setting = self._make_setting(password="********")
		setting.get_doc_before_save = lambda: frappe._dict(enable_shopify=1)
		setting.has_value_changed = lambda fieldname: False

		setting._validate_authentication_change()

	def test_new_plaintext_password_is_blocked_while_enabled(self):
		setting = self._make_setting(password="new-token")
		setting.get_doc_before_save = lambda: frappe._dict(enable_shopify=1)
		setting.has_value_changed = lambda fieldname: False

		with self.assertRaises(frappe.ValidationError):
			setting._validate_authentication_change()

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	def test_before_save_skips_second_token_mint(self, mock_log):
		"""Token minted during validate (webhook registration) must not be repeated."""
		setting = self._make_setting(
			authentication_method=AUTH_METHOD_OAUTH, client_id="cid", client_secret="cs"
		)
		setting.flags.oauth_token_refreshed_during_save = True

		with patch("shopify_integration.shopify.oauth.refresh_oauth_token") as mock_refresh:
			setting.before_save()

		mock_refresh.assert_not_called()

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	def test_before_save_refreshes_on_credential_change(self, mock_log):
		setting = self._make_setting(
			authentication_method=AUTH_METHOD_OAUTH, client_id="cid", client_secret="cs"
		)
		setting.has_value_changed = lambda fieldname: fieldname == "client_id"

		with patch("shopify_integration.shopify.oauth.refresh_oauth_token") as mock_refresh:
			setting.before_save()

		mock_refresh.assert_called_once()
		# in-memory plaintext secret must be passed, not the masked dummy
		self.assertEqual(mock_refresh.call_args[1]["client_secret"], "cs")

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	def test_before_save_ignores_static_token(self, mock_log):
		setting = self._make_setting()

		with patch("shopify_integration.shopify.oauth.refresh_oauth_token") as mock_refresh:
			setting.before_save()

		mock_refresh.assert_not_called()

	@patch("frappe.utils.password.set_encrypted_password")
	@patch("frappe.utils.password.remove_encrypted_password")
	def test_save_passwords_does_not_wipe_stored_oauth_token(self, mock_remove, mock_set):
		"""Regression: _save_passwords removes __Auth rows for empty Password fields,
		which would delete the server-minted oauth token on every setting save."""
		setting = self._make_setting(
			authentication_method=AUTH_METHOD_OAUTH, client_id="cid", client_secret=None
		)
		setting.name = SETTING_DOCTYPE
		setting.flags.ignore_save_passwords = ["oauth_access_token"]

		setting._save_passwords()

		for call in mock_remove.call_args_list:
			self.assertNotIn("oauth_access_token", str(call))
		for call in mock_set.call_args_list:
			self.assertNotIn("oauth_access_token", str(call))

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	@patch("shopify_integration.shopify.connection.register_webhooks")
	@patch("shopify_integration.shopify.oauth.invalidate_cached_token")
	def test_failed_registration_invalidates_cached_token(self, mock_invalidate, mock_register, mock_log):
		"""Regression: a token keeps the scopes granted at issue time. When all topics
		fail to register, the retry must mint a fresh token (whose scopes may have
		been fixed in the Dev Dashboard), not serve the stale cached one."""
		setting = self._make_setting(authentication_method=AUTH_METHOD_OAUTH, client_id="cid")
		setting._get_or_generate_oauth_token = lambda: "stale_token"
		mock_register.return_value = []

		with self.assertRaises(frappe.ValidationError):
			setting._handle_webhooks()

		mock_invalidate.assert_called_once_with(setting)

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	@patch("shopify_integration.shopify.connection.register_webhooks")
	@patch("shopify_integration.shopify.oauth.invalidate_cached_token")
	def test_partial_registration_also_invalidates_cached_token(
		self, mock_invalidate, mock_register, mock_log
	):
		"""A single registered topic out of five still means the token is suspect."""
		setting = self._make_setting(authentication_method=AUTH_METHOD_OAUTH, client_id="cid")
		setting._get_or_generate_oauth_token = lambda: "stale_token"
		mock_register.return_value = [{"id": "gid://shopify/WebhookSubscription/1", "topic": "ORDERS_CREATE"}]

		setting._handle_webhooks()  # no throw on partial

		mock_invalidate.assert_called_once_with(setting)
		self.assertEqual(len(setting.webhooks), 1)

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	@patch("shopify_integration.shopify.connection.register_webhooks")
	@patch("shopify_integration.shopify.oauth.invalidate_cached_token")
	def test_full_registration_keeps_cached_token(self, mock_invalidate, mock_register, mock_log):
		setting = self._make_setting(authentication_method=AUTH_METHOD_OAUTH, client_id="cid")
		setting._get_or_generate_oauth_token = lambda: "good_token"
		mock_register.return_value = [
			{"id": f"gid://shopify/WebhookSubscription/{i}", "topic": t}
			for i, t in enumerate(
				[
					"ORDERS_CREATE",
					"ORDERS_PAID",
					"ORDERS_FULFILLED",
					"ORDERS_CANCELLED",
					"ORDERS_PARTIALLY_FULFILLED",
				],
				1,
			)
		]

		setting._handle_webhooks()

		mock_invalidate.assert_not_called()
		self.assertEqual(len(setting.webhooks), 5)

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	@patch("shopify_integration.shopify.connection.register_webhooks")
	@patch("shopify_integration.shopify.oauth.invalidate_cached_token")
	def test_static_token_registration_never_invalidates(self, mock_invalidate, mock_register, mock_log):
		"""Static tokens are long-lived Admin API tokens — nothing to re-mint."""
		setting = self._make_setting()
		mock_register.return_value = []

		with self.assertRaises(frappe.ValidationError):
			setting._handle_webhooks()

		mock_invalidate.assert_not_called()

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	@patch("shopify_integration.shopify.connection.unregister_webhooks")
	def test_disable_oauth_uses_a_valid_token(self, mock_unregister, mock_log):
		setting = self._make_setting(enable_shopify=0, authentication_method=AUTH_METHOD_OAUTH)
		setting.webhooks = [frappe._dict(webhook_id="1")]
		setting._get_or_generate_oauth_token = MagicMock(return_value="fresh_token")

		setting._handle_webhooks()

		setting._get_or_generate_oauth_token.assert_called_once()
		mock_unregister.assert_called_once_with(setting.shopify_url, "fresh_token")
		self.assertEqual(setting.webhooks, [])

	@patch("shopify_integration.shopify.doctype.shopify_setting.shopify_setting.create_shopify_log")
	@patch("shopify_integration.shopify.connection.unregister_webhooks")
	def test_disable_succeeds_when_remote_cleanup_fails(self, mock_unregister, mock_log):
		setting = self._make_setting(enable_shopify=0, authentication_method=AUTH_METHOD_OAUTH)
		setting.webhooks = [frappe._dict(webhook_id="1")]
		setting._get_or_generate_oauth_token = MagicMock(side_effect=Exception("Shopify unavailable"))

		setting._handle_webhooks()

		mock_unregister.assert_not_called()
		self.assertEqual(setting.webhooks, [])
		mock_log.assert_called_once()
