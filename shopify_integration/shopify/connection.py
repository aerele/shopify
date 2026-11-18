import base64
import functools
import hashlib
import hmac
import json
from contextlib import contextmanager

import frappe
from frappe import _
from shopify import GraphQL, Session

from shopify_integration.shopify.constants import (
	API_VERSION,
	AUTH_METHOD_OAUTH,
	EVENT_MAPPER,
	WEBHOOK_EVENTS,
)
from shopify_integration.shopify.utils import (
	create_shopify_log,
	get_account_name,
	get_default_account,
	get_enabled_accounts,
	get_shopify_account,
)


def temp_shopify_session(func):
	"""Any function that needs to access shopify api needs this decorator. The decorator starts a temp session that's destroyed when function returns.

	The session belongs to the account passed as the `shopify_account` keyword argument
	(document or name), or to the only enabled account when the caller passes none.
	"""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		# no auth in testing
		if frappe.flags.in_test:
			return func(*args, **kwargs)

		account = kwargs.get("shopify_account")
		setting = get_shopify_account(account) if account else get_default_account()
		if setting.is_enabled():
			with shopify_session(setting):
				return func(*args, **kwargs)

	return wrapper


@contextmanager
def shopify_session(shopify_account):
	"""Temporary Shopify API session for one account, for code that holds the account itself."""
	# no auth in testing
	if frappe.flags.in_test:
		yield
		return

	setting = get_shopify_account(shopify_account)
	with Session.temp(setting.shopify_url, API_VERSION, _get_access_token(setting)):
		yield


def _get_access_token(setting):
	"""
	Get the appropriate access token based on authentication method.
	For OAuth, ensures token is valid and refreshes if needed.
	"""
	if setting.authentication_method == AUTH_METHOD_OAUTH:
		# Import here to avoid circular dependency
		from shopify_integration.shopify.oauth import get_valid_access_token

		try:
			return get_valid_access_token(setting)
		except Exception as e:
			create_shopify_log(
				status="Error",
				method="shopify_integration.shopify.connection._get_access_token",
				message=_("Failed to get valid OAuth access token"),
				exception=str(e),
			)
			frappe.throw(
				_("Failed to authenticate with Shopify using OAuth 2.0: {0}").format(str(e)),
				title=_("Authentication Error"),
			)
	else:
		# Static Token authentication (legacy / pre-Jan 2026 apps)
		token = setting.get_password("password", raise_exception=False)
		if not token:
			frappe.throw(
				_("Shopify access token is not configured"),
				title=_("Authentication Error"),
			)
		return token


_WEBHOOK_CREATE_MUTATION = """
mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $callbackUrl: URL!) {
	webhookSubscriptionCreate(
		topic: $topic
		webhookSubscription: {format: JSON, callbackUrl: $callbackUrl}
	) {
		webhookSubscription {
			id
			topic
			endpoint {
				__typename
				... on WebhookHttpEndpoint {
					callbackUrl
				}
			}
		}
		userErrors {
			field
			message
		}
	}
}
"""

_WEBHOOK_SUBSCRIPTIONS_QUERY = """
query webhookSubscriptions {
	webhookSubscriptions(first: 250) {
		edges {
			node {
				id
				endpoint {
					__typename
					... on WebhookHttpEndpoint {
						callbackUrl
					}
				}
			}
		}
	}
}
"""

_WEBHOOK_DELETE_MUTATION = """
mutation webhookSubscriptionDelete($id: ID!) {
	webhookSubscriptionDelete(id: $id) {
		deletedWebhookSubscriptionId
		userErrors {
			field
			message
		}
	}
}
"""


def register_webhooks(shopify_url: str, password: str) -> list[dict]:
	"""Register required webhooks with shopify and return registered webhooks."""
	new_webhooks = []

	# clear all stale webhooks matching current site url before registering new ones
	unregister_webhooks(shopify_url, password)

	with Session.temp(shopify_url, API_VERSION, password):
		for topic in WEBHOOK_EVENTS:
			response = json.loads(
				GraphQL().execute(
					_WEBHOOK_CREATE_MUTATION,
					{"topic": topic, "callbackUrl": get_callback_url()},
				)
			)
			result = response.get("data", {}).get("webhookSubscriptionCreate") or {}
			user_errors = result.get("userErrors") or []

			if response.get("errors") or user_errors:
				create_shopify_log(
					status="Error",
					method="shopify_integration.shopify.connection.register_webhooks",
					message=_("Failed to register webhook for topic {0}.").format(topic)
					+ " "
					+ _(
						"If the error mentions the topic or access scopes, add the required"
						" Admin API access scopes to the app version in the Shopify Dev"
						" Dashboard and reinstall the app on the shop."
					),
					response_data=response,
					exception=response.get("errors") or user_errors,
				)
				continue

			webhook = result.get("webhookSubscription")
			if webhook:
				new_webhooks.append(webhook)

	return new_webhooks


def unregister_webhooks(shopify_url: str, password: str) -> None:
	"""Unregister all webhooks from shopify that correspond to current site url."""
	url = get_current_domain_name()

	with Session.temp(shopify_url, API_VERSION, password):
		response = json.loads(GraphQL().execute(_WEBHOOK_SUBSCRIPTIONS_QUERY))
		if response.get("errors"):
			create_shopify_log(status="Error", response_data=response, exception=response["errors"])
			return

		edges = response.get("data", {}).get("webhookSubscriptions", {}).get("edges", [])

		for edge in edges:
			node = edge.get("node") or {}
			callback_url = (node.get("endpoint") or {}).get("callbackUrl", "")

			if url in callback_url:
				delete_response = json.loads(
					GraphQL().execute(_WEBHOOK_DELETE_MUTATION, {"id": node.get("id")})
				)
				delete_result = delete_response.get("data", {}).get("webhookSubscriptionDelete") or {}
				user_errors = delete_result.get("userErrors") or []

				if delete_response.get("errors") or user_errors:
					create_shopify_log(
						status="Error",
						response_data=delete_response,
						exception=delete_response.get("errors") or user_errors,
					)


def get_current_domain_name() -> str:
	"""Get current site domain name. E.g. test.erpnext.com

	If developer_mode is enabled and localtunnel_url is set in site config then domain  is set to localtunnel_url.
	"""
	if frappe.conf.developer_mode and frappe.conf.localtunnel_url:
		return frappe.conf.localtunnel_url
	else:
		return frappe.request.host


def get_callback_url() -> str:
	"""Shopify calls this url when new events occur to subscribed webhooks.

	If developer_mode is enabled and localtunnel_url is set in site config then callback url is set to localtunnel_url.
	"""
	url = get_current_domain_name()

	return f"https://{url}/api/method/shopify_integration.shopify.connection.store_request_data"


# Shopify calls this webhook endpoint without a session; every request is verified with
# the HMAC signature of the store it claims to come from before anything is processed.
@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
def store_request_data() -> None:
	"""Validate and enqueue webhooks of the account they belong to."""
	if frappe.request:
		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")

		shopify_account = _get_webhook_account(frappe.request)
		if not shopify_account:
			return
		_validate_request(frappe.request, hmac_header, shopify_account)

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")

		process_request(data, event, shopify_account)


def _get_webhook_account(req):
	"""The account an incoming webhook belongs to.

	Webhooks are accepted while exactly one account is enabled; with more, the request
	cannot be attributed to a store and is logged and refused. While no account is enabled,
	deliveries are ignored.
	"""
	if not get_enabled_accounts():
		return None

	try:
		return get_default_account()
	except frappe.ValidationError as e:
		create_shopify_log(status="Error", request_data=frappe.safe_decode(req.data), exception=e)
		raise


def process_request(data, event, shopify_account=None):
	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data, shopify_account=shopify_account)

	# enqueue backround job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{
			"payload": data,
			"request_id": log.name,
			"shopify_account": get_account_name(shopify_account),
		},
	)


def get_webhook_secret(setting) -> str | None:
	"""Secret Shopify signs the account's webhooks with: an OAuth app signs with its client secret."""
	if setting.authentication_method == AUTH_METHOD_OAUTH:
		return setting.get_password("client_secret", raise_exception=False)
	return setting.shared_secret


def _validate_request(req, hmac_header, shopify_account):
	"""Verify the raw webhook body with the account's signing secret."""
	secret_key = get_webhook_secret(get_shopify_account(shopify_account))
	request_data = frappe.safe_decode(req.data)

	if not secret_key:
		create_shopify_log(
			status="Error",
			request_data=request_data,
			exception="Secret key not configured",
			shopify_account=shopify_account,
		)
		frappe.throw(_("Webhook validation failed: Secret key not configured"))

	if not hmac_header:
		create_shopify_log(
			status="Error",
			request_data=request_data,
			exception="Missing HMAC header",
			shopify_account=shopify_account,
		)
		frappe.throw(_("Unverified Webhook Data"))

	sig = base64.b64encode(hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest())

	# Timing-safe comparison to prevent signature timing attacks
	if not hmac.compare_digest(sig, hmac_header.encode()):
		create_shopify_log(status="Error", request_data=request_data, shopify_account=shopify_account)
		frappe.throw(_("Unverified Webhook Data"))
