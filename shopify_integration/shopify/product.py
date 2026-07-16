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
			"weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP.get(product_dict.get("weight_unit")),
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

				for i, selected_option in enumerate(variant.get("selected_options", [])):
					if i < len(attributes) and selected_option.get("value"):
						attributes[i].update(
							{
								"attribute_value": self._get_attribute_value(
									selected_option["value"], attributes[i]
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
			# SUPPLIER_ID_FIELD is a fixed constant (custom fieldname), not user
			# input; only the query values below are dynamic, and those are
			# safely parameterized via %s.
			supplier = frappe.db.sql(  # nosemgrep: security.frappe-sql-format-injection
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


@temp_shopify_session
def _fetch_shopify_product(product_id: str) -> dict:
	"""Fetch a Shopify product with its variants and options via GraphQL."""
	query = """
	query ($id: ID!) {
	  product(id: $id) {
	    id
	    title
	    descriptionHtml
	    productType
	    vendor
	    featuredMedia {
	      ... on MediaImage {
	        image {
	          url
	        }
	      }
	    }
	    options(first: 10) {
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
	          selectedOptions {
	            name
	            value
	          }
	          inventoryItem {
	            measurement {
	              weight {
	                value
	                unit
	              }
	            }
	          }
	        }
	      }
	    }
	  }
	}
	"""
	product_gid = f"gid://shopify/Product/{product_id}"
	response = GraphQL().execute(query, variables={"id": product_gid})
	result = json.loads(response)
	product = result.get("data", {}).get("product")

	if not product:
		frappe.throw(_("Shopify product {0} not found").format(product_id))

	variants = []
	for edge in product.get("variants", {}).get("edges", []):
		node = edge.get("node", {})
		weight = node.get("inventoryItem", {}).get("measurement", {}).get("weight") or {}
		variants.append(
			{
				"id": node.get("id", "").split("/")[-1],
				"title": node.get("title"),
				"sku": node.get("sku"),
				"price": node.get("price"),
				"weight": weight.get("value"),
				"weight_unit": weight.get("unit"),
				"selected_options": node.get("selectedOptions", []),
			}
		)

	featured_media = product.get("featuredMedia") or {}
	image = featured_media.get("image")

	return {
		"id": product.get("id", "").split("/")[-1],
		"title": product.get("title"),
		"body_html": product.get("descriptionHtml"),
		"product_type": product.get("productType"),
		"vendor": product.get("vendor"),
		"image": {"src": image.get("url")} if image else None,
		"options": [
			{"name": opt.get("name"), "values": opt.get("values", [])} for opt in product.get("options", [])
		],
		"variants": variants,
	}


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
		product_input["variants"] = [
			update_default_variant_properties(
				template_item,
				sku=template_item.item_code,
				price=template_item.get(ITEM_SELLING_RATE_FIELD),
				is_stock_item=template_item.is_stock_item,
			)
		]

		if item.variant_of:
			product_options = []
			variant_attributes = {
				"sku": item.item_code,
				"price": item.get(ITEM_SELLING_RATE_FIELD),
			}
			max_index_range = min(3, len(template_item.attributes))
			for i in range(0, max_index_range):
				attr = template_item.attributes[i]
				product_options.append(
					{
						"name": attr.attribute,
						"values": [
							{"name": v}
							for v in frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							)
						],
					}
				)
				try:
					variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
				except IndexError:
					frappe.throw(_("Shopify Error: Missing value for attribute {}").format(attr.attribute))

			product_input["productOptions"] = product_options
			product_input["variants"] = [
				_build_variant_input(template_item, product_options, variant_attributes)
			]

		result = _shopify_product_set(product_input)
		product = result["product"]
		is_successful = result["success"]

		if is_successful:
			ecom_items = list(set([item, template_item]))
			for d in ecom_items:
				default_variant = (product.get("variants") or [{}])[0]
				ecom_item = frappe.get_doc(
					{
						"doctype": "Ecommerce Item",
						"erpnext_item_code": d.name,
						"integration": MODULE_NAME,
						"integration_item_code": str(product.get("id")),
						"variant_id": "" if d.has_variants else str(default_variant.get("id") or ""),
						"sku": "" if d.has_variants else str(default_variant.get("sku") or ""),
						"has_variants": d.has_variants,
						"variant_of": d.variant_of,
					}
				)
				ecom_item.insert()

		write_upload_log(status=is_successful, product=product, item=item, errors=result["errors"])
	elif setting.update_shopify_item_on_update:
		product_input = map_erpnext_item_to_shopify(erpnext_item=template_item)
		product_input["id"] = f"gid://shopify/Product/{product_id}"

		variant_attributes = None
		if not item.variant_of:
			product_input["variants"] = [
				update_default_variant_properties(
					template_item,
					is_stock_item=template_item.is_stock_item,
					price=item.get(ITEM_SELLING_RATE_FIELD),
				)
			]
		else:
			variant_attributes = {"sku": item.item_code, "price": item.get(ITEM_SELLING_RATE_FIELD)}
			product_options = []
			max_index_range = min(3, len(template_item.attributes))
			for i in range(0, max_index_range):
				attr = template_item.attributes[i]
				product_options.append(
					{
						"name": attr.attribute,
						"values": [
							{"name": v}
							for v in frappe.db.get_all(
								"Item Attribute Value", {"parent": attr.attribute}, pluck="attribute_value"
							)
						],
					}
				)
				try:
					variant_attributes[f"option{i+1}"] = item.attributes[i].attribute_value
				except IndexError:
					frappe.throw(_("Shopify Error: Missing value for attribute {}").format(attr.attribute))
			product_input["productOptions"] = product_options

		result = _shopify_product_set(product_input)
		product = result["product"]
		is_successful = result["success"]

		if is_successful and item.variant_of:
			map_erpnext_variant_to_shopify_variant(product, item, variant_attributes)

		write_upload_log(
			status=is_successful, product=product, item=item, errors=result["errors"], action="Updated"
		)


@temp_shopify_session
def map_erpnext_variant_to_shopify_variant(shopify_product: dict, erpnext_item, variant_attributes: dict):
	"""Match the ERPNext item variant to its Shopify variant using selected options.

	Links the two via an `Ecommerce Item` and returns the matched Shopify variant id.
	"""
	variant_product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"erpnext_item_code": erpnext_item.name, "integration": MODULE_NAME},
		"integration_item_code",
	)
	if not variant_product_id:
		option_values = [
			variant_attributes.get(f"option{i}") for i in range(1, 4) if variant_attributes.get(f"option{i}")
		]

		for variant in shopify_product.get("variants", []):
			selected_values = [opt.get("value") for opt in variant.get("selected_options", [])]
			if selected_values == option_values:
				variant_product_id = variant.get("id")
				if not frappe.flags.in_test:
					frappe.get_doc(
						{
							"doctype": "Ecommerce Item",
							"erpnext_item_code": erpnext_item.name,
							"integration": MODULE_NAME,
							"integration_item_code": str(shopify_product.get("id")),
							"variant_id": variant_product_id,
							"sku": variant.get("sku"),
							"variant_of": erpnext_item.variant_of,
						}
					).insert()
				break
		if not variant_product_id:
			msgprint(_("Shopify: Couldn't sync item variant."))
	return variant_product_id


def map_erpnext_item_to_shopify(erpnext_item) -> dict:
	"""Map erpnext fields to Shopify's `ProductSetInput`, for creating/updating products."""

	product_input = {
		"title": erpnext_item.item_name,
		"descriptionHtml": erpnext_item.description or erpnext_item.item_name,
		"productType": erpnext_item.item_group,
	}

	if erpnext_item.disabled:
		product_input["status"] = "DRAFT"
		msgprint(_("Status of linked Shopify product is changed to Draft."))

	return product_input


def _get_variant_weight(erpnext_item) -> dict | None:
	if erpnext_item.weight_uom not in WEIGHT_TO_ERPNEXT_UOM_MAP.values():
		return None
	return {
		"value": erpnext_item.weight_per_unit,
		"unit": get_shopify_weight_uom(erpnext_weight_uom=erpnext_item.weight_uom),
	}


def get_shopify_weight_uom(erpnext_weight_uom: str) -> str:
	for shopify_uom, erpnext_uom in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
		if erpnext_uom == erpnext_weight_uom:
			return shopify_uom


def update_default_variant_properties(
	template_item,
	is_stock_item: bool,
	sku: str | None = None,
	price: float | None = None,
) -> dict:
	"""Build the default-variant input for Shopify's `productSet` mutation.

	Shopify creates a "Default Title" variant for products without explicit
	options; this fills in its SKU, price, weight, and inventory tracking.
	"""
	variant = {"optionValues": [{"optionName": "Title", "name": "Default Title"}]}

	if sku is not None:
		variant["sku"] = sku
	if price is not None:
		variant["price"] = str(price)

	inventory_item = {}
	if is_stock_item:
		inventory_item["tracked"] = True

	weight = _get_variant_weight(template_item)
	if weight:
		inventory_item["measurement"] = {"weight": weight}

	if inventory_item:
		variant["inventoryItem"] = inventory_item

	return variant


def _build_variant_input(template_item, product_options: list, variant_attributes: dict) -> dict:
	variant = {
		"sku": variant_attributes.get("sku"),
		"price": str(variant_attributes.get("price") or 0),
		"optionValues": [
			{"optionName": opt["name"], "name": variant_attributes.get(f"option{i+1}", "")}
			for i, opt in enumerate(product_options)
		],
	}

	weight = _get_variant_weight(template_item)
	if weight:
		variant["inventoryItem"] = {"measurement": {"weight": weight}}

	return variant


@temp_shopify_session
def _shopify_product_set(product_input: dict) -> dict:
	"""Create or update a Shopify product using the `productSet` mutation.

	Returns a dict with `success`, `product` (normalized dict, or None on
	failure), and `errors` (list of user-facing error messages).
	"""
	mutation = """
	mutation ProductSet($input: ProductSetInput!) {
	  productSet(synchronous: true, input: $input) {
	    product {
	      id
	      title
	      descriptionHtml
	      productType
	      status
	      variants(first: 50) {
	        edges {
	          node {
	            id
	            sku
	            price
	            selectedOptions {
	              name
	              value
	            }
	            inventoryItem {
	              id
	            }
	          }
	        }
	      }
	    }
	    userErrors {
	      field
	      message
	    }
	  }
	}
	"""
	response = GraphQL().execute(mutation, variables={"input": product_input})
	result = json.loads(response)

	product_set = result.get("data", {}).get("productSet") or {}
	user_errors = product_set.get("userErrors") or []
	product = product_set.get("product")

	if user_errors or not product:
		return {
			"success": False,
			"product": None,
			"errors": [e.get("message") for e in user_errors],
		}

	return {
		"success": True,
		"errors": [],
		"product": {
			"id": product.get("id", "").split("/")[-1],
			"title": product.get("title"),
			"body_html": product.get("descriptionHtml"),
			"product_type": product.get("productType"),
			"status": product.get("status"),
			"variants": [
				{
					"id": edge.get("node", {}).get("id", "").split("/")[-1],
					"sku": edge.get("node", {}).get("sku"),
					"price": edge.get("node", {}).get("price"),
					"selected_options": edge.get("node", {}).get("selectedOptions", []),
					"inventory_item_id": edge.get("node", {}).get("inventoryItem", {}).get("id"),
				}
				for edge in product.get("variants", {}).get("edges", [])
			],
		},
	}


def write_upload_log(
	status: bool, product: dict | None, item, errors: list | None = None, action="Created"
) -> None:
	if not status:
		msg = _("Failed to upload item to Shopify") + "<br>"
		if errors:
			msg += _("Shopify reported errors:") + " " + ", ".join(errors)
		msgprint(msg, title="Note", indicator="orange")

		create_shopify_log(
			status="Error",
			request_data=product or {},
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
