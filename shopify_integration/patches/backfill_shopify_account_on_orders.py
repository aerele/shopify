# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

"""Tags existing Shopify orders and invoices with their store."""

import frappe

from shopify_integration.shopify.constants import ORDER_ACCOUNT_FIELD, ORDER_ID_FIELD


def execute():
	# The store is only guessable when exactly one account exists; otherwise
	# rows stay untagged and new syncs fill the field going forward.
	accounts = frappe.get_all("Shopify Account", pluck="name")
	if len(accounts) != 1:
		return

	for doctype in ("Sales Order", "Sales Invoice"):
		frappe.db.set_value(
			doctype,
			{ORDER_ID_FIELD: ("is", "set"), ORDER_ACCOUNT_FIELD: ("is", "not set")},
			ORDER_ACCOUNT_FIELD,
			accounts[0],
			update_modified=False,
		)
