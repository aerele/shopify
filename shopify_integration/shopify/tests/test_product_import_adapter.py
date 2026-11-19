# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tests for the Shopify adapter of the shared Product Sync page."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from ecommerce_core.product_import.adapter import ProductImportAdapter
from frappe.tests import UnitTestCase

from shopify_integration.product_import import ShopifyProductImportAdapter

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
			patch(f"{MODULE}.is_synced", return_value=False) as synced,
			patch(f"{MODULE}._fetch_variant_ids", return_value=["11"]),
			patch(f"{MODULE}.ShopifyProduct") as shopify_product,
		):
			self.adapter.sync_product("42", account=OTHER_ACCOUNT)
		synced.assert_called_once_with("42", shopify_account=OTHER_ACCOUNT)
		self.assertEqual(shopify_product.call_args.kwargs["shopify_account"], OTHER_ACCOUNT)

	def test_sync_creates_every_variant_for_the_account(self):
		product = MagicMock()
		with (
			patch(f"{MODULE}.is_synced", return_value=False) as synced,
			patch(f"{MODULE}._fetch_variant_ids", return_value=["11", "12"]),
			patch(f"{MODULE}.ShopifyProduct", return_value=product) as shopify_product,
		):
			self.assertEqual(self.adapter.sync_product(42), {"action": "created"})

		synced.assert_called_once_with("42", shopify_account=ACCOUNT)
		self.assertEqual(
			[c.kwargs for c in shopify_product.call_args_list],
			[
				{"variant_id": "11", "shopify_account": ACCOUNT},
				{"variant_id": "12", "shopify_account": ACCOUNT},
			],
		)
		self.assertEqual(product.sync_product.call_count, 2)

	def test_sync_skips_product_already_synced_for_the_account(self):
		with (
			patch(f"{MODULE}.is_synced", return_value=True),
			patch(f"{MODULE}.ShopifyProduct") as shopify_product,
		):
			self.assertEqual(self.adapter.sync_product("42"), {"action": "skipped"})
		shopify_product.assert_not_called()

	def test_sync_of_missing_product_throws(self):
		with (
			patch(f"{MODULE}.is_synced", return_value=False),
			patch(f"{MODULE}._fetch_variant_ids", return_value=None),
			self.assertRaises(frappe.ValidationError),
		):
			self.adapter.sync_product("42")

	def test_several_enabled_accounts_are_not_guessed(self):
		self.get_default_account.side_effect = frappe.ValidationError("More than one Shopify Account")
		with patch(f"{MODULE}.ShopifyProduct") as shopify_product, self.assertRaises(frappe.ValidationError):
			self.adapter.sync_product("42")
		shopify_product.assert_not_called()
