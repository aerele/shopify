from copy import deepcopy

import frappe
from erpnext.selling.doctype.sales_order.mapper import make_delivery_note
from frappe.utils import cint, cstr, getdate

from shopify_integration.shopify.constants import (
	FULLFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from shopify_integration.shopify.order import get_sales_order
from shopify_integration.shopify.utils import create_shopify_log


def prepare_delivery_note(payload, request_id=None):
	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	order = payload

	try:
		sales_order = get_sales_order(cstr(order["id"]))
		if sales_order:
			status, result = create_delivery_note(order, setting, sales_order)
			if status == "success":
				# Delivery Note created successfully
				create_shopify_log(status="Success")
			elif status == "invalid":
				# Skip - delivery note already exists or sync disabled
				create_shopify_log(status="Invalid", message=result)
			else:
				# Error - can be retried after fixing the issue
				create_shopify_log(
					status="Error",
					method="shopify_integration.shopify.fulfillment.prepare_delivery_note",
					message=result,
				)
		else:
			create_shopify_log(
				status="Error",
				method="shopify_integration.shopify.fulfillment.prepare_delivery_note",
				message="Sales Order not found for syncing delivery note.",
			)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def create_delivery_note(shopify_order, setting, so) -> tuple[str, str]:
	"""Create a Delivery Note for this order's fulfillment that doesn't have one yet.
	Returns a tuple (status, result) where:
	- status: "success" if created, "invalid" if skipped (no retry needed), "error" if failed (can retry)
	- result: Delivery Note name on success, reason message on invalid/error."""

	# Check if delivery note sync is enabled (INVALID - configuration, no retry needed)
	if not cint(setting.sync_delivery_note):
		return ("error", "Delivery Note sync is disabled in Shopify settings")

	# Check if Sales Order is submitted (ERROR - can be retried after submitting)
	if so.docstatus != 1:
		return ("error", f"Sales Order {so.name} is not submitted (current status: Draft)")

	# Check if there are any fulfillments to process (ERROR - missing data, can retry)
	fulfillments = shopify_order.get("fulfillments")
	if not fulfillments:
		return ("error", f"No fulfillments found for order {shopify_order.get('name')}")

	# Process the first fulfillment (similar logic to original code)
	for fulfillment in fulfillments:
		# Check if delivery note already exists for this fulfillment (INVALID - already done, no retry needed)
		existing_dn = frappe.db.get_value(
			"Delivery Note", {FULLFILLMENT_ID_FIELD: fulfillment.get("id")}, "name"
		)
		if existing_dn:
			return (
				"invalid",
				f"Delivery Note {existing_dn} already exists for fulfillment {fulfillment.get('id')}",
			)

		# All checks passed - create the delivery note
		dn = make_delivery_note(so.name)
		setattr(dn, ORDER_ID_FIELD, fulfillment.get("order_id"))
		setattr(dn, ORDER_NUMBER_FIELD, shopify_order.get("name"))
		setattr(dn, FULLFILLMENT_ID_FIELD, fulfillment.get("id"))
		dn.set_posting_time = 1
		dn.posting_date = getdate(fulfillment.get("created_at"))
		dn.naming_series = setting.delivery_note_series or "DN-Shopify-"
		dn.items = get_fulfillment_items(
			dn.items, fulfillment.get("line_items"), fulfillment.get("location_id")
		)
		dn.flags.ignore_mandatory = True
		dn.save()
		dn.submit()

		if shopify_order.get("note"):
			dn.add_comment(text=f"Order Note: {shopify_order.get('note')}")

		return ("success", dn.name)

	return ("error", "No valid fulfillment found to create Delivery Note")


def get_fulfillment_items(dn_items, fulfillment_items, location_id=None):
	# local import to avoid circular imports
	from shopify_integration.shopify.product import get_item_code

	fulfillment_items = deepcopy(fulfillment_items)

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	wh_map = setting.get_integration_to_erpnext_wh_mapping()
	warehouse = wh_map.get(str(location_id)) or setting.warehouse

	final_items = []

	def find_matching_fullfilement_item(dn_item):
		nonlocal fulfillment_items

		for item in fulfillment_items:
			if get_item_code(item) == dn_item.item_code:
				fulfillment_items.remove(item)
				return item

	for dn_item in dn_items:
		if shopify_item := find_matching_fullfilement_item(dn_item):
			final_items.append(dn_item.update({"qty": shopify_item.get("quantity"), "warehouse": warehouse}))

	return final_items
