# Copyright (c) 2026, Frappe and Contributors
# See LICENSE

import json
from unittest.mock import patch

import frappe

from shopify_integration.shopify.constants import SETTING_DOCTYPE
from shopify_integration.shopify.metafields import (
	apply_metafields,
	fetch_metafields,
	get_metafield_mappings,
	sync_metafields_to_doc,
)

from .utils import TestCase

WEBHOOKS_PATCH = (
	"shopify_integration.shopify.doctype.shopify_setting.shopify_setting.ShopifySetting._handle_webhooks"
)

PRODUCT_METAFIELDS = [
	{"namespace": "custom", "key": "fabric", "value": "100% Cotton"},
	{"namespace": "custom", "key": "care_instructions", "value": "Machine wash cold"},
]


# Every custom field used across these tests, created once in setUpClass via
# the same production function the setting save uses. Creating custom fields
# mid-test must be avoided: create_custom_fields issues DDL, which implicitly
# commits the test transaction and leaks rows into later tests.
ALL_TEST_FIELDS = [
	frappe._dict(
		shopify_resource="Product",
		namespace="custom",
		key="fabric",
		erpnext_fieldname="custom_shopify_fabric",
	),
	frappe._dict(
		shopify_resource="Product",
		namespace="custom",
		key="care_instructions",
		erpnext_fieldname="custom_shopify_care",
	),
	frappe._dict(
		shopify_resource="Product",
		namespace="custom",
		key="test_color_ref",
		erpnext_fieldname="custom_shopify_color",
	),
	frappe._dict(
		shopify_resource="Customer",
		namespace="custom",
		key="loyalty_tier",
		erpnext_fieldname="custom_shopify_loyalty_tier",
	),
]


def set_metafield_mappings(rows):
	setting = frappe.get_doc(SETTING_DOCTYPE)
	# append() builds proper child documents - assigning plain dicts breaks
	# Frappe v17's _set_defaults during save
	setting.metafield_mapping = []
	for row in rows:
		setting.append("metafield_mapping", row)
	# custom fields are pre-created in setUpClass; letting the save create
	# them again mid-test issues DDL, which implicitly commits the test
	# transaction and leaks rows into later tests
	with (
		patch(WEBHOOKS_PATCH),
		patch("shopify_integration.shopify.metafields.ensure_metafield_custom_fields"),
	):
		setting.save(ignore_permissions=True)


class TestMetafields(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.shopify import metafields

		# fabric is created alone first, like an earlier save would have
		metafields.ensure_metafield_custom_fields(ALL_TEST_FIELDS[:1])
		metafields.ensure_metafield_custom_fields(ALL_TEST_FIELDS)

	def test_mappings_are_filtered_by_resource(self):
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "fabric",
					"erpnext_fieldname": "custom_shopify_fabric",
				},
				{
					"shopify_resource": "Order",
					"namespace": "custom",
					"key": "gift_note",
					"erpnext_fieldname": "custom_shopify_gift_note",
				},
			]
		)

		product_mappings = get_metafield_mappings("Product")
		self.assertEqual(len(product_mappings), 1)
		self.assertEqual(product_mappings[0]["key"], "fabric")

		customer_mappings = get_metafield_mappings("Customer")
		self.assertEqual(customer_mappings, [])

	def test_apply_metafields_on_dict(self):
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "fabric",
					"erpnext_fieldname": "custom_shopify_fabric",
				}
			]
		)

		item_dict = {"item_code": "SHIRT-1"}
		applied = apply_metafields(item_dict, "Product", PRODUCT_METAFIELDS)

		self.assertEqual(applied, ["custom_shopify_fabric"])
		self.assertEqual(item_dict["custom_shopify_fabric"], "100% Cotton")

	def test_apply_metafields_skips_unmapped_metafields(self):
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "warranty",
					"erpnext_fieldname": "custom_shopify_warranty",
				}
			]
		)

		item_dict = {}
		applied = apply_metafields(item_dict, "Product", PRODUCT_METAFIELDS)

		self.assertEqual(applied, [])
		self.assertEqual(item_dict, {})

	def test_apply_metafields_on_document(self):
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "care_instructions",
					"erpnext_fieldname": "custom_shopify_care",
				}
			]
		)

		item = frappe.new_doc("Item")
		apply_metafields(item, "Product", PRODUCT_METAFIELDS)

		self.assertEqual(item.custom_shopify_care, "Machine wash cold")

	def test_apply_metafields_resolves_metaobject_reference(self):
		"""A metaobject_reference value is a raw GID - it must be replaced with
		the referenced entry's display name."""
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "color",
					"erpnext_fieldname": "custom_shopify_color",
				}
			]
		)
		self.fake_graphql({"data": {"metaobject": {"displayName": "Red"}}})

		item_dict = {}
		applied = apply_metafields(
			item_dict,
			"Product",
			[
				{
					"namespace": "custom",
					"key": "color",
					"type": "metaobject_reference",
					"value": "gid://shopify/Metaobject/123456",
				}
			],
		)

		self.assertEqual(applied, ["custom_shopify_color"])
		self.assertEqual(item_dict["custom_shopify_color"], "Red")

	def test_apply_metafields_resolves_list_metaobject_reference(self):
		"""list.metaobject_reference values are JSON arrays of GIDs - they
		resolve to a comma-separated list of display names."""
		from shopify_integration.shopify import metafields as mf

		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "colors",
					"erpnext_fieldname": "custom_shopify_colors",
				}
			]
		)
		responses = iter(
			[
				{"data": {"metaobject": {"displayName": "Red"}}},
				{"data": {"metaobject": {"displayName": "Blue"}}},
			]
		)

		class FakeGraphQL:
			def execute(self, query, variables=None):
				return json.dumps(next(responses))

		item_dict = {}
		with patch.object(mf, "GraphQL", FakeGraphQL):
			apply_metafields(
				item_dict,
				"Product",
				[
					{
						"namespace": "custom",
						"key": "colors",
						"type": "list.metaobject_reference",
						"value": '["gid://shopify/Metaobject/1", "gid://shopify/Metaobject/2"]',
					}
				],
			)

		self.assertEqual(item_dict["custom_shopify_colors"], "Red, Blue")

	def test_apply_metafields_keeps_raw_reference_when_resolution_fails(self):
		"""Unresolvable references (missing scope, deleted entry) fall back to
		the raw GID instead of losing the value."""
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "color",
					"erpnext_fieldname": "custom_shopify_color",
				}
			]
		)
		self.fake_graphql({"errors": [{"message": "access denied"}]})

		item_dict = {}
		apply_metafields(
			item_dict,
			"Product",
			[
				{
					"namespace": "custom",
					"key": "color",
					"type": "metaobject_reference",
					"value": "gid://shopify/Metaobject/123456",
				}
			],
		)

		self.assertEqual(item_dict["custom_shopify_color"], "gid://shopify/Metaobject/123456")

	def test_sync_metafields_to_doc_resolves_metaobject_reference(self):
		"""The order/customer fetch path resolves references too (fetch, then
		one query per referenced entry)."""
		from shopify_integration.shopify import metafields as mf
		from shopify_integration.shopify.metafields import sync_metafields_to_doc

		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "_Test Metaobject Customer",
				"customer_group": "_Test Customer Group 1",
				"territory": "All Territories",
				"customer_type": "Individual",
			}
		).insert(ignore_permissions=True)

		set_metafield_mappings(
			[
				{
					"shopify_resource": "Customer",
					"namespace": "custom",
					"key": "loyalty_tier",
					"erpnext_fieldname": "custom_shopify_loyalty_tier",
				}
			]
		)

		responses = iter(
			[
				{
					"data": {
						"customer": {
							"metafields": {
								"nodes": [
									{
										"namespace": "custom",
										"key": "loyalty_tier",
										"type": "metaobject_reference",
										"value": "gid://shopify/Metaobject/777",
									}
								]
							}
						}
					}
				},
				{"data": {"metaobject": {"displayName": "Gold"}}},
			]
		)

		class FakeGraphQL:
			def execute(self, query, variables=None):
				return json.dumps(next(responses))

		with patch.object(mf, "GraphQL", FakeGraphQL):
			sync_metafields_to_doc("Customer", customer.name, "Customer", 8899)

		self.assertEqual(
			frappe.db.get_value("Customer", customer.name, "custom_shopify_loyalty_tier"), "Gold"
		)

	def test_sync_product_metafields_resolves_metaobject_reference(self):
		"""Regression: the product query's metafield nodes must keep their
		`type` - without it a metaobject reference is stored as a raw GID."""
		import shopify_integration.shopify.product as product_module
		from shopify_integration.shopify import metafields as mf
		from shopify_integration.shopify.metafields import sync_product_metafields

		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "test_color_ref",
					"erpnext_fieldname": "custom_shopify_color",
				}
			]
		)

		def _cleanup():
			for name in frappe.get_all(
				"Ecommerce Item", filters={"integration_item_code": "555777"}, pluck="name"
			):
				frappe.delete_doc("Ecommerce Item", name, force=True, ignore_permissions=True)
			if frappe.db.exists("Item", "METAOBJ-PROD"):
				frappe.delete_doc("Item", "METAOBJ-PROD", force=True, ignore_permissions=True)

		self.addCleanup(_cleanup)

		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "METAOBJ-PROD",
				"item_name": "Metaobject Product",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
				"stock_uom": "Nos",
			}
		)
		item.flags.from_integration = True
		item.insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "shopify",
				"erpnext_item_code": "METAOBJ-PROD",
				"integration_item_code": "555777",
				"sku": "MO-1",
			}
		).insert(ignore_permissions=True)

		responses = iter(
			[
				{
					"data": {
						"product": {
							"id": "gid://shopify/Product/555777",
							"title": "Metaobject Product",
							"metafields": {
								"nodes": [
									{
										"namespace": "custom",
										"key": "test_color_ref",
										"type": "metaobject_reference",
										"value": "gid://shopify/Metaobject/709520621843",
									}
								]
							},
							"variants": {"edges": []},
						}
					}
				},
				{"data": {"metaobject": {"displayName": "Red"}}},
			]
		)

		class FakeGraphQL:
			def execute(self, query, variables=None):
				return json.dumps(next(responses))

		with patch.object(product_module, "GraphQL", FakeGraphQL), patch.object(mf, "GraphQL", FakeGraphQL):
			sync_product_metafields("555777")

		self.assertEqual(frappe.db.get_value("Item", "METAOBJ-PROD", "custom_shopify_color"), "Red")

	def test_invalid_erpnext_fieldname_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			set_metafield_mappings(
				[
					{
						"shopify_resource": "Product",
						"namespace": "custom",
						"key": "fabric",
						"erpnext_fieldname": "fabric field!",
					}
				]
			)

	def test_mapping_to_core_field_is_rejected(self):
		"""A metafield mapping must never target a core field like item_name -
		every sync would silently overwrite real data."""
		with self.assertRaises(frappe.ValidationError):
			set_metafield_mappings(
				[
					{
						"shopify_resource": "Product",
						"namespace": "custom",
						"key": "fabric",
						"erpnext_fieldname": "item_name",
					}
				]
			)

	def test_duplicate_mapping_is_rejected(self):
		row = {
			"shopify_resource": "Product",
			"namespace": "custom",
			"key": "fabric",
			"erpnext_fieldname": "custom_shopify_fabric",
		}
		with self.assertRaises(frappe.ValidationError):
			set_metafield_mappings([row, dict(row)])

	def test_duplicate_fieldname_across_rows_is_rejected(self):
		"""Two rows writing the same field would silently last-one-win."""
		with self.assertRaises(frappe.ValidationError):
			set_metafield_mappings(
				[
					{
						"shopify_resource": "Product",
						"namespace": "custom",
						"key": "fabric",
						"erpnext_fieldname": "custom_shopify_fabric",
					},
					{
						"shopify_resource": "Product",
						"namespace": "custom",
						"key": "care_instructions",
						"erpnext_fieldname": "custom_shopify_fabric",
					},
				]
			)

	def test_custom_field_is_created_automatically(self):
		"""Fields are created by ensure_metafield_custom_fields (run from
		setting save); here they are pre-created in setUpClass by the same
		function, so verify presence and placement."""
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "fabric",
					"erpnext_fieldname": "custom_shopify_fabric",
				}
			]
		)

		meta = frappe.get_meta("Item")
		self.assertTrue(meta.has_field("custom_shopify_fabric"))

		# the field must sit inside the "Shopify Metafields" section, anchored
		# to a real field - never appended to the end of the form, which spawns
		# a stray duplicate tab on tabbed forms like Item
		self.assertTrue(meta.has_field("shopify_metafields_section"))
		field = meta.get_field("custom_shopify_fabric")
		self.assertEqual(field.insert_after, "shopify_metafields_section")
		section = meta.get_field("shopify_metafields_section")
		self.assertIn(section.insert_after, ("shopify_selling_rate", "standard_rate"))

	def test_multiple_fields_are_chained_inside_the_section(self):
		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "fabric",
					"erpnext_fieldname": "custom_shopify_fabric",
				},
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "care_instructions",
					"erpnext_fieldname": "custom_shopify_care",
				},
			]
		)

		meta = frappe.get_meta("Item")
		self.assertEqual(meta.get_field("custom_shopify_fabric").insert_after, "shopify_metafields_section")
		self.assertEqual(meta.get_field("custom_shopify_care").insert_after, "custom_shopify_fabric")

	def test_fetch_metafields(self):
		self.fake_graphql(
			{
				"data": {
					"customer": {
						"metafields": {
							"nodes": [
								{
									"namespace": "custom",
									"key": "loyalty_tier",
									"value": "Gold",
									"type": "single_line_text_field",
								}
							]
						}
					}
				}
			}
		)

		metafields = fetch_metafields("Customer", 8899)

		self.assertEqual(len(metafields), 1)
		self.assertEqual(metafields[0]["key"], "loyalty_tier")

	def test_fetch_metafields_handles_graphql_errors(self):
		self.fake_graphql({"errors": [{"message": "boom"}]})

		self.assertEqual(fetch_metafields("Order", 450), [])

	def test_fetch_shopify_metafield_definitions(self):
		"""The mapping picker's data source: definitions per resource, paginated,
		with valid fieldname suggestions (keys may contain dashes or digits).

		GraphQL is stubbed because the fetcher issues one query per resource
		(and per page) inside a single call, while fake_graphql can only hold
		one queued response at a time."""
		from shopify_integration.shopify import metafields as mf
		from shopify_integration.shopify.metafields import fetch_shopify_metafield_definitions

		def page(nodes, has_next=False, cursor=None):
			return {
				"data": {
					"metafieldDefinitions": {
						"nodes": nodes,
						"pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
					}
				}
			}

		def node(namespace, key):
			return {
				"namespace": namespace,
				"key": key,
				"name": key.replace("_", " "),
				"type": {"name": "single_line_text_field"},
			}

		responses = iter(
			[
				page([node("custom", "fabric"), node("custom", "hsn_code")], True, "c1"),  # Product p1
				page([node("shopify", "color-pattern"), node("custom", "2nd_value")]),  # Product p2
				page([node("custom", "gift_note")]),  # Order
				page([]),  # Customer
			]
		)

		class FakeGraphQL:
			def execute(self, query, variables=None):
				return json.dumps(next(responses))

		with patch.object(mf, "GraphQL", FakeGraphQL):
			definitions = fetch_shopify_metafield_definitions()

		self.assertEqual(sorted(definitions), ["Order", "Product"])
		self.assertEqual(len(definitions["Product"]), 4)
		by_key = {d["key"]: d for d in definitions["Product"]}
		self.assertEqual(by_key["fabric"]["namespace"], "custom")
		# keys with dashes/digits become valid ERPNext fieldnames
		self.assertEqual(by_key["color-pattern"]["suggested_fieldname"], "color_pattern")
		self.assertEqual(by_key["2nd_value"]["suggested_fieldname"], "shopify_2nd_value")

	def test_fetch_definitions_skips_resource_on_graphql_error(self):
		from shopify_integration.shopify import metafields as mf
		from shopify_integration.shopify.metafields import fetch_shopify_metafield_definitions

		# Product fails, Order and Customer succeed - one failure must not hide the rest
		responses = iter(
			[
				{"errors": [{"message": "access denied"}]},
				{"data": {"metafieldDefinitions": {"nodes": [], "pageInfo": {}}}},
				{"data": {"metafieldDefinitions": {"nodes": [], "pageInfo": {}}}},
			]
		)

		class FakeGraphQL:
			def execute(self, query, variables=None):
				return json.dumps(next(responses))

		with patch.object(mf, "GraphQL", FakeGraphQL):
			self.assertEqual(fetch_shopify_metafield_definitions(), {})

	def test_product_update_webhook_stamps_linked_items(self):
		"""A products/update delivery (fired by any product edit, including
		metafield edits) must re-query the product and stamp its mapped
		metafields onto every synced Item linked to it."""
		from shopify_integration.shopify.metafields import handle_product_updated

		set_metafield_mappings(
			[
				{
					"shopify_resource": "Product",
					"namespace": "custom",
					"key": "fabric",
					"erpnext_fieldname": "custom_shopify_fabric",
				}
			]
		)

		def _cleanup():
			for name in frappe.get_all(
				"Ecommerce Item", filters={"integration_item_code": "6732194021530"}, pluck="name"
			):
				frappe.delete_doc("Ecommerce Item", name, force=True, ignore_permissions=True)
			if frappe.db.exists("Item", "WEBHOOK-PROD"):
				frappe.delete_doc("Item", "WEBHOOK-PROD", force=True, ignore_permissions=True)

		self.addCleanup(_cleanup)

		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "WEBHOOK-PROD",
				"item_name": "Webhook Product",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
				"stock_uom": "Nos",
			}
		)
		# skip the upload-to-Shopify Item hook; this item is "from the integration"
		item.flags.from_integration = True
		item.insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": "shopify",
				"erpnext_item_code": "WEBHOOK-PROD",
				"integration_item_code": "6732194021530",
				"sku": "WH-1",
			}
		).insert(ignore_permissions=True)

		# the delivery itself carries no metafields - the product is re-queried
		self.fake_graphql("single_product_gql")
		handle_product_updated({"id": "6732194021530"})

		self.assertEqual(frappe.db.get_value("Item", "WEBHOOK-PROD", "custom_shopify_fabric"), "100% Cotton")

	def test_sync_metafields_to_doc(self):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "_Test Metafield Customer",
				"customer_group": "_Test Customer Group 1",
				"territory": "All Territories",
				"customer_type": "Individual",
			}
		).insert(ignore_permissions=True)

		set_metafield_mappings(
			[
				{
					"shopify_resource": "Customer",
					"namespace": "custom",
					"key": "loyalty_tier",
					"erpnext_fieldname": "custom_shopify_loyalty_tier",
				}
			]
		)

		self.fake_graphql(
			{
				"data": {
					"customer": {
						"metafields": {
							"nodes": [
								{
									"namespace": "custom",
									"key": "loyalty_tier",
									"value": "Gold",
									"type": "single_line_text_field",
								}
							]
						}
					}
				}
			}
		)

		sync_metafields_to_doc("Customer", customer.name, "Customer", 8899)

		self.assertEqual(
			frappe.db.get_value("Customer", customer.name, "custom_shopify_loyalty_tier"), "Gold"
		)

	def test_sync_metafields_to_doc_skips_unchanged_values(self):
		"""Recurring refreshes (webhook redeliveries, the hourly job) must not
		rewrite - and re-timestamp - documents whose values didn't change."""
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "_Test Metafield Customer",
				"customer_group": "_Test Customer Group 1",
				"territory": "All Territories",
				"customer_type": "Individual",
			}
		).insert(ignore_permissions=True)

		set_metafield_mappings(
			[
				{
					"shopify_resource": "Customer",
					"namespace": "custom",
					"key": "loyalty_tier",
					"erpnext_fieldname": "custom_shopify_loyalty_tier",
				}
			]
		)

		def metafields_response(value):
			return {
				"data": {
					"customer": {
						"metafields": {
							"nodes": [
								{
									"namespace": "custom",
									"key": "loyalty_tier",
									"value": value,
									"type": "single_line_text_field",
								}
							]
						}
					}
				}
			}

		self.fake_graphql(metafields_response("Gold"))
		sync_metafields_to_doc("Customer", customer.name, "Customer", 8899)
		self.assertEqual(
			frappe.db.get_value("Customer", customer.name, "custom_shopify_loyalty_tier"), "Gold"
		)

		self.fake_graphql(metafields_response("Gold"))
		with patch("frappe.db.set_value") as mock_set_value:
			sync_metafields_to_doc("Customer", customer.name, "Customer", 8899)
		mock_set_value.assert_not_called()

		self.fake_graphql(metafields_response("Platinum"))
		sync_metafields_to_doc("Customer", customer.name, "Customer", 8899)
		self.assertEqual(
			frappe.db.get_value("Customer", customer.name, "custom_shopify_loyalty_tier"), "Platinum"
		)
