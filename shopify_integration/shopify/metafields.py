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

# Mapping row "Shopify Resource" value -> ERPNext doctype the value is written to
RESOURCE_TO_DOCTYPE = {
	"Product": "Item",
	"Order": "Sales Order",
	"Customer": "Customer",
}

# All metafield fields live together in one section on the target doctype.
# Without an explicit position, Frappe appends the field to the end of the
# form, which on tabbed forms like Item spawns a stray duplicate tab.
_METAFIELD_SECTION = {
	"fieldname": "shopify_metafields_section",
	"label": "Shopify Metafields",
	"fieldtype": "Section Break",
	"collapsible": 1,
}

# The section is anchored right after the connector's own custom fields when
# they exist (integration enabled), else after the core field those use.
_SECTION_ANCHORS = {
	"Item": (ITEM_SELLING_RATE_FIELD, "standard_rate"),
	"Sales Order": (ORDER_STATUS_FIELD, "title"),
	"Customer": (CUSTOMER_ID_FIELD, "series"),
}

# Resources whose metafields are fetched with a standalone query. Product
# metafields ride along in the product query instead (see product._PRODUCT_QUERY).
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

# Shopify resources -> GraphQL MetafieldOwnerType values for the
# metafieldDefinitions query
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
	"""Turn a Shopify metafield key into a valid ERPNext fieldname suggestion
	(the mapping validation only allows letters, digits and underscores)."""
	suggested = re.sub(r"[^a-zA-Z0-9_]", "_", key or "")
	if not suggested or not suggested[0].isalpha():
		suggested = f"shopify_{suggested}" if suggested else "shopify_field"
	return suggested


@frappe.whitelist(methods=["GET"])
@temp_shopify_session
def fetch_shopify_metafield_definitions() -> dict[str, list[dict]]:
	"""Return the store's metafield definitions per mapped resource.

	Definitions (Settings -> Custom data in the Shopify admin) describe every
	metafield the store can use, including ones with no value set yet -
	unlike querying metafield values, which only shows what is filled in.
	Used by the Shopify Setting form so mappings can be chosen from the
	store instead of typed in by hand.
	"""
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
	"""Return the configured mapping rows for a Shopify resource.

	Each row: {"namespace": ..., "key": ..., "erpnext_fieldname": ...}
	"""
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	return [
		{"namespace": d.namespace, "key": d.key, "erpnext_fieldname": d.erpnext_fieldname}
		for d in setting.metafield_mapping
		if d.shopify_resource == resource
	]


# metafield "type" values whose value is a metaobject reference - a single GID
# or a JSON-encoded list of them - instead of plain data
_METAOBJECT_REFERENCE_TYPES = {"metaobject_reference", "list.metaobject_reference"}

# fieldtypes an existing custom field may have to receive metafield values
# (they are stored as text); anything else (Int, Date, Link...) would corrupt
# or truncate the value
TEXT_FIELD_TYPES = ("Data", "Small Text", "Text", "Long Text")

# update-webhook resources that map to one ERPNext document found by id field;
# Product is handled separately (one product maps to many Items)
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
	"""Fetch one metaobject entry's display name (e.g. "Red") by GID.

	Returns None on failure so callers can keep the raw reference value; the
	log says to add the read_metaobjects scope if access is the problem.
	"""
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
	"""Replace a metaobject-reference value with the referenced entries'
	display names: "Red" for a single reference, "Red, Blue" for a list.
	Falls back to the raw reference when an entry can't be resolved."""
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
	"""Resolve a resource's metafields against its mapping rows into
	{erpnext_fieldname: value}. Only mapped metafields are included, so
	metaobject references (which cost one extra API call each) are only
	resolved when a mapping actually uses them. Pass `mappings` when calling
	in a loop to avoid reloading the setting per document."""
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
	"""Set mapped metafield values on an ERPNext document (or a dict of its fields).

	`target` may be a frappe Document or a plain dict (e.g. the item dict
	before create_ecommerce_item() inserts it). Values are stored as text;
	only mappings whose metafield exists on the resource are applied.

	Returns the fieldnames that were set.
	"""
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
	"""Return only the field_values entries that differ from `current`, so
	recurring refreshes (webhook redeliveries, the hourly job) don't rewrite
	- and re-timestamp - every document on every run."""
	return {f: v for f, v in field_values.items() if (current or {}).get(f) != v}


@temp_shopify_session
def fetch_metafields(resource: str, resource_id) -> list[dict]:
	"""Fetch a Shopify order's/customer's metafields via GraphQL."""
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
	"""Refresh mapped product metafields onto already-synced Items.

	Items imported before a metafield mapping was added never received its
	value, and importing again skips linked products - this backfills the
	mapped values onto every Item linked to the product (template and
	variants) without recreating anything. Returns the updated item codes.
	"""
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
	"""Refresh mapped metafields onto an existing Sales Order.

	Order metafields are often filled in Shopify only after the order was
	created, so the one-shot fetch at Sales Order creation may have found
	nothing. This re-fetches and re-stamps the mapped values.

	Can be run for a single order (`bench execute
	shopify_integration.shopify.metafields.sync_order_metafields
	--kwargs "{'shopify_order_id': '...'}"`) or from a client script.
	Returns the Sales Order name.
	"""
	frappe.has_permission("Sales Order", "write", throw=True)

	so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: cstr(shopify_order_id)})
	if not so_name:
		frappe.throw(_("Sales Order not found for Shopify order {0}").format(shopify_order_id))

	sync_metafields_to_doc("Sales Order", so_name, "Order", shopify_order_id)
	return so_name


def handle_order_updated(payload, request_id=None):
	"""Webhook handler for orders/updated: refresh mapped order metafields.

	Order metafields are often filled in Shopify only after the order was
	created, and the one-shot fetch at Sales Order creation may have found
	nothing. Shopify has no value-level metafields webhook, so this rides the
	order's own update event and re-queries the mapped metafields. Quietly
	no-ops when no Order mapping is configured or the order isn't synced.
	"""
	_handle_resource_updated("Order", payload, request_id)


def handle_product_updated(payload, request_id=None):
	"""Webhook handler for products/update: refresh mapped product metafields
	onto every synced Item linked to the product (see sync_product_metafields)."""
	_handle_resource_updated("Product", payload, request_id)


def handle_customer_updated(payload, request_id=None):
	"""Webhook handler for customers/update: refresh mapped customer metafields."""
	_handle_resource_updated("Customer", payload, request_id)


def _handle_resource_updated(resource: str, payload: dict, request_id: str | None = None) -> None:
	"""Shared body of the resource-update webhook handlers: refresh the mapped
	metafields of the ERPNext document(s) linked to the updated resource.

	Update deliveries carry no metafield values themselves, so the mapped
	values are re-queried per delivery. Quietly no-ops when no mapping is
	configured for the resource or nothing is synced for it.
	"""
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
	"""Scheduled safety net: re-query mapped order metafields for Sales Orders
	synced in the last `days` days.

	Webhook deliveries can be missed (endpoint downtime, tunnel changes), and
	orders synced before a metafield mapping was added never received their
	values - this backfills both. Costs one metafields query per recent order,
	so the window stays small.
	"""
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
			# one bad order must not abort the batch
			create_shopify_log(
				status="Error",
				method="shopify_integration.shopify.metafields.refresh_recent_order_metafields",
				message=_("Failed to refresh metafields for Sales Order {0}").format(order.name),
				exception=e,
			)


def sync_metafields_to_doc(
	doctype: str, docname: str, resource: str, resource_id, mappings: list[dict] | None = None
) -> None:
	"""Fetch metafields for a Shopify resource and write the mapped values
	onto an already-inserted ERPNext document.

	Used for Orders and Customers, whose webhook payloads carry no
	metafields, and whose documents may already be submitted (Sales Order),
	where writing via db_set is the only option. Pass `mappings` when calling
	in a loop to avoid reloading the setting per document.
	"""
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
	"""Create the ERPNext target field for each mapping row when missing, so
	configuring a mapping is enough - no separate custom field setup.

	Fields are placed inside the "Shopify Metafields" section on the target
	doctype, anchored to a known field so they never end up appended to the
	end of the form.
	"""
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

		# chain the fields one after another inside the section
		prev_fieldname = _METAFIELD_SECTION["fieldname"]
		for fieldname in fieldnames:
			if meta.has_field(fieldname):
				continue
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
