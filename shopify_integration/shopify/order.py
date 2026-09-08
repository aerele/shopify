import json
from typing import Literal, Optional

import frappe
from ecommerce_core.utils.integration_log import run_integration_job
from ecommerce_core.utils.price_list import get_dummy_price_list
from ecommerce_core.utils.taxation import get_dummy_tax_category
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	MODULE_NAME,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from shopify_integration.shopify.customer import ShopifyCustomer
from shopify_integration.shopify.product import create_items_if_not_exist, get_item_code
from shopify_integration.shopify.utils import create_shopify_log

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def sync_sales_order(payload, request_id=None):
	return run_integration_job(
		MODULE_NAME,
		_sync_sales_order,
		payload,
		request_id=request_id,
		method="shopify_integration.shopify.order.sync_sales_order",
		request_data=payload,
		set_user="Administrator",
		success_status=None,
	)


def _sync_sales_order(payload):
	order = payload

	if frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])}):
		reconcile_existing_order(order)
		create_shopify_log(status="Success")
		return
	try:
		shopify_customer = order.get("customer") if order.get("customer") is not None else {}
		shopify_customer["billing_address"] = order.get("billing_address", "")
		shopify_customer["shipping_address"] = order.get("shipping_address", "")
		customer_id = shopify_customer.get("id")
		if customer_id:
			customer = ShopifyCustomer(customer_id=customer_id)
			if not customer.is_synced():
				customer.sync_customer(customer=shopify_customer)
			else:
				customer.update_existing_addresses(shopify_customer)

		create_items_if_not_exist(order)

		setting = frappe.get_doc(SETTING_DOCTYPE)
		create_order(order, setting)

		if order.get("cancelled_at"):
			cancel_order(order)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success")


def reconcile_existing_order(order, request_id=None):
	"""Called when a Sales Order already exists for this Shopify order id
	(e.g. re-synced via sync_old_orders after enable_shopify was off).
	Brings the Sales Invoice / Delivery Note / cancellation state up to
	date with Shopify's current state, instead of skipping silently.

	Only ever called from sync_sales_order(), which has already set the
	user/request_id for this job, so it doesn't need to set them again."""
	if request_id is not None:
		frappe.flags.request_id = request_id

	if order.get("cancelled_at"):
		cancel_order(order)
		return

	# local import to avoid circular dependencies
	from shopify_integration.shopify.fulfillment import create_delivery_note
	from shopify_integration.shopify.invoice import create_sales_invoice

	try:
		sales_order = get_sales_order(cstr(order["id"]))
		if not sales_order:
			create_shopify_log(status="Invalid", message="Sales Order not found for status reconciliation")
			return

		setting = frappe.get_doc(SETTING_DOCTYPE)

		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, sales_order)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, sales_order)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success")


def create_order(order, setting, company=None):
	# local import to avoid circular dependencies
	from shopify_integration.shopify.fulfillment import create_delivery_note
	from shopify_integration.shopify.invoice import create_sales_invoice

	so = create_sales_order(order, setting, company)
	if so:
		if order.get("financial_status") == "paid":
			create_sales_invoice(order, setting, so)

		if order.get("fulfillments"):
			create_delivery_note(order, setting, so)


def create_sales_order(shopify_order, setting, company=None):
	customer = setting.default_customer
	if shopify_order.get("customer", {}):
		if customer_id := shopify_order.get("customer", {}).get("id"):
			customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name")

	so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")

	if not so:
		# Analyze all discounts (both order-level and item-level) in one pass
		discount_info = _analyze_all_discounts(shopify_order)

		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
			discount_info=discount_info,
		)

		if not items:
			message = (
				"Following items exists in the shopify order but relevant records were"
				" not found in the shopify Product master"
			)
			product_not_exists = []  # TODO: fix missing items
			message += "\n" + ", ".join(product_not_exists)

			create_shopify_log(status="Error", exception=message, rollback=True)

			return ""

		taxes = get_order_taxes(shopify_order, setting, items)

		# Build Sales Order document
		so_dict = {
			"doctype": "Sales Order",
			"naming_series": setting.sales_order_series or "SO-Shopify-",
			ORDER_ID_FIELD: str(shopify_order.get("id")),
			ORDER_NUMBER_FIELD: shopify_order.get("name"),
			"customer": customer,
			"transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
			"delivery_date": getdate(shopify_order.get("created_at")) or nowdate(),
			"company": setting.company,
			"selling_price_list": get_dummy_price_list(),
			"ignore_pricing_rule": 1,
			"items": items,
			"taxes": taxes,
			"tax_category": get_dummy_tax_category(),
		}

		# Add order-level discount fields at HEADER level if applicable
		if discount_info and discount_info.get("order_level"):
			so_dict["apply_discount_on"] = "Net Total"
			if discount_info["order_level"]["type"] == "percentage":
				so_dict["additional_discount_percentage"] = discount_info["order_level"]["value"]
			else:
				so_dict["discount_amount"] = discount_info["order_level"]["value"]

		so = frappe.get_doc(so_dict)

		if company:
			so.update({"company": company, "status": "Draft"})
		so.flags.ignore_mandatory = True
		so.flags.shopiy_order_json = json.dumps(shopify_order)
		so.save(ignore_permissions=True)
		so.submit()

		if shopify_order.get("note"):
			so.add_comment(text=f"Order Note: {shopify_order.get('note')}")

	else:
		so = frappe.get_doc("Sales Order", so)

	return so


def get_order_items(order_items, setting, delivery_date, taxes_inclusive, discount_info=None):
	"""
	Build item list for Sales Order.
	Handles all 6 discount scenarios using unified discount_info.

	Scenarios handled:
	1. Item-level percentage only
	2. Item-level amount only
	3. Order-level percentage only
	4. Order-level amount only
	5. Mixed: Item-level + Order-level percentage
	6. Mixed: Item-level + Order-level amount
	"""
	items = []
	all_product_exists = True
	product_not_exists = []

	for shopify_item in order_items:
		if not shopify_item.get("product_exists"):
			all_product_exists = False
			product_not_exists.append(
				{"title": shopify_item.get("title"), ORDER_ID_FIELD: shopify_item.get("id")}
			)
			continue

		if all_product_exists:
			item_code = get_item_code(shopify_item)
			item_dict = {
				"item_code": item_code,
				"item_name": shopify_item.get("name"),
				"delivery_date": delivery_date,
				"qty": shopify_item.get("quantity"),
				"stock_uom": shopify_item.get("uom") or "Nos",
				"warehouse": setting.warehouse,
			}

			# Get original Shopify price
			original_price = flt(shopify_item.get("price"))
			qty = cint(shopify_item.get("quantity"))

			# Set price_list_rate to original Shopify price (before any discounts)
			item_dict["price_list_rate"] = original_price

			# Get item-level discount info from unified discount_info
			item_discount = None
			if discount_info and discount_info.get("items"):
				# OPTIMIZATION: O(1) dict lookup instead of O(n) list search
				item_id = str(shopify_item.get("id"))
				item_discount = discount_info["items"].get(item_id)

			# Apply ITEM-LEVEL discounts only (order-level handled at Sales Order header)
			if item_discount and item_discount.get("has_item_level_discount"):
				item_discount_amount = item_discount["item_discount_amount"]

				if item_discount["item_discount_type"] == "percentage":
					# Item-level percentage discount
					item_dict["discount_percentage"] = item_discount["item_discount_value"]
				else:
					# Item-level amount discount
					item_dict["discount_amount"] = item_discount_amount

				# Store in shopify_item_discount for reference
				item_dict[ORDER_ITEM_DISCOUNT_FIELD] = item_discount_amount

				# Calculate rate (item-level only, order-level applied by ERPNext)
				item_dict["rate"] = original_price - (item_discount_amount / qty)
			else:
				# No item-level discount, rate = original price
				item_dict["rate"] = original_price

			items.append(item_dict)
		else:
			items = []

	return items


def _analyze_all_discounts(shopify_order):
	"""
	Unified discount analysis function that handles both order-level and item-level discounts.
	"""
	discount_applications = shopify_order.get("discount_applications") or []
	line_items = shopify_order.get("line_items", [])

	result = {
		"order_level": None,
		"items": {},  # Dict for O(1) lookup instead of list
	}

	if not discount_applications or not line_items:
		return result

	# OPTIMIZATION: Create lookup dict for discount applications
	# This eliminates repeated index lookups in nested loops
	discount_app_lookup = {}
	order_level_discount = None

	# Single pass: build lookup dict and identify order-level discount
	for idx, discount_app in enumerate(discount_applications):
		discount_app_lookup[idx] = discount_app

		allocation_method = discount_app.get("allocation_method")
		target_selection = discount_app.get("target_selection")
		value_type = discount_app.get("value_type")
		value = flt(discount_app.get("value", 0))

		if allocation_method == "across" and target_selection == "all":
			if not order_level_discount:
				order_level_discount = {
					"type": value_type,
					"value": value,
					"description": discount_app.get("title", "Order-level Discount"),
				}

	if order_level_discount:
		result["order_level"] = order_level_discount

	# Process line items with O(1) app lookup
	for line_item in line_items:
		item_id = str(line_item.get("id"))
		item_sku = line_item.get("sku")
		discount_allocations = line_item.get("discount_allocations", [])

		if not discount_allocations:
			continue

		item_discount_info = {
			"item_id": item_id,
			"item_sku": item_sku,
			"has_item_level_discount": False,
			"item_discount_type": None,
			"item_discount_value": None,
			"item_discount_amount": 0.0,
		}

		has_item_level = False
		item_discount_type = None
		total_item_discount = 0.0
		percentage_values = []

		# OPTIMIZATION: Direct dict lookup instead of index-based access
		for allocation in discount_allocations:
			amount = flt(allocation.get("amount", 0))
			app_index = allocation.get("discount_application_index")

			if app_index is not None and app_index in discount_app_lookup:
				discount_app = discount_app_lookup[app_index]
				app_allocation_method = discount_app.get("allocation_method")
				app_target_selection = discount_app.get("target_selection")
				app_value_type = discount_app.get("value_type")
				app_value = flt(discount_app.get("value", 0))

				# Check if this is an item-level discount (not order-level)
				if not (app_allocation_method == "across" and app_target_selection == "all"):
					has_item_level = True
					total_item_discount += amount

					if app_value_type == "percentage":
						item_discount_type = "percentage"
						percentage_values.append(app_value)
					else:
						if item_discount_type != "percentage":
							item_discount_type = "fixed_amount"

		# Build item discount info
		if has_item_level:
			item_discount_info["has_item_level_discount"] = True
			item_discount_info["item_discount_type"] = item_discount_type
			item_discount_info["item_discount_amount"] = total_item_discount

			if item_discount_type == "percentage":
				if len(percentage_values) == 1:
					item_discount_info["item_discount_value"] = percentage_values[0]
				else:
					item_discount_info["item_discount_type"] = "fixed_amount"
					item_discount_info["item_discount_value"] = total_item_discount
			else:
				item_discount_info["item_discount_value"] = total_item_discount

			# Store in dict for O(1) lookup
			result["items"][item_id] = item_discount_info

	return result


def get_order_taxes(shopify_order, setting, items):
	taxes = []
	line_items = shopify_order.get("line_items")

	for line_item in line_items:
		item_code = get_item_code(line_item)
		for tax in line_item.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
					"description": (
						get_tax_account_description(tax)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax.get("price"),
					"included_in_print_rate": 0,
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {item_code: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]},
					"dont_recompute_tax": 1,
				}
			)

	update_taxes_with_shipping_lines(
		taxes,
		shopify_order.get("shipping_lines"),
		setting,
		items,
		taxes_inclusive=shopify_order.get("taxes_included"),
	)

	if cint(setting.consolidate_taxes):
		taxes = consolidate_order_taxes(taxes)

	for row in taxes:
		tax_detail = row.get("item_wise_tax_detail")
		if isinstance(tax_detail, dict):
			row["item_wise_tax_detail"] = json.dumps(tax_detail)

	return taxes


def consolidate_order_taxes(taxes):
	tax_account_wise_data = {}
	for tax in taxes:
		account_head = tax["account_head"]
		tax_account_wise_data.setdefault(
			account_head,
			{
				"charge_type": "Actual",
				"account_head": account_head,
				"description": tax.get("description"),
				"cost_center": tax.get("cost_center"),
				"included_in_print_rate": 0,
				"dont_recompute_tax": 1,
				"tax_amount": 0,
				"item_wise_tax_detail": {},
			},
		)
		tax_account_wise_data[account_head]["tax_amount"] += flt(tax.get("tax_amount"))
		if tax.get("item_wise_tax_detail"):
			tax_account_wise_data[account_head]["item_wise_tax_detail"].update(tax["item_wise_tax_detail"])

	return tax_account_wise_data.values()


def get_tax_account_head(tax, charge_type: Literal["shipping", "sales_tax"] | None = None):
	tax_title = str(tax.get("title"))

	tax_account = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
		"tax_account",
	)

	if not tax_account and charge_type:
		tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, DEFAULT_TAX_FIELDS[charge_type])

	if not tax_account:
		frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

	return tax_account


def get_tax_account_description(tax):
	tax_title = tax.get("title")

	tax_description = frappe.db.get_value(
		"Shopify Tax Account",
		{"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
		"tax_description",
	)

	return tax_description


def update_taxes_with_shipping_lines(taxes, shipping_lines, setting, items, taxes_inclusive=False):
	"""Shipping lines represents the shipping details,
	each such shipping detail consists of a list of tax_lines"""
	shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
	for shipping_charge in shipping_lines:
		if shipping_charge.get("price"):
			shipping_discounts = shipping_charge.get("discount_allocations") or []
			total_discount = sum(flt(discount.get("amount")) for discount in shipping_discounts)

			shipping_taxes = shipping_charge.get("tax_lines") or []
			total_tax = sum(flt(discount.get("price")) for discount in shipping_taxes)

			shipping_charge_amount = flt(shipping_charge["price"]) - flt(total_discount)
			if bool(taxes_inclusive):
				shipping_charge_amount -= total_tax

			if shipping_as_item:
				items.append(
					{
						"item_code": setting.shipping_item,
						"rate": shipping_charge_amount,
						"delivery_date": items[-1]["delivery_date"] if items else nowdate(),
						"qty": 1,
						"stock_uom": "Nos",
						"warehouse": setting.warehouse,
					}
				)
			else:
				taxes.append(
					{
						"charge_type": "Actual",
						"account_head": get_tax_account_head(shipping_charge, charge_type="shipping"),
						"description": get_tax_account_description(shipping_charge)
						or shipping_charge["title"],
						"tax_amount": shipping_charge_amount,
						"cost_center": setting.cost_center,
					}
				)

		for tax in shipping_charge.get("tax_lines"):
			taxes.append(
				{
					"charge_type": "Actual",
					"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
					"description": (
						get_tax_account_description(tax)
						or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
					),
					"tax_amount": tax["price"],
					"cost_center": setting.cost_center,
					"item_wise_tax_detail": {
						setting.shipping_item: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]
					}
					if shipping_as_item
					else {},
					"dont_recompute_tax": 1,
				}
			)


def get_sales_order(order_id):
	"""Get ERPNext sales order using shopify order id."""
	sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
	if sales_order:
		return frappe.get_doc("Sales Order", sales_order)
	return None


def cancel_order(payload, request_id=None):
	"""Called by order/cancelled event.

	When shopify order is cancelled there could be many different someone handles it.

	Updates document with custom field showing order status.

	IF sales invoice / delivery notes are not generated against an order, then cancel it.
	"""
	if request_id is not None:
		frappe.flags.request_id = request_id

	order = payload

	try:
		order_id = order["id"]
		order_status = order["financial_status"]

		sales_order = get_sales_order(order_id)

		if not sales_order:
			create_shopify_log(status="Invalid", message="Sales Order does not exist")
			return

		sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
		delivery_notes = frappe.db.get_list("Delivery Note", filters={ORDER_ID_FIELD: order_id})

		if sales_invoice:
			frappe.db.set_value("Sales Invoice", sales_invoice, ORDER_STATUS_FIELD, order_status)

		for dn in delivery_notes:
			frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

		if not sales_invoice and not delivery_notes and sales_order.docstatus == 1:
			sales_order.flags.ignore_permissions = True
			sales_order.cancel()
		else:
			frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status)

	except Exception as e:
		create_shopify_log(status="Error", exception=e)
	else:
		create_shopify_log(status="Success")


@temp_shopify_session
def sync_old_orders():
	shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not cint(shopify_setting.sync_old_orders):
		return

	orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

	for order in orders:
		log = create_shopify_log(
			method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
		)
		sync_sales_order(order, request_id=log.name)

	shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
	shopify_setting.sync_old_orders = 0
	shopify_setting.save()


_OLD_ORDERS_QUERY = """
query oldOrders($query: String!, $limit: Int!, $cursor: String) {
	orders(first: $limit, query: $query, after: $cursor) {
		edges {
			cursor
			node {
				id
				name
				createdAt
				cancelledAt
				note
				taxesIncluded
				displayFinancialStatus
				customer {
					id
						firstName
						lastName
						defaultEmailAddress {
							emailAddress
						}
						defaultPhoneNumber {
							phoneNumber
						}
						defaultAddress {
							address1
							address2
							city
							province
							country
							zip
							phone
						}
				}
				billingAddress {
					address1
					address2
					city
					province
					country
					zip
					phone
				}
				shippingAddress {
					address1
					address2
					city
					province
					country
					zip
					phone
				}
				lineItems(first: 50) {
					edges {
						node {
							id
							name
							quantity
							sku
							product {
								id
							}
							variant {
								id
							}
							originalUnitPriceSet {
								shopMoney {
									amount
								}
							}
							taxLines {
								title
								rate
								priceSet {
									shopMoney {
										amount
									}
								}
							}
							discountAllocations {
								allocatedAmountSet {
									shopMoney {
										amount
									}
								}
							}
						}
					}
				}
				shippingLines(first: 10) {
					edges {
						node {
							title
							originalPriceSet {
								shopMoney {
									amount
								}
							}
							taxLines {
								title
								rate
								priceSet {
									shopMoney {
										amount
									}
								}
							}
							discountAllocations {
								allocatedAmountSet {
									shopMoney {
										amount
									}
								}
							}
						}
					}
				}
				fulfillments(first: 10) {
					id
					createdAt
					location {
						id
					}
					fulfillmentLineItems(first: 50) {
						edges {
							node {
								quantity
								lineItem {
									sku
									product {
										id
									}
									variant {
										id
									}
								}
							}
						}
					}
				}
			}
			pageInfo {
				hasNextPage
				endCursor
			}
		}
	}
"""


def _gid_to_id(gid) -> int | None:
	"""Extract the plain numeric id from a Shopify GraphQL global id, e.g.
	"gid://shopify/Order/123" -> 123. Used everywhere a numeric id is stored
	or compared, matching the format the REST API and live webhooks use."""
	if not gid:
		return None
	return cint(str(gid).rsplit("/", 1)[-1])


def _money(price_set) -> str:
	return (price_set or {}).get("shopMoney", {}).get("amount", "0.0")


def _normalize_gql_order(node) -> dict:
	"""Convert a Shopify GraphQL order node into the same snake_case dict
	shape the rest of this module already expects from the REST API and
	from live webhook payloads, so no downstream code needs to change."""
	customer_node = node.get("customer") or {}

	# Build customer dict matching REST API webhook payload structure
	normalized_customer = {}
	if customer_node.get("id"):
		normalized_customer["id"] = _gid_to_id(customer_node.get("id"))
		normalized_customer["first_name"] = customer_node.get("firstName") or ""
		normalized_customer["last_name"] = customer_node.get("lastName") or ""

		# Extract email from new defaultEmailAddress structure
		if customer_node.get("defaultEmailAddress"):
			normalized_customer["email"] = customer_node.get("defaultEmailAddress", {}).get("emailAddress")

		# Extract phone from new defaultPhoneNumber structure
		if customer_node.get("defaultPhoneNumber"):
			normalized_customer["phone"] = customer_node.get("defaultPhoneNumber", {}).get("phoneNumber")

		# Include defaultAddress for customer sync fallback
		if customer_node.get("defaultAddress"):
			normalized_customer["default_address"] = customer_node.get("defaultAddress")

	line_items = []
	for edge in (node.get("lineItems") or {}).get("edges", []):
		li = edge.get("node") or {}
		product_id = _gid_to_id((li.get("product") or {}).get("id"))
		line_items.append(
			{
				"id": _gid_to_id(li.get("id")),
				"name": li.get("name"),
				"quantity": li.get("quantity"),
				"sku": li.get("sku"),
				# Shopify's REST webhook payloads include this natively;
				# GraphQL has no direct equivalent, so derive it from
				# whether the product still exists (product is null if
				# it was deleted from the store).
				"product_exists": bool(product_id),
				"product_id": product_id,
				"variant_id": _gid_to_id((li.get("variant") or {}).get("id")),
				"price": _money(li.get("originalUnitPriceSet")),
				"tax_lines": [
					{
						"title": tax.get("title"),
						"rate": tax.get("rate"),
						"price": _money(tax.get("priceSet")),
					}
					for tax in li.get("taxLines") or []
				],
				"discount_allocations": [
					{"amount": _money(discount.get("allocatedAmountSet"))}
					for discount in li.get("discountAllocations") or []
				],
			}
		)

	shipping_lines = []
	for edge in (node.get("shippingLines") or {}).get("edges", []):
		sl = edge.get("node") or {}
		shipping_lines.append(
			{
				"title": sl.get("title"),
				"price": _money(sl.get("originalPriceSet")),
				"tax_lines": [
					{
						"title": tax.get("title"),
						"rate": tax.get("rate"),
						"price": _money(tax.get("priceSet")),
					}
					for tax in sl.get("taxLines") or []
				],
				"discount_allocations": [
					{"amount": _money(discount.get("allocatedAmountSet"))}
					for discount in sl.get("discountAllocations") or []
				],
			}
		)

	fulfillments = []
	for f in node.get("fulfillments") or []:
		fulfillment_line_items = []
		for edge in (f.get("fulfillmentLineItems") or {}).get("edges", []):
			fli_node = edge.get("node") or {}
			li = fli_node.get("lineItem") or {}
			fulfillment_line_items.append(
				{
					"product_id": _gid_to_id((li.get("product") or {}).get("id")),
					"variant_id": _gid_to_id((li.get("variant") or {}).get("id")),
					"sku": li.get("sku"),
					"quantity": fli_node.get("quantity"),
				}
			)

		fulfillments.append(
			{
				"id": _gid_to_id(f.get("id")),
				"order_id": _gid_to_id(node.get("id")),
				"created_at": f.get("createdAt"),
				"location_id": _gid_to_id((f.get("location") or {}).get("id")),
				"line_items": fulfillment_line_items,
			}
		)

	return {
		"id": _gid_to_id(node.get("id")),
		"name": node.get("name"),
		"created_at": node.get("createdAt"),
		"cancelled_at": node.get("cancelledAt"),
		"note": node.get("note"),
		"taxes_included": node.get("taxesIncluded"),
		# normalized to lowercase to match the format Shopify's live
		# webhook payloads already use for this same field
		"financial_status": (node.get("displayFinancialStatus") or "").lower(),
		"customer": normalized_customer,
		"billing_address": node.get("billingAddress") or {},
		"shipping_address": node.get("shippingAddress") or {},
		"line_items": line_items,
		"shipping_lines": shipping_lines,
		"fulfillments": fulfillments,
	}


def _fetch_old_orders(from_time, to_time, limit=250):
	"""Fetch all shopify orders in specified range and return an iterator on fetched orders."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()
	search_query = f'status:any AND created_at:>="{from_time}" AND created_at:<="{to_time}"'

	cursor = None
	has_next_page = True

	while has_next_page:
		response = json.loads(
			GraphQL().execute(_OLD_ORDERS_QUERY, {"query": search_query, "limit": limit, "cursor": cursor})
		)

		if response.get("errors"):
			frappe.log_error(json.dumps(response["errors"], indent=2), "Shopify Old Orders Fetch Error")
			return

		orders_data = response.get("data", {}).get("orders", {})

		for edge in orders_data.get("edges", []):
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield _normalize_gql_order(edge["node"])

		page_info = orders_data.get("pageInfo", {})
		has_next_page = page_info.get("hasNextPage", False)
		cursor = page_info.get("endCursor")
