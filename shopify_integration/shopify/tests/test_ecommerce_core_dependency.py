# Copyright (c) 2026, Aerele and Contributors
# See LICENSE

"""Tests for PR1: shopify_integration depends on shared ecommerce_core.

These assert imports, required_apps, and basic ecommerce_core APIs used by Shopify.
They do not cover GraphQL / other feature PRs.
"""

import frappe
from frappe.tests import IntegrationTestCase


class TestEcommerceCoreDependency(IntegrationTestCase):
	def test_required_apps_includes_ecommerce_core(self):
		import shopify_integration.hooks as hooks

		self.assertTrue(hasattr(hooks, "required_apps"))
		self.assertIn("ecommerce_core", hooks.required_apps)

	def test_ecommerce_core_app_is_installed(self):
		self.assertIn("ecommerce_core", frappe.get_installed_apps())

	def test_import_ecommerce_customer_from_core(self):
		from ecommerce_core.controllers.customer import EcommerceCustomer

		self.assertTrue(callable(EcommerceCustomer) or EcommerceCustomer is not None)

	def test_import_inventory_helpers_from_core(self):
		from ecommerce_core.controllers.inventory import (
			get_inventory_levels,
			update_inventory_sync_status,
		)
		from ecommerce_core.controllers.scheduling import need_to_run

		self.assertTrue(callable(get_inventory_levels))
		self.assertTrue(callable(update_inventory_sync_status))
		self.assertTrue(callable(need_to_run))

	def test_import_setting_controller_from_core(self):
		from ecommerce_core.controllers.setting import SettingController

		self.assertTrue(SettingController is not None)

	def test_import_price_list_and_taxation_utils_from_core(self):
		from ecommerce_core.utils.price_list import get_dummy_price_list
		from ecommerce_core.utils.taxation import get_dummy_tax_category

		self.assertTrue(callable(get_dummy_price_list))
		self.assertTrue(callable(get_dummy_tax_category))

	def test_import_naming_series_from_core(self):
		from ecommerce_core.utils.naming_series import get_series

		self.assertTrue(callable(get_series))
		series = get_series()
		self.assertIsInstance(series, dict)

	def test_import_ecommerce_item_from_core(self):
		from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item

		self.assertTrue(hasattr(ecommerce_item, "is_synced"))
		self.assertTrue(hasattr(ecommerce_item, "get_erpnext_item"))
		self.assertTrue(hasattr(ecommerce_item, "create_ecommerce_item"))

	def test_import_create_log_from_core(self):
		from ecommerce_core.ecommerce_core.doctype.ecommerce_integration_log.ecommerce_integration_log import (
			create_log,
		)

		self.assertTrue(callable(create_log))

	def test_shopify_modules_import_from_ecommerce_core(self):
		"""Shopify modules must resolve shared pieces from ecommerce_core, not local copies."""
		import inspect

		import shopify_integration.shopify.customer as customer
		import shopify_integration.shopify.inventory as inventory
		import shopify_integration.shopify.order as order
		import shopify_integration.shopify.product as product
		import shopify_integration.shopify.utils as utils

		self.assertIn("ecommerce_core.controllers.customer", inspect.getsource(customer))
		self.assertIn("ecommerce_core.controllers.inventory", inspect.getsource(inventory))
		self.assertIn("ecommerce_core.controllers.scheduling", inspect.getsource(inventory))
		self.assertIn("ecommerce_core.utils.price_list", inspect.getsource(order))
		self.assertIn("ecommerce_core.utils.taxation", inspect.getsource(order))
		self.assertIn("ecommerce_core.ecommerce_core.doctype.ecommerce_item", inspect.getsource(product))
		self.assertIn(
			"ecommerce_core.ecommerce_core.doctype.ecommerce_integration_log",
			inspect.getsource(utils),
		)

	def test_ecommerce_item_doctype_owned_by_ecommerce_core_module(self):
		meta = frappe.get_meta("Ecommerce Item")
		self.assertEqual(meta.module, "Ecommerce Core")

	def test_ecommerce_integration_log_doctype_owned_by_ecommerce_core_module(self):
		meta = frappe.get_meta("Ecommerce Integration Log")
		self.assertEqual(meta.module, "Ecommerce Core")

	def test_create_log_writes_ecommerce_integration_log(self):
		from ecommerce_core.ecommerce_core.doctype.ecommerce_integration_log.ecommerce_integration_log import (
			create_log,
		)

		log = create_log(
			status="Success",
			message="ecommerce_core dependency test log",
			make_new=True,
		)
		self.assertTrue(log)
		name = log if isinstance(log, str) else getattr(log, "name", None)
		if name:
			self.assertTrue(frappe.db.exists("Ecommerce Integration Log", name))

	def test_shopify_setting_uses_core_setting_controller(self):
		from ecommerce_core.controllers.setting import SettingController

		from shopify_integration.shopify.doctype.shopify_setting.shopify_setting import (
			ShopifySetting,
		)

		self.assertTrue(issubclass(ShopifySetting, SettingController))
