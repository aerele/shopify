# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tests for the Shopify adapter of the shared Product Sync page."""

import json
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import frappe
from ecommerce_core.product_import.adapter import ProductImportAdapter
from ecommerce_core.product_import.exceptions import TransientProviderError
from ecommerce_core.product_import.runner import FETCH_RETRIES, run_bulk_sync_job
from frappe.tests import UnitTestCase

from shopify_integration.product_import import ShopifyProductImportAdapter
from shopify_integration.shopify.constants import MODULE_NAME
from shopify_integration.shopify.product import _raise_on_graphql_errors
from shopify_integration.shopify.tests.utils import TestCase

ACCOUNT = "store-a.myshopify.com"
OTHER_ACCOUNT = "store-b.myshopify.com"
MODULE = "shopify_integration.product_import"


class TestShopifyProductImportAdapter(UnitTestCase):
	def setUp(self):
		self.adapter = ShopifyProductImportAdapter()
		patcher = patch(f"{MODULE}.get_default_account", return_value=SimpleNamespace(name=ACCOUNT))
		self.get_default_account = patcher.start()
		self.addCleanup(patcher.stop)

	def test_implements_the_product_import_contract(self):
		self.assertIsInstance(self.adapter, ProductImportAdapter)
		self.assertTrue(self.adapter.supports_listing)

	def test_enabled_follows_shopify_accounts(self):
		with patch(f"{MODULE}.get_enabled_accounts", return_value=[ACCOUNT]):
			self.assertTrue(self.adapter.is_enabled())
		with patch(f"{MODULE}.get_enabled_accounts", return_value=[]):
			self.assertFalse(self.adapter.is_enabled())

	def test_accounts_are_the_enabled_shopify_accounts(self):
		self.assertEqual(self.adapter.account_field, "shopify_account")
		with patch(f"{MODULE}.get_enabled_accounts", return_value=[ACCOUNT, OTHER_ACCOUNT]):
			self.assertEqual(
				self.adapter.get_accounts(),
				[{"name": ACCOUNT, "label": ACCOUNT}, {"name": OTHER_ACCOUNT, "label": OTHER_ACCOUNT}],
			)

	def test_listing_and_count_use_the_account(self):
		page = {"products": [], "next_cursor": None}
		with patch(f"{MODULE}._fetch_products_from_shopify", return_value=page) as fetch:
			self.adapter.get_products(limit=20, cursor="n:abc", search="shirt")
		fetch.assert_called_once_with(from_="n:abc", limit=20, query="title:*shirt*", shopify_account=ACCOUNT)

		with patch(f"{MODULE}.get_shopify_product_count", return_value=7) as count:
			self.assertEqual(self.adapter.get_provider_count(), 7)
		count.assert_called_once_with(shopify_account=ACCOUNT)

	def test_given_account_is_used_without_a_default(self):
		self.get_default_account.side_effect = frappe.ValidationError("More than one Shopify Account")
		page = {"products": [], "next_cursor": None}
		with patch(f"{MODULE}._fetch_products_from_shopify", return_value=page) as fetch:
			self.adapter.get_products(limit=20, account=OTHER_ACCOUNT)
		self.assertEqual(fetch.call_args.kwargs["shopify_account"], OTHER_ACCOUNT)

		with patch(f"{MODULE}.get_shopify_product_count", return_value=3) as count:
			self.assertEqual(self.adapter.get_provider_count(account=OTHER_ACCOUNT), 3)
		count.assert_called_once_with(shopify_account=OTHER_ACCOUNT)

		with (
			patch(f"{MODULE}.frappe.get_all", return_value=[]) as get_all,
			patch(f"{MODULE}._fetch_variant_ids", return_value=["11"]),
			patch(f"{MODULE}.ShopifyProduct") as shopify_product,
		):
			self.adapter.sync_product("42", account=OTHER_ACCOUNT)
		self.assertEqual(get_all.call_args.kwargs["filters"]["shopify_account"], OTHER_ACCOUNT)
		self.assertEqual(shopify_product.call_args.kwargs["shopify_account"], OTHER_ACCOUNT)

	def test_unmapped_product_is_created_in_one_sync(self):
		product = MagicMock()
		with (
			patch(f"{MODULE}.frappe.get_all", return_value=[]) as get_all,
			patch(f"{MODULE}._fetch_variant_ids", return_value=["11", "12"]) as fetch_ids,
			patch(f"{MODULE}.ShopifyProduct", return_value=product) as shopify_product,
		):
			self.assertEqual(self.adapter.sync_product(42), {"action": "created"})

		fetch_ids.assert_called_once_with("42")
		get_all.assert_called_once()
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"integration": "shopify", "shopify_account": ACCOUNT, "integration_item_code": "42"},
		)
		# one product-level sync, not one call per variant
		shopify_product.assert_called_once_with("42", shopify_account=ACCOUNT)
		product.sync_product.assert_called_once_with()
		product.sync_missing_variants.assert_not_called()

	def test_fully_mapped_product_is_skipped(self):
		rows = [
			frappe._dict(variant_id="", has_variants=1),
			frappe._dict(variant_id="11", has_variants=0),
			frappe._dict(variant_id="12", has_variants=0),
		]
		with (
			patch(f"{MODULE}.frappe.get_all", return_value=rows),
			patch(f"{MODULE}._fetch_variant_ids", return_value=["11", "12"]),
			patch(f"{MODULE}.ShopifyProduct") as shopify_product,
		):
			self.assertEqual(self.adapter.sync_product("42"), {"action": "skipped"})
		shopify_product.assert_not_called()

	def test_single_variant_product_mapped_without_template_is_skipped(self):
		rows = [frappe._dict(variant_id="11", has_variants=0)]
		with (
			patch(f"{MODULE}.frappe.get_all", return_value=rows),
			patch(f"{MODULE}._fetch_variant_ids", return_value=["11"]),
			patch(f"{MODULE}.ShopifyProduct") as shopify_product,
		):
			self.assertEqual(self.adapter.sync_product("42"), {"action": "skipped"})
		shopify_product.assert_not_called()

	def test_partly_mapped_product_syncs_missing_variants(self):
		rows = [
			frappe._dict(variant_id="", has_variants=1),
			frappe._dict(variant_id="11", has_variants=0),
		]
		product = MagicMock()
		with (
			patch(f"{MODULE}.frappe.get_all", return_value=rows),
			patch(f"{MODULE}._fetch_variant_ids", return_value=["11", "12"]),
			patch(f"{MODULE}.ShopifyProduct", return_value=product) as shopify_product,
		):
			self.assertEqual(self.adapter.sync_product("42"), {"action": "updated"})
		shopify_product.assert_called_once_with("42", shopify_account=ACCOUNT)
		product.sync_missing_variants.assert_called_once_with()
		product.sync_product.assert_not_called()

	def test_sync_of_missing_product_throws(self):
		with (
			patch(f"{MODULE}._fetch_variant_ids", return_value=None),
			patch(f"{MODULE}.ShopifyProduct") as shopify_product,
			self.assertRaises(frappe.ValidationError),
		):
			self.adapter.sync_product("42")
		shopify_product.assert_not_called()

	def test_several_enabled_accounts_are_not_guessed(self):
		self.get_default_account.side_effect = frappe.ValidationError("More than one Shopify Account")
		with patch(f"{MODULE}.ShopifyProduct") as shopify_product, self.assertRaises(frappe.ValidationError):
			self.adapter.sync_product("42")
		shopify_product.assert_not_called()


PRODUCT_MODULE = "shopify_integration.shopify.product"
PAGE_MODULE = "shopify_integration.shopify.page.shopify_import_products.shopify_import_products"
RUNNER_MODULE = "ecommerce_core.product_import.runner"
THROTTLED = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}
ACCESS_DENIED = {"errors": [{"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}]}


class FakeShopifyCatalog:
	"""Stands in for shopify.GraphQL: serves one product whose variants span several pages."""

	PAGE = 100

	def __init__(self, product_id: int, variant_count: int):
		self.product_id = product_id
		self.variant_count = variant_count
		self.list_responses: list[dict] = []
		self.calls: list[tuple[str, dict | None]] = []

	def __call__(self):  # GraphQL() -> instance with .execute
		return self

	def execute(self, query, variables=None):
		from shopify_integration.shopify import product as product_module
		from shopify_integration.shopify.page.shopify_import_products import (
			shopify_import_products as page_module,
		)

		variables = variables or {}
		if query is product_module._PRODUCT_QUERY:
			self.calls.append(("product", variables))
			response = {"data": {"product": self._product()}}
		elif query in (product_module._PRODUCT_VARIANT_PAGE_QUERY, page_module._PRODUCT_VARIANTS_QUERY):
			self.calls.append(("variants", variables))
			response = {"data": {"product": {"variants": self._variants(variables.get("after"))}}}
		elif query is page_module._PRODUCTS_LIST_QUERY:
			self.calls.append(("list", variables))
			response = self.list_responses.pop(0)
		else:
			raise AssertionError("unexpected GraphQL query")
		return json.dumps(response)

	def _variants(self, after=None):
		start = int(after[1:]) if after else 0
		end = min(start + self.PAGE, self.variant_count)
		return {
			"edges": [{"node": self._variant(i)} for i in range(start + 1, end + 1)],
			"pageInfo": {"hasNextPage": end < self.variant_count, "endCursor": f"c{end}"},
		}

	def _variant(self, i):
		return {
			"id": f"gid://shopify/ProductVariant/{self.variant_id(i)}",
			"title": f"V{i}",
			"sku": f"PIT-{self.product_id}-{i}",
			"price": "10.00",
			"inventoryItem": {"measurement": {"weight": {"value": 0.5, "unit": "KILOGRAMS"}}},
			"selectedOptions": [{"name": self.option, "value": f"V{i}"}],
		}

	def variant_id(self, i):
		return str(self.product_id * 1000 + i)

	@property
	def option(self):
		return f"PIT Size {self.product_id}"

	def _product(self):
		return {
			"id": f"gid://shopify/Product/{self.product_id}",
			"title": f"Paged Product {self.product_id}",
			"descriptionHtml": "",
			"productType": "",
			"vendor": "",
			"featuredImage": None,
			"options": [{"name": self.option, "values": [f"V{i}" for i in range(1, self.variant_count + 1)]}],
			"variants": self._variants(),
		}


class TestShopifyProductImportPagination(TestCase):
	"""Products with more than one page of variants, end to end through the adapter."""

	def setUp(self):
		super().setUp()
		self.adapter = ShopifyProductImportAdapter()
		self.product_id = int(frappe.generate_hash(length=6), 16)  # unique ids/SKUs per test

	def serve(self, variant_count: int) -> FakeShopifyCatalog:
		catalog = FakeShopifyCatalog(self.product_id, variant_count)
		for target in (PRODUCT_MODULE, PAGE_MODULE):
			patcher = patch(f"{target}.GraphQL", catalog)
			patcher.start()
			self.addCleanup(patcher.stop)
		return catalog

	def mapped_variants(self) -> set[str]:
		return set(
			frappe.get_all(
				"Ecommerce Item",
				filters={
					"integration": MODULE_NAME,
					"shopify_account": self.shopify_account,
					"integration_item_code": str(self.product_id),
					"has_variants": 0,
				},
				pluck="variant_id",
			)
		)

	def test_variants_beyond_the_first_page_are_created(self):
		catalog = self.serve(120)
		result = self.adapter.sync_product(self.product_id, account=self.shopify_account)

		self.assertEqual(result, {"action": "created"})
		self.assertEqual(self.mapped_variants(), {catalog.variant_id(i) for i in range(1, 121)})
		self.assertIn(
			("variants", {"id": f"gid://shopify/Product/{self.product_id}", "after": "c100"}), catalog.calls
		)

	def test_partly_mapped_product_gets_only_the_missing_variants(self):
		self.serve(100)
		self.assertEqual(
			self.adapter.sync_product(self.product_id, account=self.shopify_account), {"action": "created"}
		)
		before = self.mapped_variants()
		self.assertEqual(len(before), 100)
		templates_before = frappe.db.count(
			"Ecommerce Item", {"integration_item_code": str(self.product_id), "has_variants": 1}
		)

		catalog = self.serve(120)
		result = self.adapter.sync_product(self.product_id, account=self.shopify_account)

		self.assertEqual(result, {"action": "updated"})
		after = self.mapped_variants()
		self.assertEqual(len(after), 120)
		self.assertEqual(after - before, {catalog.variant_id(i) for i in range(101, 121)})
		self.assertEqual(
			frappe.db.count(
				"Ecommerce Item", {"integration_item_code": str(self.product_id), "has_variants": 1}
			),
			templates_before,
		)

	def test_fully_mapped_product_is_skipped(self):
		catalog = self.serve(120)
		self.adapter.sync_product(self.product_id, account=self.shopify_account)
		catalog.calls.clear()

		result = self.adapter.sync_product(self.product_id, account=self.shopify_account)

		self.assertEqual(result, {"action": "skipped"})
		self.assertNotIn("product", [kind for kind, _ in catalog.calls])  # only the variant ids were read
		self.assertEqual(len(self.mapped_variants()), 120)


class TestShopifyThrottling(TestCase):
	"""Shopify reports throttling as HTTP 200 + errors[].extensions.code == THROTTLED."""

	EMPTY_PAGE: ClassVar[dict] = {
		"data": {
			"products": {
				"edges": [],
				"pageInfo": {
					"hasNextPage": False,
					"hasPreviousPage": False,
					"endCursor": None,
					"startCursor": None,
				},
			}
		}
	}

	def setUp(self):
		super().setUp()
		self.catalog = FakeShopifyCatalog(1, 1)
		patches = {
			f"{PAGE_MODULE}.GraphQL": self.catalog,
			f"{RUNNER_MODULE}._require_lock": MagicMock(),
			f"{RUNNER_MODULE}._release_lock": MagicMock(),
			f"{RUNNER_MODULE}.ProgressPublisher": MagicMock(),
		}
		for target, value in patches.items():
			patcher = patch(target, value)
			patcher.start()
			self.addCleanup(patcher.stop)

		self.log = MagicMock()
		self.sleep = MagicMock()
		for target, value in (
			(f"{RUNNER_MODULE}.create_integration_log", self.log),
			(f"{RUNNER_MODULE}.sleep", self.sleep),
		):
			patcher = patch(target, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def run_job(self):
		return run_bulk_sync_job("shopify", account=self.shopify_account, force_full=True)

	def final_status(self):
		return self.log.call_args_list[-1].kwargs["status"]

	def test_graphql_errors_are_classified(self):
		with self.assertRaises(TransientProviderError):
			_raise_on_graphql_errors(THROTTLED, "products")
		with self.assertRaises(frappe.ValidationError) as ctx:
			_raise_on_graphql_errors(ACCESS_DENIED, "products")
		self.assertNotIsInstance(ctx.exception, TransientProviderError)
		_raise_on_graphql_errors({"data": {}}, "products")  # no errors -> no raise

	def test_throttled_page_is_retried(self):
		self.catalog.list_responses = [THROTTLED, self.EMPTY_PAGE]

		result = self.run_job()

		self.assertEqual(result["status"], "Success")
		self.assertEqual([kind for kind, _ in self.catalog.calls], ["list", "list"])
		self.sleep.assert_called_once()
		self.assertEqual(self.final_status(), "Success")

	def test_persistent_throttle_ends_the_job_in_error(self):
		self.catalog.list_responses = [THROTTLED] * (FETCH_RETRIES + 1)

		with self.assertRaises(TransientProviderError):
			self.run_job()

		self.assertEqual(len(self.catalog.calls), FETCH_RETRIES + 1)
		self.assertEqual(self.sleep.call_count, FETCH_RETRIES)
		self.assertEqual(self.final_status(), "Error")
		self.assertNotIn("Success", [c.kwargs.get("status") for c in self.log.call_args_list])

	def test_other_graphql_errors_are_not_retried(self):
		self.catalog.list_responses = [ACCESS_DENIED, self.EMPTY_PAGE]

		with self.assertRaises(frappe.ValidationError) as ctx:
			self.run_job()

		self.assertNotIsInstance(ctx.exception, TransientProviderError)
		self.assertEqual(len(self.catalog.calls), 1)
		self.sleep.assert_not_called()
		self.assertEqual(self.final_status(), "Error")

	def test_legacy_page_shows_a_clean_message_when_throttled(self):
		from shopify_integration.shopify.page.shopify_import_products.shopify_import_products import (
			get_shopify_products,
		)

		self.catalog.list_responses = [THROTTLED]
		with self.assertRaises(frappe.ValidationError) as ctx:
			get_shopify_products(shopify_account=self.shopify_account)
		self.assertNotIsInstance(ctx.exception, TransientProviderError)
		self.assertIn("rate limiting", str(ctx.exception))
