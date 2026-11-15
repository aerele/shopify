# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from shopify_integration.shopify.constants import ORDER_ID_FIELD, SETTING_DOCTYPE
from shopify_integration.shopify.order import _analyze_all_discounts, sync_sales_order
from shopify_integration.shopify.product import ShopifyProduct

from .utils import TestCase as ShopifyTestCase


class TestOrder(IntegrationTestCase):
	def test_sync_with_variants(self):
		pass


class TestSalesOrderSync(ShopifyTestCase):
	"""End-to-end: a webhook payload must produce a SUBMITTED Sales Order.

	Regression test: an indentation slip once made create_sales_order return
	an unsaved document while the log still said Success."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# custom field pre-created here, not mid-test: create_custom_fields
		# issues DDL, which implicitly commits the class transaction
		from shopify_integration.shopify import metafields

		metafields.ensure_metafield_custom_fields(
			[
				frappe._dict(
					shopify_resource="Order",
					namespace="custom",
					key="ordermetatest",
					erpnext_fieldname="ordermetatest",
				)
			]
		)

	def _save_setting(self, mapping_rows=None):
		setting = frappe.get_doc(SETTING_DOCTYPE)
		setting.metafield_mapping = []
		for row in mapping_rows or []:
			setting.append("metafield_mapping", row)
		if not any(t.shopify_tax == "IGST" for t in setting.taxes):
			setting.append("taxes", {"shopify_tax": "IGST", "tax_account": _ensure_tax_account()})
		with (
			patch(
				"shopify_integration.shopify.doctype.shopify_setting.shopify_setting."
				"ShopifySetting._handle_webhooks"
			),
			patch("shopify_integration.shopify.metafields.ensure_metafield_custom_fields"),
		):
			setting.save(ignore_permissions=True)

	def _sync_product(self):
		self.fake_graphql("single_product_gql")
		ShopifyProduct(product_id="6732194021530", variant_id="39933951901850").sync_product()

	def _cleanup(self, order_id):
		"""create_log() commits after every log write, so this class's data
		can outlive the class rollback - clean up everything fully."""
		name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: str(order_id)})
		if name:
			so = frappe.get_doc("Sales Order", name)
			if so.docstatus == 1:
				so.flags.ignore_permissions = True
				so.cancel()
			frappe.delete_doc("Sales Order", name, force=True, ignore_permissions=True)

		for ecom in frappe.get_all(
			"Ecommerce Item", filters={"integration_item_code": "6732194021530"}, pluck="name"
		):
			frappe.delete_doc("Ecommerce Item", ecom, force=True, ignore_permissions=True)
		if frappe.db.exists("Item", "6732194021530"):
			frappe.delete_doc("Item", "6732194021530", force=True, ignore_permissions=True)
		self._save_setting()

	def test_sales_order_is_created_and_submitted(self):
		self.addCleanup(self._cleanup, 99001)
		self._save_setting()
		self._sync_product()

		sync_sales_order(make_order_payload(99001))

		so = frappe.db.get_value(
			"Sales Order", {ORDER_ID_FIELD: "99001"}, ["name", "docstatus", "customer"], as_dict=True
		)
		self.assertIsNotNone(so, "Sales Order was not created")
		self.assertEqual(so.docstatus, 1, "Sales Order was not submitted")
		self.assertEqual(so.customer, "_Test Customer")

	def test_order_metafields_are_stamped_on_sales_order(self):
		self.addCleanup(self._cleanup, 99002)
		self._save_setting(
			mapping_rows=[
				{
					"shopify_resource": "Order",
					"namespace": "custom",
					"key": "ordermetatest",
					"erpnext_fieldname": "ordermetatest",
				}
			]
		)
		self._sync_product()
		self.fake_graphql(
			{
				"data": {
					"order": {
						"metafields": {
							"nodes": [
								{
									"namespace": "custom",
									"key": "ordermetatest",
									"value": "hello order",
									"type": "single_line_text_field",
								}
							]
						}
					}
				}
			}
		)

		sync_sales_order(make_order_payload(99002))

		so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: "99002"})
		self.assertTrue(so_name)
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "ordermetatest"), "hello order")

	def test_sync_order_metafields_refreshes_existing_sales_order(self):
		"""Metafields are often filled on the Shopify order only after
		creation - the refresh must update an already-created Sales Order."""
		from shopify_integration.shopify.metafields import sync_order_metafields

		self.addCleanup(self._cleanup, 99003)
		self._save_setting(
			mapping_rows=[
				{
					"shopify_resource": "Order",
					"namespace": "custom",
					"key": "ordermetatest",
					"erpnext_fieldname": "ordermetatest",
				}
			]
		)
		self._sync_product()
		self.fake_graphql({"data": {"order": {"metafields": {"nodes": []}}}})
		sync_sales_order(make_order_payload(99003))

		so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: "99003"})
		self.assertTrue(so_name)
		self.assertIsNone(frappe.db.get_value("Sales Order", so_name, "ordermetatest"))

		# the value is filled in Shopify afterwards; a refresh picks it up
		self.fake_graphql(
			{
				"data": {
					"order": {
						"metafields": {
							"nodes": [
								{
									"namespace": "custom",
									"key": "ordermetatest",
									"value": "filled later",
									"type": "single_line_text_field",
								}
							]
						}
					}
				}
			}
		)
		self.assertEqual(sync_order_metafields("99003"), so_name)
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "ordermetatest"), "filled later")

	def test_order_update_webhook_refreshes_metafields(self):
		"""Editing the metafield in Shopify after the order synced bumps the
		order (orders/updated webhook) - the handler re-queries the mapped
		values and stamps them onto the existing Sales Order."""
		from shopify_integration.shopify.metafields import handle_order_updated

		self.addCleanup(self._cleanup, 99004)
		self._save_setting(
			mapping_rows=[
				{
					"shopify_resource": "Order",
					"namespace": "custom",
					"key": "ordermetatest",
					"erpnext_fieldname": "ordermetatest",
				}
			]
		)
		self._sync_product()
		self.fake_graphql({"data": {"order": {"metafields": {"nodes": []}}}})
		sync_sales_order(make_order_payload(99004))

		so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: "99004"})
		self.assertTrue(so_name)
		self.assertIsNone(frappe.db.get_value("Sales Order", so_name, "ordermetatest"))

		# the value was filled in Shopify afterwards; the order's update
		# delivery carries no metafields, so the handler re-queries them
		self.fake_graphql(
			{
				"data": {
					"order": {
						"metafields": {
							"nodes": [
								{
									"namespace": "custom",
									"key": "ordermetatest",
									"value": "pushed by webhook",
									"type": "single_line_text_field",
								}
							]
						}
					}
				}
			}
		)
		handle_order_updated({"id": 99004})
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "ordermetatest"), "pushed by webhook")

		# an update for an order that was never synced is ignored
		handle_order_updated({"id": 88008})
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "ordermetatest"), "pushed by webhook")

	def test_scheduled_refresh_backfills_recent_orders(self):
		"""Missed webhook deliveries and mappings added after the order synced
		are covered by the hourly refresh of recent orders."""
		from shopify_integration.shopify.metafields import refresh_recent_order_metafields

		self.addCleanup(self._cleanup, 99005)
		self._save_setting(
			mapping_rows=[
				{
					"shopify_resource": "Order",
					"namespace": "custom",
					"key": "ordermetatest",
					"erpnext_fieldname": "ordermetatest",
				}
			]
		)
		self._sync_product()
		self.fake_graphql({"data": {"order": {"metafields": {"nodes": []}}}})
		sync_sales_order(make_order_payload(99005))

		so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: "99005"})
		self.assertIsNone(frappe.db.get_value("Sales Order", so_name, "ordermetatest"))

		self.fake_graphql(
			{
				"data": {
					"order": {
						"metafields": {
							"nodes": [
								{
									"namespace": "custom",
									"key": "ordermetatest",
									"value": "backfilled",
									"type": "single_line_text_field",
								}
							]
						}
					}
				}
			}
		)
		refresh_recent_order_metafields()
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "ordermetatest"), "backfilled")


def make_order_payload(order_id: int) -> dict:
	"""Minimal webhook-shaped order payload for the synced single product."""
	return {
		"id": order_id,
		"name": f"#{order_id}",
		"created_at": "2026-11-11 10:00:00",
		"financial_status": "pending",
		"taxes_included": 0,
		"cancelled_at": None,
		"note": None,
		"customer": None,
		"billing_address": None,
		"shipping_address": None,
		"discount_applications": [],
		"shipping_lines": [],
		"line_items": [
			{
				"id": 50933594915091,
				"name": "Orange MePhone",
				"product_id": 6732194021530,
				"variant_id": 39933951901850,
				"sku": "MePHONE-002",
				"price": "44000.00",
				"quantity": 1,
				"uom": "Nos",
				"product_exists": True,
				"tax_lines": [{"title": "IGST", "rate": 0.18, "price": "100.00"}],
				"discount_allocations": [],
			}
		],
	}


def _ensure_tax_account() -> str:
	name = "_Test Account Excise Duty - _TC"
	if frappe.db.exists("Account", name):
		return name
	from erpnext.accounts.doctype.account.test_account import create_account

	parent = frappe.db.get_value(
		"Account", {"company": "_Test Company", "account_name": "Direct Expenses"}
	) or frappe.db.get_value("Account", {"company": "_Test Company", "is_group": 1})
	return create_account(
		account_name="_Test Account Excise Duty", company="_Test Company", parent_account=parent
	)


class TestDiscountAllocation(TestCase):
	"""Test suite for discount allocation - all 6 scenarios"""

	def test_item_level_percentage_discount(self):
		"""Item-level percentage discount only"""
		shopify_order = {
			"discount_applications": [
				{
					"title": "10% Off Shirt",
					"allocation_method": "each",
					"target_selection": "entitled",
					"value_type": "percentage",
					"value": 0.10,
				}
			],
			"line_items": [
				{
					"id": "1",
					"sku": "SHIRT-001",
					"name": "Cotton Shirt",
					"quantity": 2,
					"price": 50.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 10.00, "discount_application_index": 0}],
				}
			],
		}

		result = _analyze_all_discounts(shopify_order)

		self.assertIsNone(result.get("order_level"))
		self.assertEqual(len(result.get("items", {})), 1)

		item_disc = result["items"].get("1")
		self.assertTrue(item_disc["has_item_level_discount"])
		self.assertEqual(item_disc["item_discount_type"], "percentage")
		self.assertEqual(item_disc["item_discount_value"], 0.10)
		self.assertEqual(item_disc["item_discount_amount"], 10.00)

	def test_item_level_fixed_amount_discount(self):
		"""Item-level fixed amount discount only"""
		shopify_order = {
			"discount_applications": [
				{
					"title": "$5 Off Pants",
					"allocation_method": "each",
					"target_selection": "entitled",
					"value_type": "fixed_amount",
					"value": 5.00,
				}
			],
			"line_items": [
				{
					"id": "2",
					"sku": "PANTS-001",
					"name": "Blue Pants",
					"quantity": 1,
					"price": 80.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 5.00, "discount_application_index": 0}],
				}
			],
		}

		result = _analyze_all_discounts(shopify_order)

		self.assertIsNone(result.get("order_level"))
		self.assertEqual(len(result.get("items", {})), 1)

		item_disc = result["items"].get("2")
		self.assertTrue(item_disc["has_item_level_discount"])
		self.assertEqual(item_disc["item_discount_type"], "fixed_amount")
		self.assertEqual(item_disc["item_discount_amount"], 5.00)

	def test_order_level_percentage_discount(self):
		"""Order-level percentage discount only"""
		shopify_order = {
			"discount_applications": [
				{
					"title": "15% Off Entire Order",
					"allocation_method": "across",
					"target_selection": "all",
					"value_type": "percentage",
					"value": 0.15,
				}
			],
			"line_items": [
				{
					"id": "3",
					"sku": "JACKET-001",
					"name": "Leather Jacket",
					"quantity": 1,
					"price": 200.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 30.00, "discount_application_index": 0}],
				},
				{
					"id": "4",
					"sku": "SHOES-001",
					"name": "Running Shoes",
					"quantity": 1,
					"price": 100.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 15.00, "discount_application_index": 0}],
				},
			],
		}

		result = _analyze_all_discounts(shopify_order)

		self.assertIsNotNone(result.get("order_level"))
		self.assertEqual(result["order_level"]["type"], "percentage")
		self.assertEqual(result["order_level"]["value"], 0.15)
		self.assertEqual(len(result.get("items", {})), 0)

	def test_order_level_fixed_amount_discount(self):
		"""Order-level fixed amount discount only"""
		shopify_order = {
			"discount_applications": [
				{
					"title": "$20 Off Order",
					"allocation_method": "across",
					"target_selection": "all",
					"value_type": "fixed_amount",
					"value": 20.00,
				}
			],
			"line_items": [
				{
					"id": "5",
					"sku": "HAT-001",
					"name": "Baseball Cap",
					"quantity": 2,
					"price": 25.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 10.00, "discount_application_index": 0}],
				},
				{
					"id": "6",
					"sku": "SOCKS-001",
					"name": "Sports Socks",
					"quantity": 1,
					"price": 10.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 10.00, "discount_application_index": 0}],
				},
			],
		}

		result = _analyze_all_discounts(shopify_order)

		self.assertIsNotNone(result.get("order_level"))
		self.assertEqual(result["order_level"]["type"], "fixed_amount")
		self.assertEqual(result["order_level"]["value"], 20.00)
		self.assertEqual(len(result.get("items", {})), 0)

	def test_mixed_item_and_order_percentage_discount(self):
		"""Mixed: Item-level + Order-level percentage discounts"""
		shopify_order = {
			"discount_applications": [
				{
					"title": "20% Off Shirts",
					"allocation_method": "each",
					"target_selection": "entitled",
					"value_type": "percentage",
					"value": 0.20,
				},
				{
					"title": "10% Off Entire Order",
					"allocation_method": "across",
					"target_selection": "all",
					"value_type": "percentage",
					"value": 0.10,
				},
			],
			"line_items": [
				{
					"id": "7",
					"sku": "TSHIRT-001",
					"name": "T-Shirt",
					"quantity": 2,
					"price": 40.00,
					"product_exists": True,
					"discount_allocations": [
						{"amount": 16.00, "discount_application_index": 0},
						{"amount": 8.00, "discount_application_index": 1},
					],
				},
				{
					"id": "8",
					"sku": "JEANS-001",
					"name": "Jeans",
					"quantity": 1,
					"price": 60.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 6.00, "discount_application_index": 1}],
				},
			],
		}

		result = _analyze_all_discounts(shopify_order)

		self.assertIsNotNone(result.get("order_level"))
		self.assertEqual(result["order_level"]["type"], "percentage")
		self.assertEqual(result["order_level"]["value"], 0.10)
		self.assertEqual(len(result.get("items", {})), 1)

		item_disc = result["items"].get("7")
		self.assertTrue(item_disc["has_item_level_discount"])
		self.assertEqual(item_disc["item_discount_type"], "percentage")
		self.assertEqual(item_disc["item_discount_value"], 0.20)
		self.assertEqual(item_disc["item_discount_amount"], 16.00)

	def test_mixed_item_and_order_fixed_amount_discount(self):
		"""Mixed: Item-level + Order-level fixed amount discounts"""
		shopify_order = {
			"discount_applications": [
				{
					"title": "$10 Off Each Shirt",
					"allocation_method": "each",
					"target_selection": "entitled",
					"value_type": "fixed_amount",
					"value": 10.00,
				},
				{
					"title": "$15 Off Order",
					"allocation_method": "across",
					"target_selection": "all",
					"value_type": "fixed_amount",
					"value": 15.00,
				},
			],
			"line_items": [
				{
					"id": "9",
					"sku": "SWEATER-001",
					"name": "Wool Sweater",
					"quantity": 1,
					"price": 75.00,
					"product_exists": True,
					"discount_allocations": [
						{"amount": 10.00, "discount_application_index": 0},
						{"amount": 7.50, "discount_application_index": 1},
					],
				},
				{
					"id": "10",
					"sku": "BELT-001",
					"name": "Leather Belt",
					"quantity": 1,
					"price": 35.00,
					"product_exists": True,
					"discount_allocations": [{"amount": 7.50, "discount_application_index": 1}],
				},
			],
		}

		result = _analyze_all_discounts(shopify_order)

		self.assertIsNotNone(result.get("order_level"))
		self.assertEqual(result["order_level"]["type"], "fixed_amount")
		self.assertEqual(result["order_level"]["value"], 15.00)
		self.assertEqual(len(result.get("items", {})), 1)

		item_disc = result["items"].get("9")
		self.assertTrue(item_disc["has_item_level_discount"])
		self.assertEqual(item_disc["item_discount_type"], "fixed_amount")
		self.assertEqual(item_disc["item_discount_amount"], 10.00)
