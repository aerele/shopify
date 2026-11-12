# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Feeds the Ecommerce Core dashboard with Shopify orders and payments.

Registered through the ``ecommerce_dashboard_providers`` hook (see hooks.py).
Both feeds are thin reads over ERPNext data the connector has already stamped,
built on the shared helpers in ``ecommerce_core.dashboard.queries``: Sales
Orders carrying a Shopify order id, and submitted Sales Invoices with money
still outstanding. Order rows also carry the Shopify order number and the
platform-side order status as provider-neutral columns.
"""

import frappe
from ecommerce_core.dashboard import queries
from ecommerce_core.dashboard.adapter import DashboardAdapter

from shopify_integration.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)

# (fieldname, dashboard label) pairs on Sales Order; absent fields degrade quietly.
ORDER_DETAILS = [
	(ORDER_NUMBER_FIELD, "Order Number"),
	(ORDER_STATUS_FIELD, "Platform Status"),
]
# The same facts as columns in the shared orders table; key = details key (the fieldname).
ORDER_COLUMNS = [
	{"key": ORDER_NUMBER_FIELD, "label": "Order Number", "width": "110px"},
	{"key": ORDER_STATUS_FIELD, "label": "Platform Status", "width": "130px"},
]


class ShopifyDashboardAdapter(DashboardAdapter):
	def is_enabled(self) -> bool:
		return frappe.get_cached_doc(SETTING_DOCTYPE).is_enabled()

	def get_recent_orders(self, limit, search=None, status=None, filters=None):
		return queries.recent_sales_orders(
			ORDER_ID_FIELD, limit, search, status, extra_fields=ORDER_DETAILS
		)

	def count_orders(self, search=None, status=None, filters=None):
		return queries.count_sales_orders(ORDER_ID_FIELD, search, status)

	def get_order_columns(self):
		return ORDER_COLUMNS

	def get_pending_payments(self, limit, search=None, status=None):
		return queries.pending_sales_invoices(ORDER_ID_FIELD, limit, search, status)

	def count_pending_payments(self, search=None, status=None):
		return queries.count_pending_sales_invoices(ORDER_ID_FIELD, search, status)

	def get_pending_summary(self):
		return queries.pending_sales_invoice_summary(ORDER_ID_FIELD)
