import base64
import functools
import hashlib
import hmac
import json

import frappe
from frappe import _
from shopify import GraphQL, Session

from shopify_integration.shopify.constants import (
	API_VERSION,
	EVENT_MAPPER,
	SETTING_DOCTYPE,
	WEBHOOK_EVENTS,
)
from shopify_integration.shopify.utils import create_shopify_log


def temp_shopify_session(func):
	"""Any function that needs to access shopify api needs this decorator. The decorator starts a temp session that's destroyed when function returns."""

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		# no auth in testing
		if frappe.flags.in_test:
			return func(*args, **kwargs)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		if setting.is_enabled():
			auth_details = (setting.shopify_url, API_VERSION, setting.get_password("password"))

			with Session.temp(*auth_details):
				return func(*args, **kwargs)

	return wrapper


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


@frappe.whitelist(allow_guest=True)
def store_request_data() -> None:
	if frappe.request:
		hmac_header = frappe.get_request_header("X-Shopify-Hmac-Sha256")

		_validate_request(frappe.request, hmac_header)

		data = json.loads(frappe.request.data)
		event = frappe.request.headers.get("X-Shopify-Topic")

		process_request(data, event)


def process_request(data, event):
	# create log
	log = create_shopify_log(method=EVENT_MAPPER[event], request_data=data)

	# enqueue backround job
	frappe.enqueue(
		method=EVENT_MAPPER[event],
		queue="short",
		timeout=300,
		is_async=True,
		**{"payload": data, "request_id": log.name},
	)


def _validate_request(req, hmac_header):
	settings = frappe.get_doc(SETTING_DOCTYPE)
	secret_key = settings.shared_secret

	sig = base64.b64encode(hmac.new(secret_key.encode("utf8"), req.data, hashlib.sha256).digest())

	if sig != bytes(hmac_header.encode()):
		create_shopify_log(status="Error", request_data=req.data)
		frappe.throw(_("Unverified Webhook Data"))
