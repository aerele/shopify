import frappe
from erpnext.selling.doctype.sales_order.mapper import make_sales_invoice
from frappe.utils import cint, cstr, getdate, nowdate

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
			created_sales_invoice = create_sales_invoice(order, setting, sales_order)
			if created_sales_invoice:
				create_shopify_log(status="Success")
			else:
				create_shopify_log(
					status="Invalid",
					message=(
						"No sales invoice was created for this order (sales invoice sync is"
						" disabled, an invoice already exists, the Sales Order isn't submitted,"
						" or it's already fully billed)."
					),
				)
		else:
			create_shopify_log(status="Invalid", message="Sales Order not found for syncing sales invoice.")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def create_sales_invoice(shopify_order, setting, so) -> str | None:
	"""Create a Sales Invoice for this order if one doesn't exist yet.
	Returns the created Sales Invoice's name, or None if nothing was
	created, so callers can tell an actual sync apart from a no-op (e.g.
	sales invoice sync disabled, or already invoiced)."""
	if (
		not frappe.db.get_value("Sales Invoice", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")
		and so.docstatus == 1
		and not so.per_billed
		and cint(setting.sync_sales_invoice)
	):
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
			make_payament_entry_against_sales_invoice(sales_invoice, setting, posting_date)

		if shopify_order.get("note"):
			sales_invoice.add_comment(text=f"Order Note: {shopify_order.get('note')}")

		return sales_invoice.name

	return None


def set_cost_center(items, cost_center):
	for item in items:
		item.cost_center = cost_center


def make_payament_entry_against_sales_invoice(doc, setting, posting_date=None):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=setting.cash_bank_account)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = doc.name
	payment_entry.posting_date = posting_date or nowdate()
	payment_entry.reference_date = posting_date or nowdate()
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
