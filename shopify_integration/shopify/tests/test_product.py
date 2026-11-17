# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

import frappe

from shopify_integration.shopify.product import (
	ShopifyProduct,
	_get_public_image_url,
	upload_item_image_to_shopify,
)

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

	def test_get_public_image_url(self):
		self.assertEqual(_get_public_image_url("https://example.com/img.jpg"), "https://example.com/img.jpg")
		self.assertIsNone(_get_public_image_url(None))
		self.assertIsNone(_get_public_image_url(""))
		# private files can't be fetched by Shopify's servers
		self.assertIsNone(_get_public_image_url("/private/files/img.jpg"))
		# protocol-relative and non-path values are rejected, not guessed at
		self.assertIsNone(_get_public_image_url("//example.com/img.jpg"))
		self.assertIsNone(_get_public_image_url("img.jpg"))
		# site-relative public files become absolute URLs
		url = _get_public_image_url("/files/img.jpg")
		self.assertTrue(url.startswith("http"))
		self.assertTrue(url.endswith("/files/img.jpg"))

	def test_upload_item_image_to_shopify(self):
		self.fake_graphql(
			{
				"data": {
					"productCreateMedia": {
						"media": [
							{
								"id": "gid://shopify/MediaImage/1",
								"alt": "ITEM-1",
								"image": {"url": "https://cdn.shopify.com/s/files/1.jpg"},
							}
						],
						"mediaUserErrors": [],
						"product": {"id": "gid://shopify/Product/123"},
					}
				}
			}
		)

		self.assertTrue(upload_item_image_to_shopify("123", "https://example.com/img.jpg", "ITEM-1"))

	def test_upload_item_image_to_shopify_skips_private_files(self):
		self.assertFalse(upload_item_image_to_shopify("123", "/private/files/img.jpg", "ITEM-1"))

	def test_upload_item_image_to_shopify_reports_shopify_errors(self):
		self.fake_graphql(
			{
				"data": {
					"productCreateMedia": {
						"media": [],
						"mediaUserErrors": [{"field": ["media"], "message": "Could not download the file"}],
						"product": {"id": "gid://shopify/Product/123"},
					}
				}
			}
		)

		self.assertFalse(upload_item_image_to_shopify("123", "https://example.com/missing.jpg", "ITEM-1"))


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
