import json
import re

import frappe
from frappe import _
from frappe.utils import add_days, cstr, now_datetime
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import (
	CUSTOMER_ID_FIELD,
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	ORDER_ID_FIELD,
	ORDER_STATUS_FIELD,
	SETTING_DOCTYPE,
)
from shopify_integration.shopify.utils import create_shopify_log

RESOURCE_TO_DOCTYPE = {
	"Product": "Item",
	"Order": "Sales Order",
	"Customer": "Customer",
}

_METAFIELD_SECTION = {
	"fieldname": "shopify_metafields_section",
	"label": "Shopify Metafields",
	"fieldtype": "Section Break",
	"collapsible": 1,
}

_SECTION_ANCHORS = {
	"Item": (ITEM_SELLING_RATE_FIELD, "standard_rate"),
	"Sales Order": (ORDER_STATUS_FIELD, "title"),
	"Customer": (CUSTOMER_ID_FIELD, "series"),
}

_FETCHABLE_RESOURCES = {
	"Order": "order",
	"Customer": "customer",
}

_METAFIELDS_QUERY = """
query metafields($id: ID!) {
	%(resource)s(id: $id) {
		metafields(first: 50) {
			nodes {
				namespace
				key
				value
				type
			}
		}
	}
}
"""

_METAFIELD_OWNER_TYPES = {
	"Product": "PRODUCT",
	"Order": "ORDER",
	"Customer": "CUSTOMER",
}

_METAFIELD_DEFINITIONS_QUERY = """
query metafieldDefinitions($ownerType: MetafieldOwnerType!, $first: Int!, $after: String) {
	metafieldDefinitions(ownerType: $ownerType, first: $first, after: $after) {
		nodes {
			namespace
			key
			name
			type {
				name
			}
		}
		pageInfo {
			hasNextPage
			endCursor
		}
	}
}
"""


def _suggest_fieldname(key: str) -> str:
	"""Suggest a valid ERPNext fieldname for a metafield key."""
	suggested = re.sub(r"[^a-zA-Z0-9_]", "_", key or "")
	if not suggested or not suggested[0].isalpha():
		suggested = f"shopify_{suggested}" if suggested else "shopify_field"
	return suggested


@frappe.whitelist(methods=["GET"])
@temp_shopify_session
def fetch_shopify_metafield_definitions() -> dict[str, list[dict]]:
	"""Return the store's metafield definitions per resource, for the setting form's picker."""
	frappe.has_permission(SETTING_DOCTYPE, "read", throw=True)

	definitions = {}
	for resource, owner_type in _METAFIELD_OWNER_TYPES.items():
		nodes, cursor, has_next = [], None, True
		while has_next:
			response = json.loads(
				GraphQL().execute(
					_METAFIELD_DEFINITIONS_QUERY,
					{"ownerType": owner_type, "first": 100, "after": cursor},
				)
			)
			if response.get("errors"):
				create_shopify_log(
					status="Error",
					method="shopify_integration.shopify.metafields.fetch_shopify_metafield_definitions",
					message=_("Failed to fetch {0} metafield definitions from Shopify").format(resource),
					response_data=response,
					exception=response["errors"],
				)
				break
			data = (response.get("data") or {}).get("metafieldDefinitions") or {}
			nodes += data.get("nodes") or []
			page_info = data.get("pageInfo") or {}
			has_next = page_info.get("hasNextPage", False)
			cursor = page_info.get("endCursor")
		if nodes:
			definitions[resource] = [
				{
					"namespace": n.get("namespace"),
					"key": n.get("key"),
					"name": n.get("name"),
					"type": (n.get("type") or {}).get("name"),
					"suggested_fieldname": _suggest_fieldname(n.get("key")),
				}
				for n in nodes
			]
	return definitions


def get_metafield_mappings(resource: str) -> list[dict]:
	"""Return the mapping rows for a Shopify resource."""
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	return [
		{"namespace": d.namespace, "key": d.key, "erpnext_fieldname": d.erpnext_fieldname}
		for d in setting.metafield_mapping
		if d.shopify_resource == resource
	]


_METAOBJECT_REFERENCE_TYPES = {"metaobject_reference", "list.metaobject_reference"}

TEXT_FIELD_TYPES = ("Data", "Small Text", "Text", "Long Text")

_UPDATED_RESOURCE_DOCTYPES = {
	"Order": ("Sales Order", ORDER_ID_FIELD),
	"Customer": ("Customer", CUSTOMER_ID_FIELD),
}

_METAOBJECT_QUERY = """
query metaobject($id: ID!) {
	metaobject(id: $id) {
		displayName
	}
}
"""


@temp_shopify_session
def _fetch_metaobject_display_name(gid: str) -> str | None:
	"""Fetch a metaobject entry's display name by GID, or None on failure."""
	response = json.loads(GraphQL().execute(_METAOBJECT_QUERY, {"id": gid}))

	metaobject = (response.get("data") or {}).get("metaobject")
	if response.get("errors") or not metaobject:
		details = "; ".join(e.get("message", "") for e in response.get("errors") or []) or _(
			"entry not found"
		)
		create_shopify_log(
			status="Warning",
			method="shopify_integration.shopify.metafields._fetch_metaobject_display_name",
			message=_(
				"Could not resolve metaobject reference {0}: {1}. If this is an access"
				" error, add the read_metaobjects scope to the app in the Shopify Dev"
				" Dashboard and reinstall it."
			).format(gid, details),
			response_data=response,
		)
		return None

	return metaobject.get("displayName")


def _resolve_metaobject_value(metafield: dict) -> str | None:
	"""Resolve a metaobject reference to its display name(s), e.g. "Red" or "Red, Blue"."""
	if metafield.get("type") == "metaobject_reference":
		return _fetch_metaobject_display_name(metafield.get("value")) or metafield.get("value")

	# list.metaobject_reference: the value is a JSON-encoded array of GIDs
	try:
		gids = json.loads(metafield.get("value") or "[]")
	except (TypeError, ValueError):
		return metafield.get("value")

	names = [name for gid in gids if (name := _fetch_metaobject_display_name(gid))]
	return ", ".join(names) if names else metafield.get("value")


def _mapped_field_values(
	resource: str, metafields: list[dict] | None, mappings: list[dict] | None = None
) -> dict[str, str]:
	"""Match metafields against mapping rows into {erpnext_fieldname: value}."""
	mappings = mappings if mappings is not None else get_metafield_mappings(resource)
	if not mappings or not metafields:
		return {}

	nodes = {(m.get("namespace"), m.get("key")): m for m in metafields}
	field_values = {}
	for row in mappings:
		node = nodes.get((row["namespace"], row["key"]))
		if node is None:
			continue

		value = node.get("value")
		if node.get("type") in _METAOBJECT_REFERENCE_TYPES:
			value = _resolve_metaobject_value(node)

		if value is not None:
			field_values[row["erpnext_fieldname"]] = value

	return field_values


def apply_metafields(target, resource: str, metafields: list[dict] | None) -> list[str]:
	"""Set mapped metafield values on a document or dict. Returns the fieldnames set."""
	field_values = _mapped_field_values(resource, metafields)
	if not field_values:
		return []

	for fieldname, value in field_values.items():
		if isinstance(target, dict):
			target[fieldname] = value
		else:
			target.set(fieldname, value)

	return list(field_values)


def _changed_fields(current: dict | None, field_values: dict) -> dict:
	return {f: v for f, v in field_values.items() if (current or {}).get(f) != v}


@temp_shopify_session
def fetch_metafields(resource: str, resource_id) -> list[dict]:
	"""Fetch a resource's metafields from Shopify."""
	graphql_name = _FETCHABLE_RESOURCES.get(resource)
	if not graphql_name:
		return []

	gid = f"gid://shopify/{resource}/{resource_id}"
	response = json.loads(GraphQL().execute(_METAFIELDS_QUERY % {"resource": graphql_name}, {"id": gid}))

	if response.get("errors"):
		create_shopify_log(
			status="Error",
			method="shopify_integration.shopify.metafields.fetch_metafields",
			message=_("Failed to fetch {0} metafields from Shopify").format(resource),
			response_data=response,
			exception=response["errors"],
		)
		return []

	node = (response.get("data") or {}).get(graphql_name) or {}
	return list((node.get("metafields") or {}).get("nodes") or [])


@temp_shopify_session
def sync_product_metafields(product_id, mappings: list[dict] | None = None) -> list[str]:
	"""Refresh mapped metafields onto every Item linked to a product."""
	mappings = mappings if mappings is not None else get_metafield_mappings("Product")
	if not mappings:
		return []

	# local import to avoid circular dependency
	from shopify_integration.shopify.product import _fetch_shopify_product

	metafields = _fetch_shopify_product(product_id).get("metafields") or []
	field_values = _mapped_field_values("Product", metafields, mappings=mappings)
	if not field_values:
		return []

	item_codes = frappe.get_all(
		"Ecommerce Item",
		filters={"integration": MODULE_NAME, "integration_item_code": cstr(product_id)},
		pluck="erpnext_item_code",
	)
	if not item_codes:
		return []

	existing = {
		row.name: row
		for row in frappe.get_all(
			"Item", filters={"name": ["in", item_codes]}, fields=["name", *field_values]
		)
	}
	for item_code in item_codes:
		changed = _changed_fields(existing.get(item_code), field_values)
		if changed:
			frappe.db.set_value("Item", item_code, changed)

	return item_codes


@frappe.whitelist(methods=["POST"])
def sync_order_metafields(shopify_order_id: str) -> str:
	"""Refresh mapped metafields onto an existing Sales Order. Returns its name."""
	frappe.has_permission("Sales Order", "write", throw=True)

	so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: cstr(shopify_order_id)})
	if not so_name:
		frappe.throw(_("Sales Order not found for Shopify order {0}").format(shopify_order_id))

	sync_metafields_to_doc("Sales Order", so_name, "Order", shopify_order_id)
	return so_name


def handle_order_updated(payload, request_id=None):
	"""Webhook handler for orders/updated."""
	_handle_resource_updated("Order", payload, request_id)


def handle_product_updated(payload, request_id=None):
	"""Webhook handler for products/update."""
	_handle_resource_updated("Product", payload, request_id)


def handle_customer_updated(payload, request_id=None):
	"""Webhook handler for customers/update."""
	_handle_resource_updated("Customer", payload, request_id)


def _handle_resource_updated(resource: str, payload: dict, request_id: str | None = None) -> None:
	"""Refresh the metafields of the ERPNext documents linked to an updated resource."""
	# nosemgrep: frappe-setuser
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	try:
		if resource == "Product":
			sync_product_metafields(payload.get("id"))
		else:
			mappings = get_metafield_mappings(resource)
			if mappings:
				doctype, id_field = _UPDATED_RESOURCE_DOCTYPES[resource]
				docname = frappe.db.get_value(doctype, {id_field: cstr(payload.get("id"))})
				if docname:
					sync_metafields_to_doc(doctype, docname, resource, payload.get("id"), mappings=mappings)
		create_shopify_log(status="Success")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def refresh_recent_order_metafields(days: int = 7) -> None:
	"""Re-query mapped order metafields for Sales Orders from the last `days` days."""
	if not frappe.get_cached_doc(SETTING_DOCTYPE).is_enabled():
		return

	mappings = get_metafield_mappings("Order")
	if not mappings:
		return

	recent_orders = frappe.get_all(
		"Sales Order",
		filters={
			ORDER_ID_FIELD: ("is", "set"),
			"creation": (">", add_days(now_datetime(), -days)),
		},
		fields=["name", ORDER_ID_FIELD],
	)

	for order in recent_orders:
		try:
			sync_metafields_to_doc(
				"Sales Order", order.name, "Order", order[ORDER_ID_FIELD], mappings=mappings
			)
		except Exception as e:
			create_shopify_log(
				status="Error",
				method="shopify_integration.shopify.metafields.refresh_recent_order_metafields",
				message=_("Failed to refresh metafields for Sales Order {0}").format(order.name),
				exception=e,
			)


def sync_metafields_to_doc(
	doctype: str, docname: str, resource: str, resource_id, mappings: list[dict] | None = None
) -> None:
	"""Fetch a resource's metafields and write the mapped values onto a document."""
	mappings = mappings if mappings is not None else get_metafield_mappings(resource)
	if not mappings:
		return

	metafields = fetch_metafields(resource, resource_id)
	if not metafields:
		return

	field_values = _mapped_field_values(resource, metafields, mappings=mappings)
	changed = _changed_fields(
		frappe.db.get_value(doctype, docname, list(field_values), as_dict=True), field_values
	)
	if changed:
		frappe.db.set_value(doctype, docname, changed)


def ensure_metafield_custom_fields(mapping_rows) -> None:
	"""Create the target custom field for each mapping row when missing."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	fieldnames_by_doctype = {}
	seen = set()
	for row in mapping_rows:
		doctype = RESOURCE_TO_DOCTYPE.get(row.shopify_resource)
		fieldname = row.erpnext_fieldname
		if not doctype or not fieldname or (doctype, fieldname) in seen:
			continue
		seen.add((doctype, fieldname))
		fieldnames_by_doctype.setdefault(doctype, []).append(fieldname)

	fields_to_create = {}
	for doctype, fieldnames in fieldnames_by_doctype.items():
		meta = frappe.get_meta(doctype)

		doctype_fields = []
		if not meta.has_field(_METAFIELD_SECTION["fieldname"]):
			preferred, fallback = _SECTION_ANCHORS.get(doctype, (None, None))
			anchor = preferred if preferred and meta.has_field(preferred) else fallback
			doctype_fields.append(dict(_METAFIELD_SECTION, insert_after=anchor))

		prev_fieldname = _METAFIELD_SECTION["fieldname"]
		for fieldname in fieldnames:
			if not meta.has_field(fieldname):
				doctype_fields.append(
					dict(
						fieldname=fieldname,
						label=fieldname.replace("_", " ").title(),
						fieldtype="Data",
						insert_after=prev_fieldname,
					)
				)
			prev_fieldname = fieldname

		if doctype_fields:
			fields_to_create[doctype] = doctype_fields

	if fields_to_create:
		create_custom_fields(fields_to_create)
