// Copyright (c) 2021, Frappe and contributors
// For license information, please see LICENSE

frappe.ui.form.on("Shopify Setting", {
	onload: function (frm) {
		frappe.call({
			method: "ecommerce_core.utils.naming_series.get_series",
			callback: function (r) {
				$.each(r.message, (key, value) => {
					set_field_options(key, value);
				});
			},
		});
	},

	fetch_shopify_locations: function (frm) {
		frappe.call({
			doc: frm.doc,
			method: "update_location_table",
			callback: (r) => {
				if (!r.exc) refresh_field("shopify_warehouse_mapping");
			},
		});
	},

	refresh: function (frm) {
		frm.add_custom_button(__("Import Products"), function () {
			frappe.set_route("shopify-import-products");
		});
		frm.add_custom_button(__("View Logs"), () => {
			frappe.set_route("List", "Ecommerce Integration Log", {
				integration: "Shopify",
			});
		});
		if (frm.doc.enable_shopify) {
			frm.add_custom_button(__("Fetch Shopify Metafields"), () => {
				frappe.call({
					method: "shopify_integration.shopify.metafields.fetch_shopify_metafield_definitions",
					type: "GET",
					callback: (r) => {
						if (!r.exc) show_metafield_picker(frm, r.message || {});
					},
				});
			});
		}
		frm.trigger("setup_queries");
	},

	setup_queries: function (frm) {
		const warehouse_query = () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: 0,
					disabled: 0,
				},
			};
		};
		frm.set_query("warehouse", warehouse_query);
		frm.set_query("erpnext_warehouse", "shopify_warehouse_mapping", warehouse_query);

		frm.set_query("price_list", () => {
			return {
				filters: {
					selling: 1,
				},
			};
		});

		frm.set_query("cost_center", () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: "No",
				},
			};
		});

		frm.set_query("cash_bank_account", () => {
			return {
				filters: [
					["Account", "account_type", "in", ["Cash", "Bank"]],
					["Account", "root_type", "=", "Asset"],
					["Account", "is_group", "=", 0],
					["Account", "company", "=", frm.doc.company],
				],
			};
		});

		const tax_query = () => {
			return {
				query: "erpnext.controllers.queries.tax_account_query",
				filters: {
					account_type: ["Tax", "Chargeable", "Expense Account"],
					company: frm.doc.company,
				},
			};
		};

		frm.set_query("tax_account", "taxes", tax_query);
		frm.set_query("default_sales_tax_account", tax_query);
		frm.set_query("default_shipping_charges_account", tax_query);
	},
});

function show_metafield_picker(frm, definitions) {
	const mapped = new Set(
		(frm.doc.metafield_mapping || []).map((d) =>
			[d.shopify_resource, d.namespace, d.key].join("::")
		)
	);

	const rows = [];
	$.each(definitions, (resource, defs) => {
		$.each(defs, (_i, d) => {
			rows.push({
				resource: resource,
				namespace: d.namespace,
				key: d.key,
				name: d.name,
				type: d.type,
				suggested_fieldname: d.suggested_fieldname,
				is_mapped: mapped.has([resource, d.namespace, d.key].join("::")),
			});
		});
	});

	if (!rows.length) {
		frappe.msgprint(__("No metafield definitions found on the Shopify store."));
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: __("Choose Shopify Metafields to Map"),
		size: "extra-large",
		fields: [{ fieldtype: "HTML", fieldname: "definitions_html" }],
		primary_action_label: __("Add Mappings"),
		primary_action: () => {
			const checked = dialog.$wrapper.find("input.metafield-check:checked");
			checked.each(function () {
				const d = JSON.parse(decodeURIComponent(this.dataset.definition));
				frm.add_child("metafield_mapping", {
					shopify_resource: d.resource,
					namespace: d.namespace,
					key: d.key,
					erpnext_fieldname: d.suggested_fieldname,
				});
			});
			if (checked.length) {
				frm.refresh_field("metafield_mapping");
				frappe.show_alert({
					message: __("{0} mapping row(s) added - adjust the fieldnames and save", [
						checked.length,
					]),
					indicator: "green",
				});
			}
			dialog.hide();
		},
	});

	const row_html = (d) => `
		<tr>
			<td style="width: 30px">
				<input type="checkbox" class="metafield-check"
					data-definition="${encodeURIComponent(JSON.stringify(d))}" ${d.is_mapped ? "disabled" : ""}>
			</td>
			<td>${frappe.utils.escape_html(d.resource || "")}</td>
			<td>${frappe.utils.escape_html(d.namespace || "")}</td>
			<td>${frappe.utils.escape_html(d.key || "")}</td>
			<td>${frappe.utils.escape_html(d.name || "")}</td>
			<td>${frappe.utils.escape_html(d.type || "")}</td>
			<td>${d.is_mapped ? __("Already mapped") : ""}</td>
		</tr>`;

	dialog.fields_dict.definitions_html.$wrapper.html(`
		<div style="max-height: 420px; overflow: auto;">
			<table class="table table-bordered">
				<thead>
					<tr>
						<th></th>
						<th>${__("Resource")}</th>
						<th>${__("Namespace")}</th>
						<th>${__("Key")}</th>
						<th>${__("Name")}</th>
						<th>${__("Type")}</th>
						<th></th>
					</tr>
				</thead>
				<tbody>${rows.map(row_html).join("")}</tbody>
			</table>
		</div>
	`);
	dialog.show();
}
