# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

from unittest import TestCase

from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from shopify_integration.shopify.order import _analyze_all_discounts


class TestOrder(IntegrationTestCase):
	def test_sync_with_variants(self):
		pass


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
