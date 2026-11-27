# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tests for the Shopify dashboard adapter's declarations and feeds."""

import frappe
from ecommerce_core.dashboard.adapter import DashboardAdapter
from frappe.tests import IntegrationTestCase

from shopify_integration.shopify.dashboard import (
	ORDER_COLUMNS,
	ORDER_DETAILS,
	ORDER_FILTER_FIELDS,
	PAYMENT_COLUMNS,
	PAYMENT_DETAILS,
	ShopifyDashboardAdapter,
)
from shopify_integration.shopify.doctype.shopify_account.shopify_account import setup_custom_fields


class TestShopifyDashboardAdapter(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The feeds query Shopify custom fields (e.g. Sales Order.shopify_account); make sure they
		# exist regardless of which test ran before.
		setup_custom_fields()
		frappe.db.commit()

	def test_adapter_implements_the_dashboard_contract(self):
		adapter = ShopifyDashboardAdapter()
		self.assertIsInstance(adapter, DashboardAdapter)
		for method in (
			"get_recent_orders",
			"count_orders",
			"get_pending_payments",
			"count_pending_payments",
			"get_pending_summary",
			"get_order_columns",
			"get_order_filter_options",
			"get_payment_columns",
			"get_filter_field_map",
		):
			self.assertTrue(adapter.implements(method), method)

	def test_columns_cover_every_declared_detail(self):
		column_keys = {column["key"] for column in ORDER_COLUMNS}
		detail_keys = {fieldname for fieldname, _label in ORDER_DETAILS}
		self.assertEqual(column_keys, detail_keys)
		for column in ORDER_COLUMNS:
			self.assertTrue(column["label"])

	def test_payment_columns_cover_every_declared_detail(self):
		column_keys = {column["key"] for column in PAYMENT_COLUMNS}
		detail_keys = {fieldname for fieldname, _label in PAYMENT_DETAILS}
		self.assertEqual(column_keys, detail_keys)

	def test_shop_filter_targets_the_account_column(self):
		self.assertEqual(ORDER_FILTER_FIELDS, {"shop": "shopify_account"})
		self.assertIn(ORDER_FILTER_FIELDS["shop"], {fieldname for fieldname, _label in ORDER_DETAILS})

	def test_field_map_covers_every_filter_key(self):
		field_map = ShopifyDashboardAdapter().get_filter_field_map()
		self.assertEqual(set(field_map), set(ORDER_FILTER_FIELDS))
		self.assertEqual(
			{doctype for fields in field_map.values() for doctype in fields},
			{"Sales Order", "Sales Invoice", "Ecommerce Item", "Ecommerce Integration Log"},
		)

	def test_filter_options_are_well_formed_for_any_site(self):
		for option in ShopifyDashboardAdapter().get_order_filter_options():
			self.assertEqual(option["key"], "shop")
			self.assertTrue(option["label"])
			self.assertTrue(all(pair and pair[0] and pair[1] for pair in option["options"]))

	def test_recent_orders_is_a_list_for_any_site(self):
		self.assertIsInstance(ShopifyDashboardAdapter().get_recent_orders(limit=5), list)
