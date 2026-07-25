// Item form client script for Shopify Integration
// Makes has_variants field read-only for items synced from Shopify

frappe.ui.form.on("Item", {
	refresh: function (frm) {
		// Check if item is synced from Shopify
		if (frm.doc.__islocal) {
			return;
		}

		// Call server method to check if this item is synced from Shopify
		frappe.call({
			method: "shopify_integration.shopify.product.is_item_synced_from_shopify",
			args: {
				item_code: frm.doc.name,
			},
			callback: function (r) {
				if (r.message && r.message.is_synced) {
					// Make has_variants field read-only
					frm.set_df_property("has_variants", "read_only", 1);

					var has_variants_field = frm.fields_dict["has_variants"];
					if (has_variants_field && has_variants_field.$wrapper) {
						// Add help text if it doesn't already exist
						if (!has_variants_field.$wrapper.find(".shopify-sync-help").length) {
							var help_text = $(
								'<div class="shopify-sync-help" style="font-size: 13px; color: #6c757d;">' +
									__(
										"This field cannot be modified. Items synced from Shopify have fixed variant behavior."
									) +
									"</div>"
							);
							has_variants_field.$wrapper.append(help_text);
						}
					}
				}
			},
		});
	},
});
