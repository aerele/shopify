"""Shared test helpers for shopify_integration (GraphQL-first).

Mocks ``shopify.GraphQL.execute`` so CI does not need a live Shopify shop.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

import frappe
import shopify
from frappe.tests import IntegrationTestCase
from frappe.tests.utils import make_test_records

from shopify_integration.shopify.constants import API_VERSION, SETTING_DOCTYPE

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_fixture(name: str, as_json: bool = True):
	"""Load a file from tests/data/. ``name`` may omit ``.json``."""
	if not name.endswith((".json", ".txt")):
		path = os.path.join(FIXTURE_DIR, f"{name}.json")
	else:
		path = os.path.join(FIXTURE_DIR, name)

	with open(path, encoding="utf-8") as f:
		raw = f.read()
	if as_json:
		return json.loads(raw)
	return raw


def fixture_json_string(name: str) -> str:
	"""Return fixture file contents as a string (for GraphQL.execute return values)."""
	data = load_fixture(name, as_json=True)
	return json.dumps(data)


@contextmanager
def mock_graphql(return_value=None, side_effect=None):
	"""Patch ``shopify.GraphQL.execute``.

	``return_value`` may be a dict (serialized to JSON str) or a str.
	``side_effect`` may be a callable or list (same as mock.patch).
	"""
	if side_effect is not None:
		kwargs = {"side_effect": side_effect}
	else:
		if isinstance(return_value, dict):
			payload = json.dumps(return_value)
		elif return_value is None:
			payload = json.dumps({"data": {}})
		else:
			payload = return_value
		kwargs = {"return_value": payload}

	# GraphQL() builds endpoint from ShopifyResource site; provide a dummy site.
	shopify.ShopifyResource.site = f"https://frappetest.myshopify.com/admin/api/{API_VERSION}"

	with patch.object(shopify.GraphQL, "execute", **kwargs) as mock_exec:
		yield mock_exec


@contextmanager
def mock_shopify_session():
	"""No-op Session.temp for code paths that open a real session outside in_test skip."""

	class _DummyCtx:
		def __enter__(self):
			return self

		def __exit__(self, *args):
			return False

	with patch("shopify.Session.temp", return_value=_DummyCtx()):
		yield


def _ensure_erpnext_test_masters():
	"""Create ERPNext ``_Test *`` masters used by Shopify Setting links.

	``IntegrationTestCase`` only auto-loads records for the test's own doctype /
	module overrides. CI sites often have no ``_Test Company`` / warehouses yet,
	so we explicitly seed the doctypes Shopify Setting links to.
	"""
	for doctype in (
		"Company",
		"Customer",
		"Customer Group",
		"Supplier",
		"Supplier Group",
		"Item",
		"Item Group",
		"Warehouse",
		"Account",
		"Cost Center",
		"Price List",
		"Currency",
		"UOM",
	):
		try:
			make_test_records(doctype)
		except Exception:
			# Some doctypes may not ship test records in every ERPNext version.
			frappe.logger().debug("make_test_records skipped/failed for %s", doctype)

	# Weight UOMs referenced by imported products (WEIGHT_TO_ERPNEXT_UOM_MAP) must
	# exist, otherwise the Item insert fails on a bare CI site.
	for uom in ("Gram", "Kg", "Lb", "Oz", "Nos"):
		if not frappe.db.exists("UOM", uom):
			try:
				frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)
			except Exception:
				frappe.logger().debug("could not create UOM %s", uom)


def _first_existing(doctype: str, preferred: str | None = None) -> str | None:
	if preferred and frappe.db.exists(doctype, preferred):
		return preferred
	return frappe.db.get_value(doctype, {}, "name")


def _configure_shopify_setting_for_tests():
	"""Fill Shopify Setting with valid links (or ignore missing ones)."""
	from erpnext import get_default_cost_center

	company = _first_existing("Company", "_Test Company")

	# Item creation via ecommerce_core.create_ecommerce_item builds item_defaults
	# from get_default_company(); on a bare CI site that is unset, so the Item
	# insert fails silently and the product is never marked synced. Seed the
	# company default (both the user/global "company" default and Global Defaults)
	# so get_default_company() resolves the seeded test company, which carries the
	# default income/expense accounts the Item needs.
	if company:
		frappe.db.set_single_value("Global Defaults", "default_company", company)
		frappe.defaults.set_global_default("company", company)
	customer = _first_existing("Customer", "_Test Customer")
	customer_group = _first_existing("Customer Group", "_Test Customer Group 1") or _first_existing(
		"Customer Group", "All Customer Groups"
	)
	warehouse = _first_existing("Warehouse", "_Test Warehouse - _TC")
	warehouse_1 = _first_existing("Warehouse", "_Test Warehouse 1 - _TC") or warehouse
	warehouse_2 = _first_existing("Warehouse", "_Test Warehouse 2 - _TC") or warehouse
	price_list = _first_existing("Price List", "_Test Price List") or _first_existing(
		"Price List", "Standard Selling"
	)

	cash_bank = None
	if company:
		cash_bank = frappe.db.get_value(
			"Account",
			{"account_type": "Bank", "company": company, "is_group": 0},
			"name",
		) or frappe.db.get_value(
			"Account",
			{"account_type": "Cash", "company": company, "is_group": 0},
			"name",
		)
		if frappe.db.exists("Account", "_Test Bank - _TC"):
			cash_bank = "_Test Bank - _TC"

	cost_center = None
	if company:
		try:
			cost_center = get_default_cost_center(company)
		except Exception:
			cost_center = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")

	values = {
		"enable_shopify": 1,
		"shopify_url": "frappetest.myshopify.com",
		"password": "supersecret",
		"shared_secret": "supersecret",
		"sales_order_series": "SAL-ORD-.YYYY.-",
		"sync_delivery_note": 1,
		"delivery_note_series": "MAT-DN-.YYYY.-",
		"sync_sales_invoice": 1,
		"sales_invoice_series": "SINV-.YY.-",
		"upload_erpnext_items": 1,
		"update_shopify_item_on_update": 1,
		"update_erpnext_stock_levels_to_shopify": 1,
	}
	if customer:
		values["default_customer"] = customer
	if customer_group:
		values["customer_group"] = customer_group
	if company:
		values["company"] = company
	if cost_center:
		values["cost_center"] = cost_center
	if cash_bank:
		values["cash_bank_account"] = cash_bank
	if price_list:
		values["price_list"] = price_list
	if warehouse:
		values["warehouse"] = warehouse

	wh_rows = []
	if warehouse_1:
		wh_rows.append(
			{
				"shopify_location_id": "62279942297",
				"shopify_location_name": "WH 1",
				"erpnext_warehouse": warehouse_1,
			}
		)
	if warehouse_2:
		wh_rows.append(
			{
				"shopify_location_id": "61724295321",
				"shopify_location_name": "WH 2",
				"erpnext_warehouse": warehouse_2,
			}
		)
	if wh_rows:
		values["shopify_warehouse_mapping"] = wh_rows

	with patch(
		"shopify_integration.shopify.doctype.shopify_setting.shopify_setting.ShopifySetting._handle_webhooks"
	):
		setting = frappe.get_doc(SETTING_DOCTYPE)
		setting.update(values)
		# CI / bare sites: never fail setUpClass solely on missing masters
		setting.flags.ignore_links = True
		setting.flags.ignore_mandatory = True
		setting.flags.ignore_permissions = True
		setting.save(ignore_permissions=True)


class TestCase(IntegrationTestCase):
	"""Base class: Shopify Setting + GraphQL-friendly environment."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_erpnext_test_masters()
		_configure_shopify_setting_for_tests()

	def setUp(self):
		shopify.ShopifyResource.clear_session()
		shopify.ShopifyResource.site = f"https://frappetest.myshopify.com/admin/api/{API_VERSION}"

	def load_fixture(self, name: str, as_json: bool = True):
		return load_fixture(name, as_json=as_json)

	def mock_graphql(self, return_value=None, side_effect=None):
		return mock_graphql(return_value=return_value, side_effect=side_effect)

	def mock_shopify_session(self):
		return mock_shopify_session()
