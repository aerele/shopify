"""Move the configuration of the `Shopify Setting` single into a `Shopify Account`.

The single held one store. Its values become the first account, named after the shop URL;
its child rows (tax accounts, warehouse mapping, registered webhooks) and its stored
password move along, so the store keeps working without registering its webhooks again.
Existing Ecommerce Items and Shopify integration logs belong to that store and are linked
to the account. Afterwards the single is removed.
"""

from pathlib import Path

import frappe
from frappe.model import table_fields
from frappe.modules.import_file import import_file_by_path
from frappe.query_builder.functions import IfNull

import shopify_integration
from shopify_integration.shopify.constants import ACCOUNT_DOCTYPE, MODULE_NAME
from shopify_integration.shopify.utils import normalize_shop_url

LEGACY_SETTING_DOCTYPE = "Shopify Setting"
CHILD_DOCTYPES = ("Shopify Tax Account", "Shopify Warehouse Mapping", "Shopify Webhooks")


def _reload_account_doctype():
	"""Reload Shopify Account from THIS app.

	`frappe.reload_doc("shopify", ...)` cannot be used here: the module name
	"shopify" exists in both `shopify_integration` and `ecommerce_integrations`,
	and module-to-app resolution may pick the wrong app, clobbering the doctype
	with the other app's (stale) definition.
	"""
	app_path = Path(shopify_integration.__file__).parent
	doctype_json = app_path / "shopify" / "doctype" / "shopify_account" / "shopify_account.json"
	import_file_by_path(str(doctype_json), force=True)


def execute():
	_reload_account_doctype()
	frappe.reload_doc("ecommerce_core", "doctype", "ecommerce_item")
	frappe.reload_doc("ecommerce_core", "doctype", "ecommerce_integration_log")

	values = get_legacy_values()
	shopify_url = normalize_shop_url(values.get("shopify_url"))

	if shopify_url and not frappe.db.exists(ACCOUNT_DOCTYPE, shopify_url):
		create_account(shopify_url, values)
		move_child_rows(shopify_url)
		move_passwords(shopify_url)
		link_existing_records(shopify_url)

	if frappe.db.exists("DocType", LEGACY_SETTING_DOCTYPE):
		frappe.delete_doc("DocType", LEGACY_SETTING_DOCTYPE, ignore_missing=True, force=True)
	# deleting the DocType leaves the values of a single behind
	frappe.db.delete("Singles", {"doctype": LEGACY_SETTING_DOCTYPE})


def get_legacy_values() -> dict:
	# raw SQL: `Singles` is an internal table without a DocType schema, so qb can't address it
	return dict(
		frappe.db.sql(
			"select field, value from `tabSingles` where doctype = %s",
			LEGACY_SETTING_DOCTYPE,
		)
	)


def create_account(shopify_url: str, values: dict) -> None:
	"""Insert the account row without running its controller.

	Validation would register the webhooks with Shopify again, and on_update would start the
	old-connector migration; both already happened for this store.
	"""
	account = frappe.new_doc(ACCOUNT_DOCTYPE)
	for df in account.meta.fields:
		if df.fieldtype in table_fields or df.fieldname not in values:
			continue
		account.set(df.fieldname, values[df.fieldname])

	account.name = shopify_url
	account.shopify_url = shopify_url
	account.db_insert()


def move_child_rows(shopify_url: str) -> None:
	for child_doctype in CHILD_DOCTYPES:
		table = frappe.qb.DocType(child_doctype)
		(
			frappe.qb.update(table)
			.set(table.parent, shopify_url)
			.set(table.parenttype, ACCOUNT_DOCTYPE)
			.where(table.parenttype == LEGACY_SETTING_DOCTYPE)
		).run()


def move_passwords(shopify_url: str) -> None:
	# raw SQL: `__Auth` is an internal table; re-keying the stored rows in place keeps the
	# encrypted values untouched, which the password API (set/remove only) cannot express
	frappe.db.sql(
		"""update `__Auth`
		set doctype = %(account_doctype)s, name = %(account)s
		where doctype = %(legacy)s and name = %(legacy)s""",
		{"account": shopify_url, "account_doctype": ACCOUNT_DOCTYPE, "legacy": LEGACY_SETTING_DOCTYPE},
	)


def link_existing_records(shopify_url: str) -> None:
	for doctype in ("Ecommerce Item", "Ecommerce Integration Log"):
		table = frappe.qb.DocType(doctype)
		(
			frappe.qb.update(table)
			.set(table.shopify_account, shopify_url)
			.where(table.integration == MODULE_NAME)
			.where(IfNull(table.shopify_account, "") == "")
		).run()
