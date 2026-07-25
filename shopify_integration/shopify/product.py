import json

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item
from frappe import _, msgprint
from frappe.utils import cint, cstr
from frappe.utils.nestedset import get_root_of
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import (
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
	SHOPIFY_VARIANTS_ATTR_LIST,
	SUPPLIER_ID_FIELD,
	WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from shopify_integration.shopify.utils import create_shopify_log


class ShopifyProduct:
	def __init__(
		self,
		product_id: str,
		variant_id: str | None = None,
		sku: str | None = None,
		has_variants: int | None = 0,
	):
		self.product_id = str(product_id)
		self.variant_id = str(variant_id) if variant_id else None
		self.sku = str(sku) if sku else None
		self.has_variants = has_variants
		self.setting = frappe.get_doc(SETTING_DOCTYPE)

		if not self.setting.is_enabled():
			frappe.throw(_("Can not create Shopify product when integration is disabled."))

	def is_synced(self) -> bool:
		return ecommerce_item.is_synced(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
		)

	def get_erpnext_item(self):
		return ecommerce_item.get_erpnext_item(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
			has_variants=self.has_variants,
		)

	@temp_shopify_session
	def sync_product(self):
		if not self.is_synced():
			product_dict = _fetch_shopify_product(self.product_id)
			self._make_item(product_dict)

	def _make_item(self, product_dict):
		_add_weight_details(product_dict)

		warehouse = self.setting.warehouse

		if _has_variants(product_dict):
			self.has_variants = 1
			attributes = self._create_attribute(product_dict)
			self._create_item(product_dict, warehouse, 1, attributes)
			self._create_item_variants(product_dict, warehouse, attributes)

		else:
			product_dict["variant_id"] = product_dict["variants"][0]["id"]
			self._create_item(product_dict, warehouse)

	def _create_attribute(self, product_dict):
		attribute = []
		for attr in product_dict.get("options"):
			if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": attr.get("name"),
						"item_attribute_values": [
							{"attribute_value": attr_value, "abbr": attr_value}
							for attr_value in attr.get("values")
						],
					}
				).insert()
				attribute.append({"attribute": attr.get("name")})

			else:
				# check for attribute values
				item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
				if not item_attr.numeric_values:
					self._set_new_attribute_values(item_attr, attr.get("values"))
					item_attr.save()
					attribute.append({"attribute": attr.get("name")})

				else:
					attribute.append(
						{
							"attribute": attr.get("name"),
							"from_range": item_attr.get("from_range"),
							"to_range": item_attr.get("to_range"),
							"increment": item_attr.get("increment"),
							"numeric_values": item_attr.get("numeric_values"),
						}
					)

		return attribute

	def _set_new_attribute_values(self, item_attr, values):
		for attr_value in values:
			if not any(
				(d.abbr.lower() == attr_value.lower() or d.attribute_value.lower() == attr_value.lower())
				for d in item_attr.item_attribute_values
			):
				item_attr.append("item_attribute_values", {"attribute_value": attr_value, "abbr": attr_value})

	def _create_item(self, product_dict, warehouse, has_variant=0, attributes=None, variant_of=None):
		item_dict = {
			"variant_of": variant_of,
			"is_stock_item": 1,
			"item_code": cstr(product_dict.get("item_code")) or cstr(product_dict.get("id")),
			"item_name": product_dict.get("title", "").strip(),
			"description": product_dict.get("body_html") or product_dict.get("title"),
			"item_group": self._get_item_group(product_dict.get("product_type")),
			"has_variants": has_variant,
			"attributes": attributes or [],
			"stock_uom": product_dict.get("uom") or _("Nos"),
			"sku": product_dict.get("sku") or _get_sku(product_dict),
			"default_warehouse": warehouse,
			"image": _get_item_image(product_dict),
			"weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP[product_dict.get("weight_unit")],
			"weight_per_unit": product_dict.get("weight"),
			"default_supplier": self._get_supplier(product_dict),
		}

		integration_item_code = product_dict["id"]  # shopify product_id
		variant_id = product_dict.get("variant_id", "")  # shopify variant_id if has variants
		sku = item_dict["sku"]

		if not _match_sku_and_link_item(
			item_dict, integration_item_code, variant_id, variant_of=variant_of, has_variant=has_variant
		):
			ecommerce_item.create_ecommerce_item(
				MODULE_NAME,
				integration_item_code,
				item_dict,
				variant_id=variant_id,
				sku=sku,
				variant_of=variant_of,
				has_variants=has_variant,
			)

	def _create_item_variants(self, product_dict, warehouse, attributes):
		template_item = ecommerce_item.get_erpnext_item(
			MODULE_NAME, integration_item_code=product_dict.get("id"), has_variants=1
		)

		if template_item:
			for variant in product_dict.get("variants"):
				shopify_item_variant = {
					"id": product_dict.get("id"),
					"variant_id": variant.get("id"),
					"item_code": variant.get("id"),
					"title": product_dict.get("title", "").strip() + "-" + variant.get("title"),
					"product_type": product_dict.get("product_type"),
					"sku": variant.get("sku"),
					"uom": template_item.stock_uom or _("Nos"),
					"item_price": variant.get("price"),
					"weight_unit": variant.get("weight_unit"),
					"weight": variant.get("weight"),
				}

				for i, variant_attr in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
					if variant.get(variant_attr):
						attributes[i].update(
							{
								"attribute_value": self._get_attribute_value(
									variant.get(variant_attr), attributes[i]
								)
							}
						)
				self._create_item(shopify_item_variant, warehouse, 0, attributes, template_item.name)

	def _get_attribute_value(self, variant_attr_val, attribute):
		attribute_value = frappe.db.sql(
			"""select attribute_value from `tabItem Attribute Value`
			where parent = %s and (abbr = %s or attribute_value = %s)""",
			(attribute["attribute"], variant_attr_val, variant_attr_val),
			as_list=1,
		)
		return attribute_value[0][0] if len(attribute_value) > 0 else cint(variant_attr_val)

	def _get_item_group(self, product_type=None):
		parent_item_group = get_root_of("Item Group")

		if not product_type:
			return parent_item_group

		if frappe.db.get_value("Item Group", product_type, "name"):
			return product_type
		item_group = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": product_type,
				"parent_item_group": parent_item_group,
				"is_group": "No",
			}
		).insert()
		return item_group.name

	def _get_supplier(self, product_dict):
		if product_dict.get("vendor"):
			supplier = frappe.db.sql(
				f"""select name from tabSupplier
				where name = %s or {SUPPLIER_ID_FIELD} = %s """,
				(product_dict.get("vendor"), product_dict.get("vendor").lower()),
				as_list=1,
			)

			if supplier:
				return product_dict.get("vendor")
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": product_dict.get("vendor"),
					SUPPLIER_ID_FIELD: product_dict.get("vendor").lower(),
					"supplier_group": self._get_supplier_group(),
				}
			).insert()
			return supplier.name
		else:
			return ""

	def _get_supplier_group(self):
		supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
		if not supplier_group:
			supplier_group = frappe.get_doc(
				{"doctype": "Supplier Group", "supplier_group_name": _("Shopify Supplier")}
			).insert()
			return supplier_group.name
		return supplier_group


_PRODUCT_QUERY = """
query product($id: ID!) {
	product(id: $id) {
		id
		title
		descriptionHtml
		productType
		vendor
		featuredImage {
			url
		}
		options {
			name
			values
		}
		variants(first: 100) {
			edges {
				node {
					id
					title
					sku
					price
					inventoryItem {
						measurement {
							weight {
								value
								unit
							}
						}
					}
					selectedOptions {
						name
						value
					}
				}
			}
		}
	}
}
"""


def _gid_to_id(gid) -> str:
	"""Extract the plain numeric id from a Shopify GraphQL global id, e.g.
	"gid://shopify/Product/123" -> "123". Used everywhere a numeric id is
	stored, matching the format the REST API used."""
	if not gid:
		return ""
	return str(gid).rsplit("/", 1)[-1]


def _get_selling_rate(item, template=None):
	"""Shopify Selling Rate isn't carried over when ERPNext creates a new
	variant (e.g. via the "Create Variant" button), so it's still 0 on the
	very first save - Shopify rejects a variant/product with a blank price.
	Fall back to the item's standard selling rate, then to the template
	item's rates, since a rate set only on the template doesn't get copied
	to variants either."""
	rate = item.get(ITEM_SELLING_RATE_FIELD) or item.get("standard_rate")
	if not rate and template:
		rate = template.get(ITEM_SELLING_RATE_FIELD) or template.get("standard_rate")
	return rate


def _normalize_gql_variant(node) -> dict:
	weight = ((node.get("inventoryItem") or {}).get("measurement") or {}).get("weight") or {}
	variant = {
		"id": _gid_to_id(node.get("id")),
		"title": node.get("title"),
		"sku": node.get("sku"),
		"price": node.get("price"),
		"weight": weight.get("value"),
		"weight_unit": weight.get("unit"),
	}
	# GraphQL reports selected options by name/value pairs instead of REST's
	# positional option1/option2/option3, so map them back positionally to
	# keep _create_item_variants() (which reads SHOPIFY_VARIANTS_ATTR_LIST)
	# unchanged.
	for i, option in enumerate(node.get("selectedOptions") or []):
		if i >= len(SHOPIFY_VARIANTS_ATTR_LIST):
			break
		variant[SHOPIFY_VARIANTS_ATTR_LIST[i]] = option.get("value")

	return variant


def _normalize_gql_product(node) -> dict:
	"""Convert a Shopify GraphQL product node into the same snake_case dict
	shape `_create_item()`/`_create_item_variants()`/etc. already expect
	from the REST API, so those functions need no changes."""
	variants = [
		_normalize_gql_variant(edge.get("node") or {})
		for edge in (node.get("variants") or {}).get("edges", [])
	]

	image = node.get("featuredImage") or {}

	return {
		"id": _gid_to_id(node.get("id")),
		"title": node.get("title"),
		"body_html": node.get("descriptionHtml"),
		"product_type": node.get("productType"),
		"vendor": node.get("vendor"),
		"image": {"src": image.get("url")} if image.get("url") else None,
		"options": node.get("options") or [],
		"variants": variants,
	}


def _fetch_shopify_product(product_id) -> dict:
	response = json.loads(GraphQL().execute(_PRODUCT_QUERY, {"id": f"gid://shopify/Product/{product_id}"}))

	if response.get("errors"):
		frappe.throw(
			_("Shopify GraphQL error fetching product {0}: {1}").format(
				product_id, json.dumps(response["errors"])
			)
		)

	product = response.get("data", {}).get("product")
	if not product:
		frappe.throw(_("Shopify product {0} not found (may have been deleted).").format(product_id))

	return _normalize_gql_product(product)


def _add_weight_details(product_dict):
	variants = product_dict.get("variants")
	if variants:
		product_dict["weight"] = variants[0]["weight"]
		product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
	options = product_dict.get("options")
	return bool(options and "Default Title" not in options[0]["values"])


def _get_sku(product_dict):
	if product_dict.get("variants"):
		return product_dict.get("variants")[0].get("sku")
	return ""


def _get_item_image(product_dict):
	if product_dict.get("image"):
		return product_dict.get("image").get("src")
	return None


def _match_sku_and_link_item(item_dict, product_id, variant_id, variant_of=None, has_variant=False) -> bool:
	"""Tries to match new item with existing item using Shopify SKU == item_code.

	Returns true if matched and linked.
	"""
	sku = item_dict["sku"]
	if not sku or variant_of or has_variant:
		return False

	item_name = frappe.db.get_value("Item", {"item_code": sku})
	if item_name:
		try:
			ecommerce_item = frappe.get_doc(
				{
					"doctype": "Ecommerce Item",
					"integration": MODULE_NAME,
					"erpnext_item_code": item_name,
					"integration_item_code": product_id,
					"has_variants": 0,
					"variant_id": cstr(variant_id),
					"sku": sku,
				}
			)

			ecommerce_item.insert()
			return True
		except Exception:
			return False


def create_items_if_not_exist(order):
	"""Using shopify order, sync all items that are not already synced."""
	for item in order.get("line_items", []):
		product_id = item["product_id"]
		variant_id = item.get("variant_id")
		sku = item.get("sku")
		product = ShopifyProduct(product_id, variant_id=variant_id, sku=sku)

		if not product.is_synced():
			product.sync_product()


def get_item_code(shopify_item):
	"""Get item code using shopify_item dict.

	Item should contain both product_id and variant_id."""

	item = ecommerce_item.get_erpnext_item(
		integration=MODULE_NAME,
		integration_item_code=shopify_item.get("product_id"),
		variant_id=shopify_item.get("variant_id"),
		sku=shopify_item.get("sku"),
	)
	if item:
		return item.item_code


_PRODUCT_MUTATION_FIELDS = """
product {
	id
	variants(first: 5) {
		edges {
			node {
				id
				sku
				price
				inventoryItem {
					measurement {
						weight {
							value
							unit
						}
					}
				}
				selectedOptions {
					name
					value
				}
			}
		}
	}
}
userErrors {
	field
	message
}
"""

_PRODUCT_CREATE_MUTATION = f"""
mutation productCreate($input: ProductInput!) {{
	productCreate(input: $input) {{
		{_PRODUCT_MUTATION_FIELDS}
	}}
}}
"""

_PRODUCT_UPDATE_MUTATION = f"""
mutation productUpdate($input: ProductInput!) {{
	productUpdate(input: $input) {{
		{_PRODUCT_MUTATION_FIELDS}
	}}
}}
"""

_VARIANTS_BULK_UPDATE_MUTATION = """
mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
	productVariantsBulkUpdate(productId: $productId, variants: $variants) {
		productVariants {
			id
			sku
			price
			inventoryItem {
				measurement {
					weight {
						value
						unit
					}
				}
			}
			selectedOptions {
				name
				value
			}
		}
		userErrors {
			field
			message
		}
	}
}
"""

_VARIANTS_BULK_CREATE_MUTATION = """
mutation productVariantsBulkCreate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
	productVariantsBulkCreate(productId: $productId, variants: $variants) {
		productVariants {
			id
			sku
			selectedOptions {
				name
				value
			}
		}
		userErrors {
			field
			message
		}
	}
}
"""


def _save_shopify_product(mutation: str, mutation_name: str, product_input: dict) -> dict | None:
	"""Run productCreate/productUpdate and return the normalized product dict,
	or None if Shopify reported errors."""
	response = json.loads(GraphQL().execute(mutation, {"input": product_input}))
	result = response.get("data", {}).get(mutation_name) or {}
	user_errors = result.get("userErrors") or []

	if response.get("errors") or user_errors:
		create_shopify_log(
			status="Error",
			response_data=response,
			exception=response.get("errors") or user_errors,
		)
		return None

	product = result.get("product") or {}
	variants = [
		_normalize_gql_variant(edge.get("node") or {})
		for edge in (product.get("variants") or {}).get("edges", [])
	]
	return {"id": _gid_to_id(product.get("id")), "variants": variants}


@temp_shopify_session
def upload_erpnext_item(doc, method=None):
	"""This hook is called when inserting new or updating existing `Item`.

	New items are pushed to shopify and changes to existing items are
	updated depending on what is configured in "Shopify Setting" doctype.
	"""
	template_item = item = doc  # alias for readability
	# a new item recieved from ecommerce_integrations is being inserted
	if item.flags.from_integration:
		return

	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.upload_erpnext_items:
		return

	if frappe.flags.in_import:
		return

	if item.has_variants:
		return

	if len(item.attributes) > 3:
		msgprint(_("Template items/Items with 4 or more attributes can not be uploaded to Shopify."))
		return

	if doc.variant_of and not setting.upload_variants_as_items:
		msgprint(_("Enable variant sync in setting to upload item to Shopify."))
		return

	if item.variant_of:
		template_item = frappe.get_doc("Item", item.variant_of)

	product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": template_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	is_new_product = not bool(product_id)

	if is_new_product:
		product_input = map_erpnext_item_to_shopify(erpnext_item=template_item)
		# weight is not a valid ProductInput field in the GraphQL Admin API -
		# it only exists at the variant level, so apply it separately below.
		weight = product_input.pop("weight", None)
		weight_unit = product_input.pop("weight_unit", None)
		product_input["status"] = "ACTIVE" if setting.sync_new_item_as_active else "DRAFT"

		variant_attributes = {
			"sku": template_item.item_code,
			"price": _get_selling_rate(template_item),
		}
		option_values = None

		if item.variant_of:
			option_values = []
			product_options = []
			variant_attributes = {"sku": item.item_code, "price": _get_selling_rate(item, template_item)}
			max_index_range = min(3, len(template_item.attributes))
			for i in range(0, max_index_range):
				attr = template_item.attributes[i]
				try:
					attribute_value = item.attributes[i].attribute_value
				except IndexError:
					frappe.throw(_("Shopify Error: Missing value for attribute {}").format(attr.attribute))
				variant_attributes[f"option{i + 1}"] = attribute_value
				option_values.append({"optionName": attr.attribute, "name": attribute_value})
				product_options.append(
					{"name": attr.attribute, "position": i + 1, "values": [{"name": attribute_value}]}
				)
			# ProductInput has no "options" field (plain string list) - option
			# names/values are set via "productOptions" instead.
			product_input["productOptions"] = product_options

		product = _save_shopify_product(_PRODUCT_CREATE_MUTATION, "productCreate", product_input)
		is_successful = bool(product)

		if is_successful:
			product = update_default_variant_properties(
				product,
				sku=variant_attributes["sku"],
				price=variant_attributes["price"],
				is_stock_item=template_item.is_stock_item,
				weight=weight,
				weight_unit=weight_unit,
				option_values=option_values,
			)

			ecom_items = list(set([item, template_item]))
			for d in ecom_items:
				first_variant = (product.get("variants") or [{}])[0]
				ecom_item = frappe.get_doc(
					{
						"doctype": "Ecommerce Item",
						"erpnext_item_code": d.name,
						"integration": MODULE_NAME,
						"integration_item_code": cstr(product.get("id")),
						"variant_id": "" if d.has_variants else cstr(first_variant.get("id") or ""),
						"sku": "" if d.has_variants else cstr(first_variant.get("sku") or ""),
						"has_variants": d.has_variants,
						"variant_of": d.variant_of,
					}
				)
				ecom_item.insert()

		write_upload_log(status=is_successful, product=product, item=item)
	elif setting.update_shopify_item_on_update:
		product = _fetch_shopify_product(product_id)
		if product:
			product_input = map_erpnext_item_to_shopify(erpnext_item=template_item)
			weight = product_input.pop("weight", None)
			weight_unit = product_input.pop("weight_unit", None)
			product_input["id"] = f"gid://shopify/Product/{product_id}"

			product = _save_shopify_product(_PRODUCT_UPDATE_MUTATION, "productUpdate", product_input)
			is_successful = bool(product)

			if is_successful and not item.variant_of:
				product = update_default_variant_properties(
					product,
					is_stock_item=template_item.is_stock_item,
					price=_get_selling_rate(item),
					weight=weight,
					weight_unit=weight_unit,
				)
			elif is_successful:
				variant_attributes = {"sku": item.item_code, "price": _get_selling_rate(item, template_item)}
				max_index_range = min(3, len(template_item.attributes))
				for i in range(0, max_index_range):
					attr = template_item.attributes[i]
					try:
						variant_attributes[f"option{i + 1}"] = item.attributes[i].attribute_value
					except IndexError:
						frappe.throw(
							_("Shopify Error: Missing value for attribute {}").format(attr.attribute)
						)
				map_erpnext_variant_to_shopify_variant(product, item, variant_attributes)

			write_upload_log(status=is_successful, product=product, item=item, action="Updated")


def map_erpnext_variant_to_shopify_variant(shopify_product: dict, erpnext_item, variant_attributes):
	variant_product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not variant_product_id:
		for variant in shopify_product.get("variants") or []:
			if (
				variant.get("option1") == variant_attributes.get("option1")
				and variant.get("option2") == variant_attributes.get("option2")
				and variant.get("option3") == variant_attributes.get("option3")
			):
				variant_product_id = str(variant.get("id"))
				if not frappe.flags.in_test:
					frappe.get_doc(
						{
							"doctype": "Ecommerce Item",
							"erpnext_item_code": erpnext_item.name,
							"integration": MODULE_NAME,
							"integration_item_code": str(shopify_product.get("id")),
							"variant_id": variant_product_id,
							"sku": str(variant.get("sku")),
							"variant_of": erpnext_item.variant_of,
						}
					).insert()
				break
		if not variant_product_id:
			variant_product_id = _create_shopify_variant(shopify_product, erpnext_item, variant_attributes)
	return variant_product_id


def _create_shopify_variant(shopify_product: dict, erpnext_item, variant_attributes) -> str | None:
	"""Create a new variant on an existing Shopify product for an ERPNext
	variant that doesn't have a matching Shopify variant yet (e.g. a second
	or later variant added after the product's first variant was already
	synced, which only ever created the product with its default variant)."""
	option_values = [
		{"optionName": attr.attribute, "name": attr.attribute_value} for attr in erpnext_item.attributes
	]

	variant_input = {
		"price": variant_attributes.get("price"),
		"optionValues": option_values,
		"inventoryItem": {"sku": variant_attributes.get("sku")},
	}

	response = json.loads(
		GraphQL().execute(
			_VARIANTS_BULK_CREATE_MUTATION,
			{
				"productId": f"gid://shopify/Product/{shopify_product.get('id')}",
				"variants": [variant_input],
			},
		)
	)
	result = response.get("data", {}).get("productVariantsBulkCreate") or {}
	user_errors = result.get("userErrors") or []
	created_variants = result.get("productVariants") or []

	if response.get("errors") or user_errors or not created_variants:
		create_shopify_log(
			status="Error",
			response_data=response,
			exception=response.get("errors") or user_errors or "No variant returned",
		)
		msgprint(_("Shopify: Couldn't sync item variant."))
		return None

	variant = created_variants[0]
	variant_product_id = _gid_to_id(variant.get("id"))

	if not frappe.flags.in_test:
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"erpnext_item_code": erpnext_item.name,
				"integration": MODULE_NAME,
				"integration_item_code": str(shopify_product.get("id")),
				"variant_id": variant_product_id,
				"sku": str(variant.get("sku")),
				"variant_of": erpnext_item.variant_of,
			}
		).insert()

	return variant_product_id


def map_erpnext_item_to_shopify(erpnext_item) -> dict:
	"""Map erpnext fields to a Shopify GraphQL product input, used both when
	updating and creating new products.

	The returned dict also carries "weight"/"weight_unit" keys, which aren't
	valid ProductInput fields (GraphQL only supports weight at the variant
	level) - the caller pops them off and applies them via
	update_default_variant_properties() instead.
	"""
	product_data = {
		"title": erpnext_item.item_name,
		"descriptionHtml": erpnext_item.description or erpnext_item.item_name,
		"productType": erpnext_item.item_group,
	}

	if erpnext_item.weight_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
		# reverse lookup for key
		uom = get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom)
		product_data["weight"] = erpnext_item.weight_per_unit
		product_data["weight_unit"] = uom

	if erpnext_item.disabled:
		product_data["status"] = "DRAFT"
		msgprint(_("Status of linked Shopify product is changed to Draft."))

	return product_data


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
		if erpnext_uom == erpnext_weight_uom:
			return shopify_uom


def update_default_variant_properties(
	shopify_product: dict,
	is_stock_item: bool,
	sku: str | None = None,
	price: float | None = None,
	weight: float | None = None,
	weight_unit: str | None = None,
	option_values: list | None = None,
) -> dict:
	"""Shopify creates default variant upon saving the product.

	Some item properties are supposed to be updated on the default variant.
	Input: saved shopify_product (dict), sku, price, weight and weight_unit.
	"""
	variants = shopify_product.get("variants") or []
	if not variants:
		return shopify_product

	variant_input = {"id": f"gid://shopify/ProductVariant/{variants[0]['id']}"}
	inventory_item: dict = {}

	# this will create Inventory item and qty will be updated by scheduled job.
	if is_stock_item:
		inventory_item["tracked"] = True

	if sku is not None:
		inventory_item["sku"] = sku

	if weight is not None and weight_unit is not None:
		inventory_item["measurement"] = {"weight": {"value": weight, "unit": weight_unit}}

	if inventory_item:
		variant_input["inventoryItem"] = inventory_item

	if price is not None:
		variant_input["price"] = price

	if option_values:
		variant_input["optionValues"] = option_values

	response = json.loads(
		GraphQL().execute(
			_VARIANTS_BULK_UPDATE_MUTATION,
			{
				"productId": f"gid://shopify/Product/{shopify_product['id']}",
				"variants": [variant_input],
			},
		)
	)
	result = response.get("data", {}).get("productVariantsBulkUpdate") or {}
	user_errors = result.get("userErrors") or []

	if response.get("errors") or user_errors:
		create_shopify_log(
			status="Error",
			response_data=response,
			exception=response.get("errors") or user_errors,
		)
		return shopify_product

	updated_variants = [_normalize_gql_variant(v) for v in result.get("productVariants") or []]
	if updated_variants:
		shopify_product["variants"] = updated_variants

	return shopify_product


def write_upload_log(status: bool, product: dict | None, item, action="Created") -> None:
	product = product or {}
	if not status:
		msg = _("Failed to upload item to Shopify") + "<br>"
		msgprint(msg, title="Note", indicator="orange")

		create_shopify_log(
			status="Error",
			request_data=product,
			message=msg,
			method="upload_erpnext_item",
		)
	else:
		create_shopify_log(
			status="Success",
			request_data=product,
			message=f"{action} Item: {item.name}, shopify product: {product.get('id')}",
			method="upload_erpnext_item",
		)


@frappe.whitelist()
def is_item_synced_from_shopify(item_code: str) -> dict:
	"""Return whether the ERPNext item is synced with Shopify.

	Used by the client script to make the `has_variants` field read-only.
	"""
	ecommerce_item = frappe.db.get_value(
		"Ecommerce Item", {"erpnext_item_code": item_code, "integration": MODULE_NAME}, "name"
	)

	return {"is_synced": bool(ecommerce_item)}
