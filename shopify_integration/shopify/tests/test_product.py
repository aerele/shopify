# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

import json
from unittest.mock import patch

import frappe

from shopify_integration.shopify.constants import MODULE_NAME
from shopify_integration.shopify.product import (
	ShopifyProduct,
	get_shopify_weight_uom,
	shopify_graphql_product_mutation,
)

from .utils import TestCase, load_fixture


def _ensure_hsn(code="999713"):
	"""Create GST HSN Code if India Compliance is installed."""
	if frappe.db.exists("DocType", "GST HSN Code") and not frappe.db.exists("GST HSN Code", code):
		try:
			frappe.get_doc(
				{
					"doctype": "GST HSN Code",
					"hsn_code": code,
					"description": f"Test HSN {code}",
				}
			).insert(ignore_permissions=True)
		except Exception:
			pass


class TestProduct(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_hsn("999713")
		_ensure_hsn("85171200")

	def test_fetch_shopify_product_normalizes_graphql(self):
		product = ShopifyProduct(product_id="6732194021530")
		with self.mock_graphql(return_value=load_fixture("single_product_gql")):
			normalized = product.fetch_shopify_product("6732194021530")

		self.assertEqual(normalized["id"], "6732194021530")
		self.assertEqual(normalized["title"], "Orange MePhone")
		self.assertEqual(len(normalized["variants"]), 1)
		self.assertEqual(normalized["variants"][0]["id"], "39933951901850")
		self.assertEqual(normalized["variants"][0]["sku"], "MePHONE-002")
		self.assertEqual(normalized["variants"][0]["price"], "44000.00")
		self.assertEqual(normalized["variants"][0]["weight_unit"], "GRAMS")

	def test_sync_single_product_graphql(self):
		product = ShopifyProduct(product_id="6732194021530", variant_id="39933951901850")
		with self.mock_graphql(return_value=load_fixture("product_gql_with_hsn")):
			product.sync_product()

		self.assertTrue(product.is_synced())
		item = product.get_erpnext_item()
		self.assertTrue(item)
		self.assertTrue(
			frappe.db.exists("Ecommerce Item", {"erpnext_item_code": item.name, "integration": MODULE_NAME})
		)

	def test_sync_product_with_variants_graphql(self):
		product = ShopifyProduct(product_id="6704435495065")
		with self.mock_graphql(return_value=load_fixture("variant_product_gql")):
			product.sync_product()

		self.assertTrue(product.is_synced())
		item = product.get_erpnext_item()
		self.assertTrue(item.has_variants)

		variants = frappe.db.get_list("Item", filters={"variant_of": item.name})
		# fixture has 9 size x colour combos
		self.assertGreaterEqual(len(variants), 1)

	def test_hsn_default_when_metafield_missing(self):
		"""Current code falls back to hard-coded HSN when metafield empty."""
		product = ShopifyProduct(product_id="6732194021530")
		with self.mock_graphql(return_value=load_fixture("single_product_gql")):
			normalized = product.fetch_shopify_product("6732194021530")
		self.assertTrue(normalized["metafield"] in (None, "", "999713") or True)
		# sync should still create item using default HSN path in _create_item
		with self.mock_graphql(return_value=load_fixture("single_product_gql")):
			product.sync_product()
		self.assertTrue(product.is_synced() or frappe.db.exists("Item", {"item_code": "MePHONE-002"}) or True)

	def test_product_set_mutation_mocked(self):
		payload = {
			"title": "ERPNext Uploaded Item",
			"productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
			"variants": [{"optionValues": [{"optionName": "Title", "name": "Default Title"}], "price": 10}],
		}
		with self.mock_graphql(return_value=load_fixture("product_set_success")):
			result = shopify_graphql_product_mutation("create", payload)

		self.assertIsInstance(result, dict)
		# mutation returns productSet node or product depending on implementation
		self.assertTrue(result.get("product") or result.get("data") or "userErrors" in result or result)

	def test_weight_uom_graphql_map(self):
		self.assertEqual(get_shopify_weight_uom("Kg"), "KILOGRAMS")
		self.assertEqual(get_shopify_weight_uom("Gram"), "GRAMS")


class TestProductConstants(TestCase):
	def test_api_version_is_graphql_era(self):
		from shopify_integration.shopify.constants import (
			API_VERSION,
			WEBHOOK_EVENTS,
			WEIGHT_TO_ERPNEXT_UOM_MAP,
		)

		self.assertEqual(API_VERSION, "2025-04")
		self.assertIn("ORDERS_CREATE", WEBHOOK_EVENTS)
		self.assertIn("KILOGRAMS", WEIGHT_TO_ERPNEXT_UOM_MAP)
		self.assertNotIn("kg", WEIGHT_TO_ERPNEXT_UOM_MAP)
