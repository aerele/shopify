# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Feeds the Ecommerce Core dashboard with Shopify orders and payments."""

import frappe
from ecommerce_core.dashboard import queries
from ecommerce_core.dashboard.adapter import DashboardAdapter

from shopify_integration.shopify.constants import (
	ACCOUNT_DOCTYPE,
	ORDER_ACCOUNT_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
)
from shopify_integration.shopify.utils import get_enabled_accounts

# (fieldname, dashboard label) pairs on Sales Order.
ORDER_DETAILS = [
	(ORDER_ACCOUNT_FIELD, "Shop"),
	(ORDER_NUMBER_FIELD, "Order Number"),
	(ORDER_STATUS_FIELD, "Platform Status"),
]
# The same facts as columns in the orders table.
ORDER_COLUMNS = [
	{"key": ORDER_ACCOUNT_FIELD, "label": "Shop", "width": "200px"},
	{"key": ORDER_NUMBER_FIELD, "label": "Order Number", "width": "110px"},
	{"key": ORDER_STATUS_FIELD, "label": "Platform Status", "width": "130px"},
]
# Dashboard filter key -> Sales Order fieldname it filters on.
ORDER_FILTER_FIELDS = {"shop": ORDER_ACCOUNT_FIELD}
# (fieldname, dashboard label) pairs on pending Sales Invoices.
PAYMENT_DETAILS = [(ORDER_ACCOUNT_FIELD, "Shop")]
PAYMENT_COLUMNS = [{"key": ORDER_ACCOUNT_FIELD, "label": "Shop", "width": "200px"}]
# Where the shop filter also applies: tiles, items and sync logs read these fields.
FILTER_FIELD_MAP = {
	"shop": {
		"Sales Order": ORDER_ACCOUNT_FIELD,
		"Sales Invoice": ORDER_ACCOUNT_FIELD,
		"Ecommerce Item": ORDER_ACCOUNT_FIELD,
		"Ecommerce Integration Log": ORDER_ACCOUNT_FIELD,
	}
}


def _distinct_shops() -> list[str]:
	"""Distinct store accounts seen on imported orders, for the Shop filter."""
	return sorted(
		value
		for value in frappe.get_all(
			"Sales Order",
			filters={ORDER_ACCOUNT_FIELD: ("is", "set")},
			pluck=ORDER_ACCOUNT_FIELD,
			distinct=True,
		)
		if value
	)


class ShopifyDashboardAdapter(DashboardAdapter):
	def is_enabled(self) -> bool:
		# Any enabled store turns the dashboard feed on.
		return bool(get_enabled_accounts())

	def get_erpnext_warehouses(self) -> list[str]:
		"""Stock spans every enabled store's warehouses."""
		warehouses = []
		for account in get_enabled_accounts():
			warehouses += frappe.get_cached_doc(ACCOUNT_DOCTYPE, account).get_erpnext_warehouses() or []
		return warehouses

	def get_recent_orders(self, limit, search=None, status=None, filters=None):
		return queries.recent_sales_orders(
			ORDER_ID_FIELD,
			limit,
			search,
			status,
			extra_fields=ORDER_DETAILS,
			filter_fields=ORDER_FILTER_FIELDS,
			filters=filters,
		)

	def count_orders(self, search=None, status=None, filters=None):
		return queries.count_sales_orders(
			ORDER_ID_FIELD, search, status, filter_fields=ORDER_FILTER_FIELDS, filters=filters
		)

	def get_order_filter_options(self):
		values = _distinct_shops()
		if not values:
			return []
		return [{"key": "shop", "label": "Shop", "options": [(value, value) for value in values]}]

	def get_order_columns(self):
		return ORDER_COLUMNS

	def get_pending_payments(self, limit, search=None, status=None):
		return queries.pending_sales_invoices(
			ORDER_ID_FIELD, limit, search, status, extra_fields=PAYMENT_DETAILS
		)

	def get_payment_columns(self):
		return PAYMENT_COLUMNS

	def get_filter_field_map(self):
		return FILTER_FIELD_MAP

	def count_pending_payments(self, search=None, status=None):
		return queries.count_pending_sales_invoices(ORDER_ID_FIELD, search, status)

	def get_pending_summary(self, filters=None):
		return queries.pending_sales_invoice_summary(
			ORDER_ID_FIELD, filter_fields=ORDER_FILTER_FIELDS, filters=filters
		)
