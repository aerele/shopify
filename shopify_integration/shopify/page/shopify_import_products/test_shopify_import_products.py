import json
import os
from unittest.mock import patch

import frappe

from shopify_integration.shopify.product import ShopifyProduct

from ...tests.utils import TestCase
from .shopify_import_products import (
	_build_product_search_query,
	get_shopify_products,
	queue_sync_all_products,
)

_WEIGHT_UNIT_TO_GQL = {"kg": "KILOGRAMS", "g": "GRAMS", "lb": "POUNDS", "oz": "OUNCES"}


def _rest_variant_to_gql_node(variant):
	selected_options = []
	for key in ("option1", "option2", "option3"):
		if variant.get(key):
			selected_options.append({"name": key, "value": variant[key]})

	return {
		"id": f"gid://shopify/ProductVariant/{variant['id']}",
		"title": variant.get("title"),
		"sku": variant.get("sku"),
		"price": variant.get("price"),
		"inventoryItem": {
			"measurement": {
				"weight": {
					"value": variant.get("weight"),
					"unit": _WEIGHT_UNIT_TO_GQL.get(variant.get("weight_unit"), "GRAMS"),
				}
			}
		},
		"selectedOptions": selected_options,
	}


def _rest_product_to_gql_detail_response(product):
	"""Convert a REST-shaped Shopify product fixture (as used by the old
	REST tests) into the GraphQL response shape `_fetch_shopify_product()`'s
	query expects, so the same underlying fixture data can drive both."""
	image = product.get("image") or {}

	return {
		"data": {
			"product": {
				"id": f"gid://shopify/Product/{product['id']}",
				"title": product.get("title"),
				"descriptionHtml": product.get("body_html"),
				"productType": product.get("product_type"),
				"vendor": product.get("vendor"),
				"featuredImage": {"url": image.get("src")} if image.get("src") else None,
				"options": [
					{"name": o.get("name"), "values": o.get("values")} for o in product.get("options", [])
				],
				"variants": {
					"edges": [{"node": _rest_variant_to_gql_node(v)} for v in product.get("variants", [])]
				},
			}
		}
	}


def _rest_products_to_gql_list_response(products):
	edges = []
	for product in products:
		variant_edges = [{"node": {"sku": v.get("sku")}} for v in product.get("variants", [])]
		edges.append(
			{
				"node": {
					"id": f"gid://shopify/Product/{product['id']}",
					"title": product.get("title"),
					"variants": {"edges": variant_edges},
				}
			}
		)

	return {
		"data": {
			"products": {
				"edges": edges,
				"pageInfo": {
					"hasNextPage": False,
					"hasPreviousPage": False,
					"startCursor": None,
					"endCursor": None,
				},
			}
		}
	}


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

		# Every GraphQL call goes to the same endpoint, so http_fake's
		# URL-based mocking (one static response per URL) can't distinguish
		# between the count/list/detail queries queue_sync_all_products()
		# issues in sequence. Instead, fake the SDK's execute() call itself
		# and route by query text, reusing the same REST-shaped fixture
		# data the old tests used.
		products_by_id = {str(p["id"]): p for p in self._products}

		def fake_execute(graphql_self, query, variables=None, operation_name=None):
			if "productsCount" in query:
				return json.dumps({"data": {"productsCount": {"count": len(self._products)}}})
			if "query products(" in query:
				return json.dumps(_rest_products_to_gql_list_response(self._products))
			if "query product(" in query:
				product_id = variables["id"].rsplit("/", 1)[-1]
				return json.dumps(_rest_product_to_gql_detail_response(products_by_id[product_id]))
			raise AssertionError(f"Unexpected GraphQL query in test_import_all_products: {query}")

		with patch("shopify.resources.graphql.GraphQL.execute", fake_execute):
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

	def test_build_product_search_query(self):
		self.assertIsNone(_build_product_search_query(None))
		self.assertIsNone(_build_product_search_query(""))
		self.assertIsNone(_build_product_search_query("   "))

		self.assertEqual(_build_product_search_query("shirt"), "title:*shirt*")

		self.assertEqual(
			_build_product_search_query("123456"),
			"title:*123456* OR id:123456",
		)

		# Shopify's search DSL treats `: " ( ) *` as syntax characters -
		# confirmed live that leaving them in silently breaks the query
		# instead of erroring, so they must be stripped before embedding.
		self.assertEqual(
			_build_product_search_query("(Sample) Coconut Bar Soap"),
			"title:*Sample Coconut Bar Soap*",
		)

	def test_get_shopify_products_with_search_term(self):
		captured = {}

		def fake_execute(graphql_self, query, variables=None, operation_name=None):
			captured["query"] = variables.get("query")
			return json.dumps(_rest_products_to_gql_list_response(self._products[:1]))

		with patch("shopify.resources.graphql.GraphQL.execute", fake_execute):
			get_shopify_products(search_term="shirt")

		self.assertEqual(captured["query"], "title:*shirt*")

	def test_get_shopify_products_page_size_clamped(self):
		captured = {}

		def fake_execute(graphql_self, query, variables=None, operation_name=None):
			captured["first"] = variables.get("first")
			return json.dumps(_rest_products_to_gql_list_response(self._products[:1]))

		with patch("shopify.resources.graphql.GraphQL.execute", fake_execute):
			get_shopify_products(limit=999)

		self.assertEqual(captured["first"], 20)
