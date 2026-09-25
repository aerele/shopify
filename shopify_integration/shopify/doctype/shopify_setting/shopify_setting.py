# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import json

import frappe
from ecommerce_core.controllers.setting import (
	ERPNextWarehouse,
	IntegrationWarehouse,
	SettingController,
)
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import get_datetime
from shopify import GraphQL

from shopify_integration.shopify import connection
from shopify_integration.shopify.constants import (
	ADDRESS_ID_FIELD,
	AUTH_METHOD_OAUTH,
	AUTH_METHOD_STATIC,
	CUSTOMER_ID_FIELD,
	FULLFILLMENT_ID_FIELD,
	ITEM_SELLING_RATE_FIELD,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SUPPLIER_ID_FIELD,
	WEBHOOK_EVENTS,
)
from shopify_integration.shopify.utils import (
	create_shopify_log,
	ensure_old_connector_is_disabled,
	migrate_from_old_connector,
)

_LOCATIONS_QUERY = """
query locations($first: Int!, $after: String) {
	locations(first: $first, after: $after) {
		edges {
			node {
				id
				name
			}
		}
		pageInfo {
			hasNextPage
			endCursor
		}
	}
}
"""


def _gid_to_id(gid) -> str:
	"""Extract the plain numeric id from a Shopify GraphQL global id, e.g.
	"gid://shopify/Location/123" -> "123". Used everywhere a numeric id is
	stored, matching the format the REST API used."""
	if not gid:
		return ""
	return str(gid).rsplit("/", 1)[-1]


class ShopifySetting(SettingController):
	def is_enabled(self) -> bool:
		return bool(self.enable_shopify)

	def validate(self):
		ensure_old_connector_is_disabled()

		if self.shopify_url:
			self.shopify_url = self.shopify_url.replace("https://", "").replace("http://", "")

		self._set_default_authentication_method()
		self._validate_authentication_change()
		self._validate_authentication_fields()
		self._handle_webhooks()
		self._validate_warehouse_links()
		self._initalize_default_values()

		if self.is_enabled():
			setup_custom_fields()

	def on_update(self):
		if self.is_enabled() and not self.is_old_data_migrated:
			migrate_from_old_connector()

	def before_save(self):
		"""Pre-generate OAuth token on save for better UX."""
		if not self.is_enabled() or self.authentication_method != AUTH_METHOD_OAUTH:
			return

		# _handle_webhooks (during validate) already minted a token for these
		# credentials in this save cycle — don't hit Shopify a second time.
		if self.flags.get("oauth_token_refreshed_during_save"):
			return

		credentials_changed = self.has_value_changed("client_id") or self.has_value_changed("client_secret")

		try:
			if credentials_changed:
				from shopify_integration.shopify.oauth import refresh_oauth_token

				refresh_oauth_token(self, client_secret=self._get_secret("client_secret"))
			elif not self._get_password_safe("oauth_access_token"):
				self._get_or_generate_oauth_token()
		except frappe.ValidationError:
			if credentials_changed:
				raise  # Bad credentials on rotation must block save
			create_shopify_log(
				status="Warning",
				method="shopify_integration.shopify.doctype.shopify_setting.shopify_setting.before_save",
				message=_("Token pre-generation failed on save; will retry on first sync."),
			)
		except Exception as e:
			create_shopify_log(
				status="Warning",
				method="shopify_integration.shopify.doctype.shopify_setting.shopify_setting.before_save",
				message=_("Token pre-generation failed on save; will retry on first sync."),
				exception=str(e),
			)

	# --- Authentication helpers ---

	def _set_default_authentication_method(self):
		"""Set default authentication method for existing documents."""
		if not self.authentication_method:
			self.authentication_method = AUTH_METHOD_STATIC

	def _validate_authentication_change(self):
		"""Prevent active credentials from diverging from remote webhook subscriptions.

		Disabling first removes subscriptions with the old credentials. The user can
		then save new credentials and enable the integration to register them again.
		"""
		previous = self.get_doc_before_save()
		if not previous or not previous.enable_shopify:
			return

		non_password_fields = (
			"authentication_method",
			"shopify_url",
			"shared_secret",
			"client_id",
		)
		if any(self.has_value_changed(fieldname) for fieldname in non_password_fields) or any(
			self._has_password_changed(fieldname, previous) for fieldname in ("password", "client_secret")
		):
			frappe.throw(
				_("Disable Shopify and save before changing the shop or authentication credentials."),
				title=_("Disable Shopify First"),
			)

	def _has_password_changed(self, fieldname: str, previous) -> bool:
		"""Return whether a newly entered secret differs from the stored value."""
		value = self.get(fieldname)
		if not value or self.is_dummy_password(value):
			return False

		return value != previous.get_password(fieldname, raise_exception=False)

	def _get_password_safe(self, fieldname: str) -> str:
		"""
		Safely get password field value without raising exceptions.
		Returns empty string if password doesn't exist or document is new.
		"""
		try:
			if not self.name or self.is_new():
				return ""

			password = self.get_password(fieldname, raise_exception=False)
			return password if password else ""
		except Exception:
			return ""

	def _get_secret(self, fieldname: str) -> str:
		"""
		Return the plaintext secret for a Password field.

		Uses the in-memory value when it is freshly entered (new or edited doc),
		otherwise reads the decrypted value from the encrypted store. Frappe masks
		saved Password fields to a dummy '*****' placeholder, so a naive
		``self.field or fallback`` would leak the dummy as the real secret on any
		re-save where the user did not retype it.
		"""
		value = self.get(fieldname)
		if value and not self.is_dummy_password(value):
			return value
		return self._get_password_safe(fieldname)

	def _validate_authentication_fields(self):
		"""Validate that required fields are present based on authentication method."""
		if not self.is_enabled():
			return

		if self.authentication_method == AUTH_METHOD_STATIC:
			password = self._get_secret("password")
			if not password:
				frappe.throw(_("Password / Access Token is required for Static Token authentication"))

			if not self.shared_secret:
				frappe.throw(_("Shared secret / API Secret is required for Static Token authentication"))

		elif self.authentication_method == AUTH_METHOD_OAUTH:
			if not self.client_id:
				frappe.throw(_("Client ID is required for OAuth 2.0 authentication"))

			client_secret = self._get_secret("client_secret")
			if not client_secret:
				frappe.throw(_("Client Secret is required for OAuth 2.0 authentication"))

	def _get_or_generate_oauth_token(self) -> str:
		"""Get existing valid OAuth token, or generate a new one."""
		from shopify_integration.shopify.oauth import is_token_valid, refresh_oauth_token

		current_token = self._get_password_safe("oauth_access_token")
		token_expiry = self.token_expires_at

		if current_token and is_token_valid(token_expiry):
			return current_token

		# Pass in-memory plaintext during validate/before_save (not yet in encrypted store)
		client_secret_raw = self._get_secret("client_secret")

		try:
			new_token = refresh_oauth_token(self, client_secret=client_secret_raw)
			return new_token
		except Exception as e:
			frappe.throw(
				_("Failed to generate OAuth token: {0}").format(str(e)),
				title=_("OAuth Authentication Error"),
			)

	# --- Webhooks ---

	def _handle_webhooks(self):
		"""Handle webhook registration/unregistration. Uses appropriate token based on auth method."""
		import requests

		if self.is_enabled() and not self.webhooks:
			if self.authentication_method == AUTH_METHOD_OAUTH:
				password = self._get_or_generate_oauth_token()
			else:
				password = self._get_secret("password")

			try:
				new_webhooks = connection.register_webhooks(self.shopify_url, password)
			except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
				create_shopify_log(
					status="Warning",
					method="shopify_integration.shopify.doctype.shopify_setting.shopify_setting._handle_webhooks",
					message=_(
						"Webhook registration failed due to network error. Disable and re-enable to retry."
					),
					exception=str(e),
				)
				return

			if self.authentication_method == AUTH_METHOD_OAUTH and len(new_webhooks) < len(WEBHOOK_EVENTS):
				# A short result usually means the token lacks the scopes its app
				# version now requires (scopes are fixed at token issue). Drop it so
				# the retry after fixing the app in the Dev Dashboard re-mints.
				from shopify_integration.shopify.oauth import invalidate_cached_token

				invalidate_cached_token(self)

			if not new_webhooks:
				msg = _("Failed to register webhooks with Shopify.") + "<br>"
				msg += (
					_(
						"Please check the credentials and the app's Admin API access scopes in"
						" the Shopify Dev Dashboard, then retry."
					)
					+ " "
				)
				msg += _("Disabling and re-enabling the integration might also help.")
				frappe.throw(msg)

			for webhook in new_webhooks:
				self.append(
					"webhooks", {"webhook_id": _gid_to_id(webhook.get("id")), "method": webhook.get("topic")}
				)

		elif not self.is_enabled():
			try:
				if self.authentication_method == AUTH_METHOD_OAUTH:
					password = self._get_or_generate_oauth_token()
				else:
					password = self._get_password_safe("password")

				if password:
					connection.unregister_webhooks(self.shopify_url, password)
			except Exception as e:
				# Disabling locally must not depend on Shopify being reachable. The
				# webhook endpoint also ignores deliveries while this setting is disabled.
				create_shopify_log(
					status="Warning",
					method=(
						"shopify_integration.shopify.doctype.shopify_setting."
						"shopify_setting._handle_webhooks"
					),
					message=_("Shopify was disabled, but remote webhooks could not be removed."),
					exception=str(e),
				)

			self.webhooks = list()  # remove all webhooks

	def _validate_warehouse_links(self):
		for wh_map in self.shopify_warehouse_mapping:
			if not wh_map.erpnext_warehouse:
				frappe.throw(_("ERPNext warehouse required in warehouse map table."))

	def _initalize_default_values(self):
		if not self.last_inventory_sync:
			self.last_inventory_sync = get_datetime("1970-01-01")

	@frappe.whitelist()
	@connection.temp_shopify_session
	def update_location_table(self):
		"""Fetch locations from shopify and add it to child table so user can
		map it with correct ERPNext warehouse."""

		self.shopify_warehouse_mapping = []

		cursor = None
		has_next_page = True

		while has_next_page:
			response = json.loads(GraphQL().execute(_LOCATIONS_QUERY, {"first": 250, "after": cursor}))
			locations_data = response.get("data", {}).get("locations", {})

			for edge in locations_data.get("edges", []):
				node = edge.get("node") or {}
				self.append(
					"shopify_warehouse_mapping",
					{
						"shopify_location_id": _gid_to_id(node.get("id")),
						"shopify_location_name": node.get("name"),
					},
				)

			page_info = locations_data.get("pageInfo", {})
			has_next_page = page_info.get("hasNextPage", False)
			cursor = page_info.get("endCursor")

	def get_erpnext_warehouses(self) -> list[ERPNextWarehouse]:
		return [wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping]

	def get_erpnext_to_integration_wh_mapping(self) -> dict[ERPNextWarehouse, IntegrationWarehouse]:
		return {
			wh_map.erpnext_warehouse: wh_map.shopify_location_id for wh_map in self.shopify_warehouse_mapping
		}

	def get_integration_to_erpnext_wh_mapping(self) -> dict[IntegrationWarehouse, ERPNextWarehouse]:
		return {
			wh_map.shopify_location_id: wh_map.erpnext_warehouse for wh_map in self.shopify_warehouse_mapping
		}


def setup_custom_fields():
	custom_fields = {
		"Item": [
			dict(
				fieldname=ITEM_SELLING_RATE_FIELD,
				label="Shopify Selling Rate",
				fieldtype="Currency",
				insert_after="standard_rate",
			)
		],
		"Customer": [
			dict(
				fieldname=CUSTOMER_ID_FIELD,
				label="Shopify Customer Id",
				fieldtype="Data",
				insert_after="series",
				read_only=1,
				print_hide=1,
			)
		],
		"Supplier": [
			dict(
				fieldname=SUPPLIER_ID_FIELD,
				label="Shopify Supplier Id",
				fieldtype="Data",
				insert_after="supplier_name",
				read_only=1,
				print_hide=1,
			)
		],
		"Address": [
			dict(
				fieldname=ADDRESS_ID_FIELD,
				label="Shopify Address Id",
				fieldtype="Data",
				insert_after="fax",
				read_only=1,
				print_hide=1,
			)
		],
		"Sales Order": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Order Item": [
			dict(
				fieldname=ORDER_ITEM_DISCOUNT_FIELD,
				label="Shopify Discount per unit",
				fieldtype="Float",
				insert_after="discount_and_margin",
				read_only=1,
			),
		],
		"Delivery Note": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_NUMBER_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=FULLFILLMENT_ID_FIELD,
				label="Shopify Fulfillment Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
		],
		"Sales Invoice": [
			dict(
				fieldname=ORDER_ID_FIELD,
				label="Shopify Order Id",
				fieldtype="Small Text",
				insert_after="title",
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_NUMBER_FIELD,
				label="Shopify Order Number",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
			dict(
				fieldname=ORDER_STATUS_FIELD,
				label="Shopify Order Status",
				fieldtype="Small Text",
				insert_after=ORDER_ID_FIELD,
				read_only=1,
				print_hide=1,
			),
		],
	}

	create_custom_fields(custom_fields)
