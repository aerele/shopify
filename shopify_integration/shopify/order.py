import json
from typing import Literal, Optional

import frappe
from ecommerce_core.utils.price_list import get_dummy_price_list
from ecommerce_core.utils.taxation import get_dummy_tax_category
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import (
	CUSTOMER_ID_FIELD,
	EVENT_MAPPER,
	ORDER_ID_FIELD,
	ORDER_ITEM_DISCOUNT_FIELD,
	ORDER_NUMBER_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
	SHOPIFY_LINE_ITEM_ID_FIELD,
)
from shopify_integration.shopify.customer import ShopifyCustomer
from shopify_integration.shopify.product import create_items_if_not_exist, get_item_code
from shopify_integration.shopify.utils import create_shopify_log

DEFAULT_TAX_FIELDS = {
	"sales_tax": "default_sales_tax_account",
	"shipping": "default_shipping_charges_account",
}


def sync_sales_order(payload, request_id=None):
	order = payload
	# Runs only as a webhook-dispatched background job (see EVENT_MAPPER /
	# process_request's HMAC-validated dispatch), never in a request context
	# with a logged-in user.
	frappe.set_user("Administrator")  # nosemgrep: security.frappe-setuser
	frappe.flags.request_id = request_id

	if frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])}):
		create_shopify_log(status="Invalid", message="Sales order already exists, not synced")
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
		items = get_order_items(
			shopify_order.get("line_items"),
			setting,
			getdate(shopify_order.get("created_at")),
			taxes_inclusive=shopify_order.get("taxes_included"),
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
		so = frappe.get_doc(
			{
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
		)

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


def get_order_items(order_items, setting, delivery_date, taxes_inclusive):
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
			items.append(
				{
					"item_code": item_code,
					"item_name": shopify_item.get("name"),
					"rate": _get_item_price(shopify_item, taxes_inclusive),
					"delivery_date": delivery_date,
					"qty": shopify_item.get("quantity"),
					"stock_uom": shopify_item.get("uom") or "Nos",
					"warehouse": setting.warehouse,
					ORDER_ITEM_DISCOUNT_FIELD: (
						_get_total_discount(shopify_item) / cint(shopify_item.get("quantity"))
					),
					SHOPIFY_LINE_ITEM_ID_FIELD: str(shopify_item.get("id")),
				}
			)
		else:
			items = []

	return items


def _get_item_price(line_item, taxes_inclusive: bool) -> float:
	price = flt(line_item.get("price"))
	qty = cint(line_item.get("quantity"))

	# remove line item level discounts
	total_discount = _get_total_discount(line_item)

	if not taxes_inclusive:
		return price - (total_discount / qty)

	total_taxes = 0.0
	for tax in line_item.get("tax_lines"):
		total_taxes += flt(tax.get("price"))

	return price - (total_taxes + total_discount) / qty


def _get_total_discount(line_item) -> float:
	discount_allocations = line_item.get("discount_allocations") or []
	return sum(flt(discount.get("amount")) for discount in discount_allocations)


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


def cancel_order(payload, request_id=None):
	"""Called by order/cancelled event.

	When shopify order is cancelled there could be many different someone handles it.

	Updates document with custom field showing order status.

	IF sales invoice / delivery notes are not generated against an order, then cancel it.
	"""
	# Runs only as a webhook-dispatched background job (see EVENT_MAPPER /
	# process_request's HMAC-validated dispatch), never in a request context
	# with a logged-in user.
	frappe.set_user("Administrator")  # nosemgrep: security.frappe-setuser
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


def _fetch_old_orders(from_time, to_time, limit=50):
	"""Fetch shopify orders in the given date range via GraphQL and yield them
	as REST-webhook-shaped dicts, so downstream sync logic doesn't need to
	special-case the order's origin."""

	from_time = get_datetime(from_time).astimezone().isoformat()
	to_time = get_datetime(to_time).astimezone().isoformat()

	query = """
	query GetOrdersByDateRange($query: String!, $limit: Int!, $cursor: String) {
	  orders(first: $limit, query: $query, after: $cursor) {
	    edges {
	      node {
	        id
	        name
	        note
	        createdAt
	        cancelledAt
	        taxesIncluded
	        displayFinancialStatus
	        customer {
	          id
	          firstName
	          lastName
	          email
	          phone
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
	        shippingLines(first: 10) {
	          edges {
	            node {
	              title
	              discountedPriceSet {
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
	            }
	          }
	        }
	        lineItems(first: 50) {
	          edges {
	            node {
	              id
	              name
	              quantity
	              sku
	              taxable
	              originalUnitPriceSet {
	                shopMoney {
	                  amount
	                }
	              }
	              product {
	                id
	              }
	              variant {
	                id
	              }
	              discountAllocations {
	                allocatedAmountSet {
	                  shopMoney {
	                    amount
	                  }
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
	    }
	    pageInfo {
	      hasNextPage
	      endCursor
	    }
	  }
	}
	"""

	search_query = f'created_at:>="{from_time}" AND created_at:<="{to_time}"'
	cursor = None
	has_next_page = True

	while has_next_page:
		variables = {"query": search_query, "limit": limit, "cursor": cursor}
		response = json.loads(GraphQL().execute(query, variables=variables))

		if "errors" in response:
			frappe.log_error(json.dumps(response["errors"], indent=2), "Shopify Order Fetch Error")
			break

		orders_data = response.get("data", {}).get("orders", {})

		for edge in orders_data.get("edges", []):
			# Using generator instead of fetching all at once is better for
			# avoiding rate limits and reducing resource usage.
			yield _normalize_order(edge.get("node", {}))

		page_info = orders_data.get("pageInfo", {})
		has_next_page = page_info.get("hasNextPage", False)
		cursor = page_info.get("endCursor")


def _gid_to_id(gid):
	return gid.split("/")[-1] if gid else None


def _money(money_set):
	return ((money_set or {}).get("shopMoney") or {}).get("amount", "0.0")


def _normalize_order(node):
	customer = node.get("customer") or {}

	line_items = []
	for edge in node.get("lineItems", {}).get("edges", []):
		item = edge.get("node", {})
		product_id = _gid_to_id((item.get("product") or {}).get("id"))
		variant_id = _gid_to_id((item.get("variant") or {}).get("id"))

		line_items.append(
			{
				"id": _gid_to_id(item.get("id")),
				"name": item.get("name"),
				"title": item.get("name"),
				"quantity": item.get("quantity"),
				"sku": item.get("sku"),
				"price": _money(item.get("originalUnitPriceSet")),
				"taxable": item.get("taxable"),
				"product_exists": bool(product_id),
				"product_id": product_id,
				"variant_id": variant_id,
				"discount_allocations": [
					{"amount": _money(a.get("allocatedAmountSet"))}
					for a in item.get("discountAllocations", [])
				],
				"tax_lines": [
					{"title": t.get("title"), "rate": t.get("rate"), "price": _money(t.get("priceSet"))}
					for t in item.get("taxLines", [])
				],
			}
		)

	shipping_lines = []
	for edge in node.get("shippingLines", {}).get("edges", []):
		line = edge.get("node", {})
		shipping_lines.append(
			{
				"title": line.get("title"),
				"price": _money(line.get("discountedPriceSet")),
				"discount_allocations": [],
				"tax_lines": [
					{"title": t.get("title"), "rate": t.get("rate"), "price": _money(t.get("priceSet"))}
					for t in line.get("taxLines", [])
				],
			}
		)

	order_id = _gid_to_id(node.get("id"))
	fulfillments = []
	for fulfillment in node.get("fulfillments", []):
		fulfillment_line_items = []
		for edge in fulfillment.get("fulfillmentLineItems", {}).get("edges", []):
			fli = edge.get("node", {})
			line_item = fli.get("lineItem") or {}
			fulfillment_line_items.append(
				{
					"quantity": fli.get("quantity"),
					"sku": line_item.get("sku"),
					"product_id": _gid_to_id((line_item.get("product") or {}).get("id")),
					"variant_id": _gid_to_id((line_item.get("variant") or {}).get("id")),
				}
			)

		fulfillments.append(
			{
				"id": _gid_to_id(fulfillment.get("id")),
				"order_id": order_id,
				"created_at": fulfillment.get("createdAt"),
				"location_id": _gid_to_id((fulfillment.get("location") or {}).get("id")),
				"line_items": fulfillment_line_items,
			}
		)

	return {
		"id": order_id,
		"name": node.get("name"),
		"note": node.get("note"),
		"created_at": node.get("createdAt"),
		"cancelled_at": node.get("cancelledAt"),
		"taxes_included": node.get("taxesIncluded"),
		# Normalized to lowercase to match the casing Shopify uses in its REST
		# webhook payloads (the only other source of orders for this codebase),
		# so `create_order`'s "paid" check works regardless of order origin.
		"financial_status": (node.get("displayFinancialStatus") or "").lower(),
		"customer": {
			"id": _gid_to_id(customer.get("id")),
			"first_name": customer.get("firstName"),
			"last_name": customer.get("lastName"),
			"email": customer.get("email"),
			"phone": customer.get("phone"),
		},
		"billing_address": node.get("billingAddress") or {},
		"shipping_address": node.get("shippingAddress") or {},
		"line_items": line_items,
		"shipping_lines": shipping_lines,
		"fulfillments": fulfillments,
	}
