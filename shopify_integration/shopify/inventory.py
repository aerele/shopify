import json
from collections import Counter

import frappe
from ecommerce_core.controllers.inventory import (
	get_inventory_levels,
	update_inventory_sync_status,
)
from ecommerce_core.controllers.scheduling import need_to_run
from frappe.utils import cint, create_batch, now
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import MODULE_NAME, SETTING_DOCTYPE
from shopify_integration.shopify.utils import create_shopify_log


class _VariantNotFound(Exception):
	"""Raised when a Shopify product variant no longer exists."""


def update_inventory_on_shopify() -> None:
	"""Upload stock levels from ERPNext to Shopify.

	Called by scheduler on configured interval.
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopify:
		return

	if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
		return

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	inventory_levels = get_inventory_levels(tuple(warehous_map.keys()), MODULE_NAME)

	if inventory_levels:
		upload_inventory_data_to_shopify(inventory_levels, warehous_map)


_VARIANT_INVENTORY_ITEM_QUERY = """
query productVariant($id: ID!) {
	productVariant(id: $id) {
		id
		inventoryItem {
			id
		}
	}
}
"""

_INVENTORY_ACTIVATE_MUTATION = """
mutation inventoryActivate($inventoryItemId: ID!, $locationId: ID!) {
	inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) {
		inventoryLevel {
			id
		}
		userErrors {
			field
			message
		}
	}
}
"""

_INVENTORY_SET_QUANTITIES_MUTATION = """
mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
	inventorySetQuantities(input: $input) {
		inventoryAdjustmentGroup {
			id
		}
		userErrors {
			field
			message
		}
	}
}
"""


@temp_shopify_session
def upload_inventory_data_to_shopify(inventory_levels, warehous_map) -> None:
	synced_on = now()

	for inventory_sync_batch in create_batch(inventory_levels, 50):
		for d in inventory_sync_batch:
			d.shopify_location_id = warehous_map[d.warehouse]

			try:
				variant_gid = f"gid://shopify/ProductVariant/{d.variant_id}"
				location_gid = f"gid://shopify/Location/{d.shopify_location_id}"

				variant_response = json.loads(
					GraphQL().execute(_VARIANT_INVENTORY_ITEM_QUERY, {"id": variant_gid})
				)
				variant_data = variant_response.get("data", {}).get("productVariant")
				if not variant_data:
					raise _VariantNotFound

				inventory_item_id = variant_data.get("inventoryItem", {}).get("id")

				# Ensure the location is tracking this inventory item before setting
				# its quantity. On repeat syncs the item is already activated, which
				# Shopify reports as a userError here; that's expected, not a failure,
				# so it doesn't block the actual quantity update below.
				GraphQL().execute(
					_INVENTORY_ACTIVATE_MUTATION,
					{"inventoryItemId": inventory_item_id, "locationId": location_gid},
				)

				set_response = json.loads(
					GraphQL().execute(
						_INVENTORY_SET_QUANTITIES_MUTATION,
						{
							"input": {
								"reason": "correction",
								"name": "available",
								"ignoreCompareQuantity": True,
								"quantities": [
									{
										"inventoryItemId": inventory_item_id,
										"locationId": location_gid,
										# shopify doesn't support fractional quantity
										"quantity": cint(d.actual_qty) - cint(d.reserved_qty),
									}
								],
							}
						},
					)
				)
				user_errors = (
					set_response.get("data", {}).get("inventorySetQuantities", {}).get("userErrors") or []
				)
				errors = set_response.get("errors") or user_errors
				if errors:
					raise Exception("; ".join(err.get("message", "") for err in errors))

				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Success"
			except _VariantNotFound:
				# Variant or location is deleted, mark as last synced and ignore.
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Not Found"
			except Exception as e:
				d.status = "Failed"
				d.failure_reason = str(e)

			frappe.db.commit()

		_log_inventory_update_status(inventory_sync_batch)


def _log_inventory_update_status(inventory_levels) -> None:
	"""Create log of inventory update."""
	log_message = "variant_id,location_id,status,failure_reason\n"

	log_message += "\n".join(
		f"{d.variant_id},{d.shopify_location_id},{d.status},{d.failure_reason or ''}"
		for d in inventory_levels
	)

	stats = Counter([d.status for d in inventory_levels])

	percent_successful = stats["Success"] / len(inventory_levels)

	if percent_successful == 0:
		status = "Failed"
	elif percent_successful < 1:
		status = "Partial Success"
	else:
		status = "Success"

	log_message = f"Updated {percent_successful * 100}% items\n\n" + log_message

	create_shopify_log(method="update_inventory_on_shopify", status=status, message=log_message)
