# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

"""
OAuth 2.0 Client Credentials Flow for Shopify Apps (post Jan 1, 2026)

Apps created via Shopify Dev Dashboard use Client ID + Client Secret
to obtain short-lived (~24h) access tokens, instead of the old static
Admin API access token.

The token exchange itself happens over a plain REST endpoint
(`/{shop}/admin/oauth/access_token`), independent of the Admin API flavour.
The token it returns is only valid for the **GraphQL Admin API** — which is
all this connector uses — so it drops into `Session.temp(...)` and every
`GraphQL().execute(...)` call in the app works unchanged.

Reference:
  https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant
"""

import json
import time
from datetime import timedelta

import frappe
import requests
from frappe import _
from frappe.utils import get_datetime, get_datetime_str, now_datetime
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

from shopify_integration.shopify.constants import (
	AUTH_METHOD_OAUTH,
	HISTORICAL_ORDERS_ACCESS_SCOPE,
	REQUIRED_ACCESS_SCOPES,
	SETTING_DOCTYPE,
)
from shopify_integration.shopify.utils import create_shopify_log

# Default Shopify token lifetime when the response omits expires_in (~24h)
DEFAULT_TOKEN_EXPIRY_SECONDS = 86399


def get_oauth_token_endpoint(shopify_url: str) -> str:
	"""
	Construct the OAuth token endpoint URL for a given shop.

	Args:
	    shopify_url: e.g. 'example.myshopify.com'

	Returns:
	    'https://example.myshopify.com/admin/oauth/access_token'
	"""
	shop_url = shopify_url.replace("https://", "").replace("http://", "").rstrip("/")
	return f"https://{shop_url}/admin/oauth/access_token"


def get_missing_access_scopes(granted_scope: str | None) -> list[str]:
	"""Return the connector's required Admin API scopes missing from a token's
	granted scope string (comma-separated, as returned by Shopify's token
	response, e.g. "read_orders,write_products")."""
	if not granted_scope:
		return list(REQUIRED_ACCESS_SCOPES)
	granted = {scope.strip() for scope in granted_scope.split(",") if scope.strip()}
	return [scope for scope in REQUIRED_ACCESS_SCOPES if scope not in granted]


def generate_oauth_token(shopify_url: str, client_id: str, client_secret: str) -> dict:
	"""
	Generate a new OAuth 2.0 access token using Client Credentials Grant.

	POST https://{shop}.myshopify.com/admin/oauth/access_token
	  Content-Type: application/x-www-form-urlencoded
	  grant_type=client_credentials&client_id=...&client_secret=...

	Returns:
	    dict with keys: access_token, expires_in (86399 = ~24h), scope

	Raises:
	    frappe.ValidationError: If token generation fails
	"""
	token_endpoint = get_oauth_token_endpoint(shopify_url)

	payload = {
		"grant_type": "client_credentials",
		"client_id": client_id,
		"client_secret": client_secret,
	}

	headers = {
		"Content-Type": "application/x-www-form-urlencoded",
	}

	try:
		response = requests.post(token_endpoint, data=payload, headers=headers, timeout=30)
		response.raise_for_status()

		token_data = response.json()

		# Guard a malformed 200 (valid JSON but no token) so callers don't KeyError later
		if not token_data.get("access_token"):
			frappe.throw(
				_("Shopify returned a response without an access token"),
				title=_("OAuth Authentication Error"),
			)

		granted_scope = token_data.get("scope")
		missing_scopes = get_missing_access_scopes(granted_scope)
		if missing_scopes:
			# A token mints fine without the scopes this connector needs — it only
			# fails later, e.g. webhookSubscriptionCreate rejects order topics
			# without read_orders. Warn at mint time so the fix is obvious.
			create_shopify_log(
				status="Warning",
				method="shopify_integration.shopify.oauth.generate_oauth_token",
				message=_(
					"The Shopify app is missing the Admin API access scopes: {0}. Add them"
					" to the app version in the Shopify Dev Dashboard (app → App versions →"
					" Configuration → Admin API integration), release the version, reinstall"
					" the app on the shop, and save Shopify Setting again — otherwise webhook"
					" registration and sync calls will fail."
				).format(", ".join(missing_scopes)),
			)

		granted_scopes = {scope.strip() for scope in (granted_scope or "").split(",") if scope.strip()}
		if HISTORICAL_ORDERS_ACCESS_SCOPE not in granted_scopes:
			create_shopify_log(
				status="Warning",
				method="shopify_integration.shopify.oauth.generate_oauth_token",
				message=_(
					"The app does not have read_all_orders. Shopify order imports will be limited"
					" to orders created in the last 60 days. Request this scope only if older"
					" historical orders must be imported."
				),
			)

		create_shopify_log(
			status="Success",
			method="shopify_integration.shopify.oauth.generate_oauth_token",
			message=_("OAuth token generated successfully. Granted scopes: {0}").format(
				granted_scope or _("none")
			),
		)

		return token_data

	except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
		# Preserve transient failures so get_valid_access_token can retry them.
		raise
	except requests.exceptions.RequestException as e:
		error_message = str(e)
		error_code = None
		error_response = None

		if hasattr(e, "response") and e.response is not None:
			try:
				error_response = e.response.json()
				error_code = error_response.get("error")
				error_message = error_response.get("error_description", error_response.get("error", str(e)))
			except json.JSONDecodeError:
				error_message = e.response.text or str(e)

		# Surface actionable guidance for Shopify's known, predictable errors
		if error_code == "app_not_installed":
			error_message = _(
				"{0} — The app is not installed on this shop. Install it from the Shopify Dev"
				" Dashboard (App → Home → Install app → select this shop) and make sure the app"
				" and the shop belong to the same Shopify organization, then save again."
			).format(error_message)
		elif error_code == "shop_not_permitted":
			error_message = _(
				"{0} — Client credentials only work when the app and shop belong to the same"
				" Shopify organization. Create the app in the shop owner's organization, or use"
				" an authorization flow intended for apps installed on external merchant stores."
			).format(error_message)

		# Never log the actual client_secret
		sanitized_payload = payload.copy()
		sanitized_payload["client_secret"] = "***REDACTED***"

		create_shopify_log(
			status="Error",
			method="shopify_integration.shopify.oauth.generate_oauth_token",
			message=_("Failed to generate OAuth token"),
			exception=error_message,
			request_data=sanitized_payload,
			response_data=error_response,
		)

		frappe.throw(
			_("Failed to generate OAuth token: {0}").format(error_message),
			title=_("OAuth Authentication Error"),
		)


def is_token_valid(token_expires_at, buffer_minutes: int = 5) -> bool:
	"""
	Check if the OAuth token is still valid with a safety buffer.

	Returns False if token_expires_at is empty or within buffer_minutes of expiry.
	The 5-minute default prevents mid-request token expiry.
	"""
	if not token_expires_at:
		return False

	expiry_datetime = get_datetime(token_expires_at)
	buffer_time = now_datetime() + timedelta(minutes=buffer_minutes)

	return expiry_datetime > buffer_time


def calculate_token_expiry(expires_in_seconds: int):
	"""Convert Shopify's expires_in (seconds) into an absolute datetime."""
	return now_datetime() + timedelta(seconds=expires_in_seconds)


def refresh_oauth_token(setting, client_secret=None) -> str:
	"""
	Generate a fresh OAuth token and persist it encrypted in Shopify Setting.

	Args:
	    setting: ShopifySetting document instance
	    client_secret: Optional — pass directly during save cycle when
	                   the password hasn't been committed to DB yet.

	Returns:
	    The new access token string
	"""
	if setting.authentication_method != AUTH_METHOD_OAUTH:
		frappe.throw(
			_("Token refresh is only applicable for OAuth 2.0 authentication"),
			title=_("Invalid Authentication Method"),
		)

	# If client_secret not passed, read from DB (raise_exception=False so the
	# guard below produces a clean message instead of an AuthenticationError)
	if not client_secret:
		client_secret = setting.get_password("client_secret", raise_exception=False)

	if not client_secret:
		frappe.throw(
			_("Client Secret is missing. Please re-enter and save."),
			title=_("OAuth Authentication Error"),
		)

	token_data = generate_oauth_token(
		setting.shopify_url,
		setting.client_id,
		client_secret,
	)

	# Shopify may send expires_in as null or a string; coerce defensively
	try:
		expires_in = int(token_data.get("expires_in") or DEFAULT_TOKEN_EXPIRY_SECONDS)
	except (TypeError, ValueError):
		expires_in = DEFAULT_TOKEN_EXPIRY_SECONDS
	expires_at = calculate_token_expiry(expires_in)

	# Store token encrypted (same mechanism as the password field)
	set_encrypted_password(
		SETTING_DOCTYPE,
		setting.name,
		token_data["access_token"],
		fieldname="oauth_access_token",
	)

	# oauth_access_token is server-managed and never submitted by the client.
	# Flag it so Document._save_passwords (which runs after validate/before_save
	# and removes __Auth rows for empty Password fields) doesn't wipe the token
	# we just stored when the setting doc is saved.
	ignored = setting.flags.ignore_save_passwords
	if ignored is True:
		pass  # all password saving already disabled; token persists untouched
	else:
		ignored = list(ignored or [])
		if "oauth_access_token" not in ignored:
			ignored.append("oauth_access_token")
		setting.flags.ignore_save_passwords = ignored

	frappe.db.set_value(
		SETTING_DOCTYPE,
		setting.name,
		"token_expires_at",
		get_datetime_str(expires_at),
		update_modified=False,
	)

	# Sync only the field we wrote — full reload() would discard in-memory changes
	# made earlier in the same validate() call (e.g. the shopify_url https-strip)
	setting.token_expires_at = get_datetime_str(expires_at)

	# Let the controller know this doc instance already holds a fresh token,
	# so before_save doesn't mint a second one in the same save cycle.
	setting.flags.oauth_token_refreshed_during_save = True

	return token_data["access_token"]


def invalidate_cached_token(setting) -> None:
	"""Drop the cached OAuth token so the next save mints a fresh one.

	Granted scopes are fixed when a token is issued. If the app's Admin API
	access scopes were changed in the Shopify Dev Dashboard (e.g. to allow the
	order webhook topics), a still-valid cached token keeps the old scopes for
	up to ~24h. Called when webhook registration fails, so the retry after
	fixing the app re-mints with the updated scopes.
	"""
	if not setting.name:
		return

	try:
		remove_encrypted_password(SETTING_DOCTYPE, setting.name, fieldname="oauth_access_token")
		frappe.db.set_value(SETTING_DOCTYPE, setting.name, "token_expires_at", None, update_modified=False)
		setting.token_expires_at = None
	except Exception as e:
		create_shopify_log(
			status="Warning",
			method="shopify_integration.shopify.oauth.invalidate_cached_token",
			message=_("Could not clear the cached OAuth token; it will refresh at expiry."),
			exception=str(e),
		)


def get_valid_access_token(setting) -> str:
	"""
	Return a valid OAuth access token, auto-refreshing if needed.

	This is the main entry point — called by connection.py on every API request.
	"""
	if setting.authentication_method != AUTH_METHOD_OAUTH:
		frappe.throw(
			_("This method is only for OAuth 2.0 authentication"),
			title=_("Invalid Authentication Method"),
		)

	# Fast path: cached token is still valid
	if is_token_valid(setting.token_expires_at):
		current_token = setting.get_password("oauth_access_token", raise_exception=False)
		if current_token:
			return current_token

	# Token missing or expired — refresh, with a single retry for transient network errors only.
	# Permanent errors (bad credentials, missing secret) are NOT retried.
	try:
		return refresh_oauth_token(setting)
	except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
		create_shopify_log(
			status="Warning",
			method="shopify_integration.shopify.oauth.get_valid_access_token",
			message=_("Token refresh failed due to network error, retrying once..."),
			exception=str(e),
		)
		time.sleep(1)
		return refresh_oauth_token(setting)  # Let this raise if it still fails
