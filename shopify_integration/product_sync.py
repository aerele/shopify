"""Product Sync adapter for the shared Ecommerce Core page.

Registered through the ``ecommerce_product_sync_providers`` hook (see hooks.py).
A thin facade over the existing Shopify product sync; the page's list and
search reuse the GraphQL helpers of the Shopify import page.
"""

from __future__ import annotations

import json

import frappe
from ecommerce_core.ecommerce_core.doctype.ecommerce_item import ecommerce_item
from ecommerce_core.product_sync.adapter import ProductSyncAdapter
from shopify import GraphQL

from shopify_integration.shopify.connection import temp_shopify_session
from shopify_integration.shopify.constants import MODULE_NAME, SETTING_DOCTYPE
from shopify_integration.shopify.page.shopify_import_products.shopify_import_products import (
	_PRODUCT_VARIANTS_QUERY,
	_build_product_search_query,
	_fetch_products_from_shopify,
	_gid_to_id,
	get_shopify_product_count,
)
from shopify_integration.shopify.product import ShopifyProduct


class ShopifyProductSyncAdapter(ProductSyncAdapter):
	"""Contract implementation for ``ecommerce_core.product_sync``."""

	def is_enabled(self) -> bool:
		return bool(frappe.db.get_single_value(SETTING_DOCTYPE, "enable_shopify"))

	def get_products(self, limit: int, cursor: str | None = None, search: str | None = None) -> dict:
		# The opaque cursor is the connector's own "n:<endCursor>" token.
		page = _fetch_products_from_shopify(
			from_=cursor, limit=limit, query=_build_product_search_query(search)
		)
		return {
			"products": [
				{
					"id": product["id"],
					"name": product["title"],
					"sku": next((v["sku"] for v in product["variants"] if v.get("sku")), None),
					"variants": product["variants"],
				}
				for product in page["products"]
			],
			"total": None,
			"next_cursor": page["next_cursor"],
		}

	def get_provider_count(self) -> int:
		return int(get_shopify_product_count() or 0)

	def sync_product(self, product_id: str) -> dict:
		was_synced = ecommerce_item.is_synced(MODULE_NAME, integration_item_code=str(product_id))
		for variant_id in self._variant_ids(product_id) or [None]:
			ShopifyProduct(product_id, variant_id=variant_id).sync_product()
		return {"action": "updated" if was_synced else "created"}

	@temp_shopify_session
	def _variant_ids(self, product_id: str) -> list[str]:
		response = json.loads(
			GraphQL().execute(_PRODUCT_VARIANTS_QUERY, {"id": f"gid://shopify/Product/{product_id}"})
		)
		product = response.get("data", {}).get("product")
		if not product:
			frappe.throw(f"Shopify product {product_id} not found")
		edges = (product.get("variants") or {}).get("edges", [])
		return [_gid_to_id((edge.get("node") or {}).get("id")) for edge in edges]
