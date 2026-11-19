from pathlib import Path

import frappe
from frappe.modules.import_file import import_file_by_path

import shopify_integration
from shopify_integration.shopify.constants import ACCOUNT_DOCTYPE, AUTH_METHOD_STATIC


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
	"""
	Migration patch: set default authentication method for existing Shopify installations.
	Ensures backward compatibility when introducing OAuth 2.0 support.

	Existing accounts get "Static Token" so their current setup continues working. It runs
	after the `Shopify Setting` single was migrated into a `Shopify Account`.
	"""
	_reload_account_doctype()

	account = frappe.qb.DocType(ACCOUNT_DOCTYPE)
	(
		frappe.qb.update(account)
		.set(account.authentication_method, AUTH_METHOD_STATIC)
		.where(account.authentication_method.isnull() | (account.authentication_method == ""))
	).run()
