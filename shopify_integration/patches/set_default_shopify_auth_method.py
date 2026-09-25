from pathlib import Path

import frappe
from frappe.modules.import_file import import_file_by_path

import shopify_integration
from shopify_integration.shopify.constants import AUTH_METHOD_STATIC, SETTING_DOCTYPE


def _reload_setting_doctype():
	"""Reload Shopify Setting from THIS app.

	`frappe.reload_doc("shopify", ...)` cannot be used here: the module name
	"shopify" exists in both `shopify_integration` and `ecommerce_integrations`,
	and module-to-app resolution may pick the wrong app, clobbering the doctype
	with the other app's (stale) definition.
	"""
	app_path = Path(shopify_integration.__file__).parent
	doctype_json = app_path / "shopify" / "doctype" / "shopify_setting" / "shopify_setting.json"
	import_file_by_path(str(doctype_json), force=True)


def execute():
	"""
	Migration patch: set default authentication method for existing Shopify installations.
	Ensures backward compatibility when introducing OAuth 2.0 support.

	Existing installs get "Static Token" so their current setup continues working.
	"""
	_reload_setting_doctype()

	if frappe.db.exists("DocType", SETTING_DOCTYPE):
		settings = frappe.get_doc(SETTING_DOCTYPE)

		if not settings.authentication_method:
			settings.db_set("authentication_method", AUTH_METHOD_STATIC, update_modified=False)
			frappe.db.commit()

			frappe.logger().info(
				"Shopify Setting: Set default authentication method to 'Static Token' for existing installation"
			)
