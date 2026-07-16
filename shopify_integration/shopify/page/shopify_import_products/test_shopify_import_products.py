import json
import os

import frappe

from shopify_integration.shopify.product import ShopifyProduct

from ...tests.utils import TestCase, rest_product_to_graphql_node
from .shopify_import_products import queue_sync_all_products


class TestShopifyImportProducts(TestCase):
	def __init__(self, obj):
		with open(os.path.join(os.path.dirname(__file__), "../../tests/data/bulk_products.json"), "rb") as f:
			products_json = json.loads(f.read())
			self._products = products_json["products"]

		super().__init__(obj)

	def test_import_all_products(self):
		required_products = {
			"6808908169263": [
				"40279118250031",
				"40279118282799",
				"40279118315567",
				"40279118348335",
				"40279118381103",
				"40279118413871",
			],
			"6808928124975": [
				"40279218028591",
				"40279218061359",
				"40279218094127",
				"40279218126895",
			],
			"6808887689263": ["40279042883631", "40279042916399", "40279042949167"],
			"6808908955695": ["40279122673711", "40279122706479", "40279122739247"],
			"6808917737519": ["40279168221231", "40279168253999", "40279168286767"],
			"6808921735215": [
				"40279189323823",
				"40279189356591",
				"40279189389359",
				"40279189422127",
				"40279189454895",
			],
			"6808907317295": ["40279113826351", "40279113859119"],
			"6808873467951": [
				"40278994944047",
				"40278994976815",
				"40278995009583",
				"40278995042351",
				"40278995075119",
			],
			"6808929337391": ["40279220551727"],
			"6808929304623": ["40279220518959"],
		}

		self.fake_graphql(self._graphql_resolver)

		queue_sync_all_products()

		for product, required_variants in required_products.items():
			# has_variants is needed to avoid get_erpnext_item()
			# fetching the variant instead of template because of
			# matching integration_item_code
			shopify_product = ShopifyProduct(
				product_id=product, has_variants=1 if bool(required_variants) else 0
			)

			# product is synced
			self.assertTrue(shopify_product.is_synced())

			item = shopify_product.get_erpnext_item()

			self.assertEqual(bool(item.has_variants), bool(required_variants))
			# self.assertEqual(item.name, str(shopify_product.product_id))

			variants = frappe.db.get_list("Item", filters={"variant_of": item.name})
			ecom_variants = frappe.db.get_list(
				"Ecommerce Item", filters={"variant_of": item.name}, fields="erpnext_item_code"
			)

			created_variants = [v.name for v in variants]
			created_ecom_variants = [e.erpnext_item_code for e in ecom_variants]

			# variants are created right
			self.assertEqual(sorted(required_variants), sorted(created_variants))

			self.assertEqual(len(created_ecom_variants), len(required_variants))
			self.assertEqual(sorted(required_variants), sorted(created_ecom_variants))

	def _graphql_resolver(self, query, variables):
		if "productsCount" in query:
			return {"data": {"productsCount": {"count": len(self._products)}}}

		if "product(id: $id)" in query:
			product_id = variables["id"].split("/")[-1]
			product = next(p for p in self._products if str(p["id"]) == product_id)
			return {"data": {"product": rest_product_to_graphql_node(product)}}

		# paginated product-list query
		return {
			"data": {
				"products": {
					"edges": [{"node": rest_product_to_graphql_node(p)} for p in self._products],
					"pageInfo": {
						"hasNextPage": False,
						"hasPreviousPage": False,
						"startCursor": None,
						"endCursor": None,
					},
				}
			}
		}
