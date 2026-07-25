import frappe
from erpnext.selling.doctype.sales_order.mapper import make_sales_invoice
from frappe.utils import cint, cstr, flt, getdate, nowdate

from shopify_integration.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	SETTING_DOCTYPE,
)
from shopify_integration.shopify.utils import create_shopify_log


def prepare_sales_invoice(payload, request_id=None):
	from shopify_integration.shopify.order import get_sales_order

	order = payload

	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	try:
		sales_order = get_sales_order(cstr(order["id"]))
		if sales_order:
			status, result = create_sales_invoice(order, setting, sales_order)
			if status == "success":
				# Sales Invoice created successfully
				create_shopify_log(status="Success")
			elif status == "invalid":
				# Skip - invoice already exists, already billed, or sync disabled
				create_shopify_log(status="Invalid", message=result)
			else:
				# Error - can be retried after fixing the issue
				create_shopify_log(
					status="Error",
					method="shopify_integration.shopify.invoice.prepare_sales_invoice",
					message=result,
				)
		else:
			create_shopify_log(
				status="Invalid",
				method="shopify_integration.shopify.invoice.prepare_sales_invoice",
				message="Sales Order not found for syncing sales invoice.",
			)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def create_sales_invoice(shopify_order, setting, so) -> tuple[str, str]:
	"""Create a Sales Invoice for this order if one doesn't exist yet.
	Returns a tuple (status, result) where:
	- status: "success" if created, "invalid" if skipped (no retry needed), "error" if failed (can retry)
	- result: Invoice name on success, reason message on invalid/error."""

	# Check if invoice already exists (INVALID - already done, no retry needed)
	existing_invoice = frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")
	if existing_invoice:
		return ("invalid", f"Sales Invoice {existing_invoice} already exists for this order")

	# Check if Sales Order is submitted (ERROR - can be retried after submitting)
	if so.docstatus != 1:
		return ("error", f"Sales Order {so.name} is not submitted (current status: Draft)")

		# Check if Sales Order is already fully billed (INVALID - already done, no retry needed)
		if flt(so.per_billed) == 100:
			return ("invalid", f"Sales Order {so.name} is already fully billed ({so.per_billed}% billed)")

	# Check if sales invoice sync is enabled (INVALID - configuration, no retry needed)
	if not cint(setting.sync_sales_invoice):
		return ("Error", "Sales Invoice sync is disabled in Shopify settings")

	# All checks passed - create the invoice
	posting_date = getdate(shopify_order.get("created_at")) or nowdate()

	sales_invoice = make_sales_invoice(so.name, ignore_permissions=True)
	sales_invoice.set(ORDER_ID_FIELD, str(shopify_order.get("id")))
	sales_invoice.set(ORDER_NUMBER_FIELD, shopify_order.get("name"))
	sales_invoice.set_posting_time = 1
	sales_invoice.posting_date = posting_date
	sales_invoice.due_date = posting_date
	sales_invoice.naming_series = setting.sales_invoice_series or "SI-Shopify-"
	sales_invoice.flags.ignore_mandatory = True
	set_cost_center(sales_invoice.items, setting.cost_center)
	sales_invoice.insert(ignore_mandatory=True)
	sales_invoice.submit()
	if sales_invoice.grand_total > 0:
		make_payment_entry_against_sales_invoice(sales_invoice, setting, posting_date)

	if shopify_order.get("note"):
		sales_invoice.add_comment(text=f"Order Note: {shopify_order.get('note')}")

	return ("success", sales_invoice.name)


def set_cost_center(items, cost_center):
	for item in items:
		item.cost_center = cost_center


def make_payment_entry_against_sales_invoice(doc, setting, posting_date=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=setting.cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = doc.name
	payment_entry.posting_date = posting_date or nowdate()
	payment_entry.reference_date = posting_date or nowdate()
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
