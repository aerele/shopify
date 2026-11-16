# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

import json
from unittest.mock import patch

import frappe

from shopify_integration.shopify.product import ShopifyProduct, _fetch_shopify_product

from .utils import TestCase


class TestProduct(TestCase):
	def test_sync_single_product(self):
		self.fake_graphql("single_product_gql")

		product = ShopifyProduct(product_id="6732194021530", variant_id="39933951901850")

		product.sync_product()

		self.assertTrue(product.is_synced())

		item = product.get_erpnext_item()

		self.assertEqual(frappe.get_last_doc("Item").item_code, item.item_code)

		ecommerce_item_exists = frappe.db.exists("Ecommerce Item", {"erpnext_item_code": item.name})
		self.assertTrue(bool(ecommerce_item_exists))

	def test_sync_product_with_variants(self):
		self.fake_graphql("variant_product_gql")

		product = ShopifyProduct(product_id="6704435495065")

		product.sync_product()

		self.assertTrue(product.is_synced())

		item = product.get_erpnext_item()  # should return template item
		self.assertTrue(bool(item.has_variants))
		self.assertEqual(item.name, str(product.product_id))

		required_variants = [
			"39845261443225",
			"39845261475993",
			"39845261508761",
			"39845261541529",
			"39845261574297",
			"39845261607065",
			"39845261639833",
			"39845261672601",
			"39845261705369",
		]

		variants = frappe.db.get_list("Item", filters={"variant_of": item.name})
		ecom_variants = frappe.db.get_list(
			"Ecommerce Item", filters={"variant_of": item.name}, fields="erpnext_item_code"
		)

		created_variants = [v.name for v in variants]
		created_ecom_variants = [e.erpnext_item_code for e in ecom_variants]

		self.assertEqual(len(variants), 9)  # 3 * 3
		self.assertEqual(sorted(required_variants), sorted(created_variants))

		self.assertEqual(len(created_ecom_variants), 9)
		self.assertEqual(sorted(required_variants), sorted(created_ecom_variants))

	def test_variant_id_mapping(self):
		template_item = make_item()
		from erpnext.controllers.item_variant import create_variant

		variant_LR = create_variant(
			template_item.item_code, {"Test Sync Size": "L", "Test Sync Colour": "Red"}
		)
		variant_MR = create_variant(
			template_item.item_code, {"Test Sync Size": "M", "Test Sync Colour": "Red"}
		)
		variant_LG = create_variant(
			template_item.item_code, {"Test Sync Size": "L", "Test Sync Colour": "Green"}
		)
		variant_MG = create_variant(
			template_item.item_code, {"Test Sync Size": "M", "Test Sync Colour": "Green"}
		)

		self.fake_graphql("variant_product_gql")
		product = ShopifyProduct(product_id="6704435495065", has_variants=1)
		product.sync_product()

		self.assertTrue(product.is_synced())

		self.fake_graphql("variant_product_gql")
		from shopify_integration.shopify.product import _fetch_shopify_product

		shopify_product = _fetch_shopify_product(product.product_id)

		from shopify_integration.shopify.product import map_erpnext_variant_to_shopify_variant

		self.assertEqual(
			map_erpnext_variant_to_shopify_variant(
				shopify_product, variant_LG, {"option1": "L", "option2": "Green"}
			),
			"39845261705369",
		)
		self.assertEqual(
			map_erpnext_variant_to_shopify_variant(
				shopify_product, variant_LR, {"option1": "L", "option2": "Red"}
			),
			"39845261639833",
		)
		self.assertEqual(
			map_erpnext_variant_to_shopify_variant(
				shopify_product, variant_MG, {"option1": "M", "option2": "Green"}
			),
			"39845261607065",
		)
		self.assertEqual(
			map_erpnext_variant_to_shopify_variant(
				shopify_product, variant_MR, {"option1": "M", "option2": "Red"}
			),
			"39845261541529",
		)


class TestVariantPagination(TestCase):
	PRODUCT_ID = "123"

	@staticmethod
	def _gql_variant_node(id):
		value = str(id)
		return {
			"id": f"gid://shopify/ProductVariant/{id}",
			"title": value,
			"sku": f"SKU-{value}",
			"price": "10.00",
			"inventoryItem": {"measurement": {"weight": {"value": 1, "unit": "GRAMS"}}},
			"selectedOptions": [{"name": "Test Sync Size", "value": value}],
		}

	@classmethod
	def _gql_product_response(cls, variant_ids, all_values, next_cursor):
		return {
			"data": {
				"product": {
					"id": f"gid://shopify/Product/{cls.PRODUCT_ID}",
					"title": "Paginated Product",
					"options": [{"name": "Test Sync Size", "values": all_values}],
					"variants": {
						"edges": [{"node": cls._gql_variant_node(i)} for i in variant_ids],
						"pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor},
					},
				}
			}
		}

	def test_fetch_shopify_product_follows_variant_pages(self):
		"""A product with more variants than one page is fetched whole."""
		calls = []
		all_values = [str(i) for i in range(258)]

		def fake_execute(graphql_self, query, variables=None, operation_name=None):
			calls.append(variables.get("variantsCursor"))
			cursor = variables.get("variantsCursor")
			if cursor is None:
				ids, next_cursor = range(250), "page-1"
			elif cursor == "page-1":
				ids, next_cursor = range(250, 258), None
			else:
				raise AssertionError(f"unexpected variants cursor: {cursor}")

			return json.dumps(self._gql_product_response(ids, all_values, next_cursor))

		with patch("shopify.resources.graphql.GraphQL.execute", fake_execute):
			product = _fetch_shopify_product(self.PRODUCT_ID)

		# the second page is requested with the first page's endCursor, then it stops
		self.assertEqual(calls, [None, "page-1"])
		self.assertEqual([v["id"] for v in product["variants"]], all_values)

	def test_sync_product_with_multiple_variant_pages(self):
		"""Every variant beyond the first page becomes an item."""
		all_values = ["1000", "1001", "1002", "1003", "1004"]

		def fake_execute(graphql_self, query, variables=None, operation_name=None):
			cursor = variables.get("variantsCursor")
			if cursor is None:
				ids, next_cursor = [1000, 1001, 1002], "page-1"
			elif cursor == "page-1":
				ids, next_cursor = [1003, 1004], None
			else:
				raise AssertionError(f"unexpected variants cursor: {cursor}")

			return json.dumps(self._gql_product_response(ids, all_values, next_cursor))

		product = ShopifyProduct(product_id=self.PRODUCT_ID)

		with patch("shopify.resources.graphql.GraphQL.execute", fake_execute):
			product.sync_product()

		self.assertTrue(product.is_synced())

		template = product.get_erpnext_item()
		self.assertTrue(bool(template.has_variants))

		variants = frappe.db.get_list("Item", filters={"variant_of": template.name})
		self.assertEqual(sorted(v.name for v in variants), sorted(all_values))


def create_item_attributes():
	if not frappe.db.exists("Item Attribute", "Test Sync Size"):
		frappe.get_doc(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Test Sync Size",
				"priority": 1,
				"item_attribute_values": [
					{"attribute_value": "XSL", "abbr": "XSL"},
					{"attribute_value": "S", "abbr": "S"},
					{"attribute_value": "M", "abbr": "M"},
					{"attribute_value": "L", "abbr": "L"},
					{"attribute_value": "XL", "abbr": "XL"},
					{"attribute_value": "2XL", "abbr": "2XL"},
				],
			}
		).insert()
	if not frappe.db.exists("Item Attribute", "Test Sync Colour"):
		frappe.get_doc(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Test Sync Colour",
				"priority": 2,
				"item_attribute_values": [
					{"attribute_value": "Red", "abbr": "R"},
					{"attribute_value": "Green", "abbr": "G"},
					{"attribute_value": "Blue", "abbr": "B"},
				],
			}
		).insert()


def make_item(item_code=None, properties=None):
	create_item_attributes()
	if not item_code:
		item_code = frappe.generate_hash(length=16)

	if frappe.db.exists("Item", item_code):
		return frappe.get_doc("Item", item_code)

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"description": item_code,
			"item_group": "Products",
			"attributes": [
				{"attribute": "Test Sync Size"},
				{"attribute": "Test Sync Colour"},
			],
			"has_variants": 1,
		}
	)

	if properties:
		item.update(properties)

	if item.is_stock_item:
		for item_default in [doc for doc in item.get("item_defaults") if not doc.default_warehouse]:
			item_default.default_warehouse = "_Test Warehouse - _TC"
			item_default.company = "_Test Company"
	item.insert()

	return item
