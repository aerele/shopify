import json
import os
import sys
from unittest.mock import patch

import frappe
import shopify
from erpnext import get_default_cost_center
from frappe.tests import IntegrationTestCase
from pyactiveresource.activeresource import ActiveResource
from pyactiveresource.testing import http_fake

from shopify_integration.shopify.constants import API_VERSION, SETTING_DOCTYPE

# Following code is adapted from Shopify python api under MIT license with minor changes.

# Copyright (c) 2011 "JadedPixel inc."

# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:

# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.


class TestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		# Shopify Setting is a Single doctype, so IntegrationTestCase's automatic
		# dependency-record generation (which only applies to non-Single doctypes)
		# never walks its link fields. Explicitly ensure Customer's test records
		# exist — generating them pulls in Company, Warehouse, Customer Group,
		# and Account test fixtures too (_Test Company, _Test Warehouse - _TC,
		# etc.), which the Shopify Setting test data below hard-codes references to.
		from frappe.tests.utils import make_test_records

		make_test_records("Customer")

		# Now setup Shopify settings with test data
		with patch(
			"shopify_integration.shopify.doctype.shopify_setting.shopify_setting.ShopifySetting._handle_webhooks"
		):
			setting = frappe.get_doc(SETTING_DOCTYPE)

			setting.update(
				{
					"enable_shopify": 1,
					"shopify_url": "frappetest.myshopify.com",
					"password": "supersecret",
					"shared_secret": "supersecret",
					"default_customer": "_Test Customer",
					"customer_group": "_Test Customer Group 1",
					"company": "_Test Company",
					"cost_center": get_default_cost_center("_Test Company"),
					"cash_bank_account": "_Test Bank - _TC",
					"price_list": "_Test Price List",
					"warehouse": "_Test Warehouse - _TC",
					"sales_order_series": "SAL-ORD-.YYYY.-",
					"sync_delivery_note": 1,
					"delivery_note_series": "MAT-DN-.YYYY.-",
					"sync_sales_invoice": 1,
					"sales_invoice_series": "SINV-.YY.-",
					"upload_erpnext_items": 1,
					"update_shopify_item_on_update": 1,
					"update_erpnext_stock_levels_to_shopify": 1,
					"doctype": "Shopify Setting",
					"shopify_warehouse_mapping": [
						{
							"shopify_location_id": "62279942297",
							"shopify_location_name": "WH 1",
							"erpnext_warehouse": "_Test Warehouse 1 - _TC",
						},
						{
							"shopify_location_id": "61724295321",
							"shopify_location_name": "WH 2",
							"erpnext_warehouse": "_Test Warehouse 2 - _TC",
						},
					],
				}
			).save(ignore_permissions=True)

	def setUp(self):
		ActiveResource.site = None
		ActiveResource.headers = None

		shopify.ShopifyResource.clear_session()
		shopify.ShopifyResource.site = f"https://frappetest.myshopify.com/admin/api/{API_VERSION}"
		shopify.ShopifyResource.password = None
		shopify.ShopifyResource.user = None

		http_fake.initialize()
		self.http = http_fake.TestHandler
		self.http.set_response(Exception("Bad request"))
		self.http.site = "https://frappetest.myshopify.com"

	def load_fixture(self, name, format="json"):
		with open(os.path.dirname(__file__) + f"/data/{name}.{format}", "rb") as f:
			return f.read()

	def fake_graphql(self, resolver):
		"""Mock `shopify.GraphQL().execute()` for GraphQL-based endpoints.

		`resolver` is a callable `(query, variables) -> dict` that returns the
		GraphQL response body (as a dict) for a given call; it's inspected
		per-call so callers don't need to hard-code an exact call order.
		"""

		def side_effect(query, variables=None, operation_name=None):
			return json.dumps(resolver(query, variables))

		patcher = patch("shopify.GraphQL.execute", side_effect=side_effect)
		self.addCleanup(patcher.stop)
		return patcher.start()

	def fake(self, endpoint, **kwargs):
		body = kwargs.pop("body", None) or self.load_fixture(endpoint)
		method = kwargs.pop("method", "GET")
		prefix = kwargs.pop("prefix", f"/admin/api/{API_VERSION}")

		if "extension" in kwargs and not kwargs["extension"]:
			extension = ""
		else:
			extension = ".{}".format(kwargs.pop("extension", "json"))

		url = f"https://frappetest.myshopify.com{prefix}/{endpoint}{extension}"
		try:
			url = kwargs["url"]
		except KeyError:
			pass

		headers = {}
		if kwargs.pop("has_user_agent", True):
			userAgent = "ShopifyPythonAPI/{} Python/{}".format(shopify.VERSION, sys.version.split(" ", 1)[0])
			headers["User-agent"] = userAgent

		try:
			headers.update(kwargs["headers"])
		except KeyError:
			pass

		code = kwargs.pop("code", 200)

		self.http.respond_to(
			method,
			url,
			headers,
			body=body,
			code=code,
			response_headers=kwargs.pop("response_headers", None),
		)


REST_TO_GRAPHQL_WEIGHT_UNIT = {
	"g": "GRAMS",
	"kg": "KILOGRAMS",
	"oz": "OUNCES",
	"lb": "POUNDS",
}


def rest_variant_to_graphql_node(variant):
	"""Convert a REST-shaped variant dict (as found in test fixtures) into the
	GraphQL `ProductVariant` node shape our GraphQL queries/mutations consume."""
	selected_options = []
	for i in (1, 2, 3):
		value = variant.get(f"option{i}")
		if value:
			selected_options.append({"name": f"option{i}", "value": value})

	return {
		"id": f"gid://shopify/ProductVariant/{variant['id']}",
		"title": variant.get("title"),
		"sku": variant.get("sku"),
		"price": variant.get("price"),
		"selectedOptions": selected_options,
		"inventoryItem": {
			"measurement": {
				"weight": {
					"value": variant.get("weight"),
					"unit": REST_TO_GRAPHQL_WEIGHT_UNIT.get(variant.get("weight_unit")),
				}
			}
		},
	}


def rest_product_to_graphql_node(product):
	"""Convert a REST-shaped product dict (as found in test fixtures) into the
	GraphQL `Product` node shape consumed by `product._fetch_shopify_product`
	and the import-products page's paginated product queries."""
	image = product.get("image")

	return {
		"id": f"gid://shopify/Product/{product['id']}",
		"title": product.get("title"),
		"descriptionHtml": product.get("body_html"),
		"productType": product.get("product_type"),
		"vendor": product.get("vendor"),
		"featuredMedia": {"image": {"url": image.get("src")}} if image else None,
		"options": [
			{"name": opt.get("name"), "values": opt.get("values", [])} for opt in product.get("options", [])
		],
		"variants": {
			"edges": [
				{"node": rest_variant_to_graphql_node(variant)} for variant in product.get("variants", [])
			]
		},
	}
